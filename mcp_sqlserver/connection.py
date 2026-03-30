"""Connection manager — pool per-source, health checks, async wrapper.

Each source gets its own pyodbc connection. All blocking pyodbc calls are
wrapped in asyncio thread executor to avoid blocking the FastMCP event loop.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

import pyodbc

from mcp_sqlserver.config import AppConfig, GuardrailConfig, SourceConfig

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages pyodbc connections for all configured sources."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._connections: dict[str, pyodbc.Connection] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect_all(self) -> None:
        """Connect to all non-lazy sources at startup."""
        for source in self._config.sources:
            if source.lazy:
                logger.info("[%s] Lazy — will connect on first use", source.id)
                continue
            self._connect_source(source)

    def close_all(self) -> None:
        """Close all open connections."""
        for source_id, conn in self._connections.items():
            try:
                conn.close()
                logger.info("[%s] Disconnected", source_id)
            except Exception as exc:
                logger.warning("[%s] Error closing connection: %s", source_id, exc)
        self._connections.clear()

    def _connect_source(self, source: SourceConfig) -> pyodbc.Connection:
        """Open a connection to a single source."""
        guardrail = self._config.get_guardrail(source.id)
        try:
            conn = pyodbc.connect(source.dsn, autocommit=True, timeout=guardrail.query_timeout)
            conn.add_output_converter(-155, _handle_datetimeoffset)
            self._connections[source.id] = conn
            logger.info("[%s] Connected — %s", source.id, source.description or source.dsn[:50])
            return conn
        except pyodbc.Error as exc:
            logger.error("[%s] Connection failed: %s", source.id, exc)
            raise

    def _ensure_connected(self, source_id: str) -> pyodbc.Connection:
        """Get or create connection for a source (handles lazy connect)."""
        if source_id in self._connections:
            conn = self._connections[source_id]
            # Test connection health
            try:
                conn.execute("SELECT 1")
                return conn
            except Exception:
                logger.warning("[%s] Connection lost — reconnecting", source_id)
                try:
                    conn.close()
                except Exception:
                    pass
                del self._connections[source_id]

        source = self._config.get_source(source_id)
        if not source:
            raise ValueError(f"Unknown database source: '{source_id}'")
        return self._connect_source(source)

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------

    def health_check(self) -> list[dict[str, Any]]:
        """Run health check on all configured sources. Returns status per source."""
        results = []
        for source in self._config.sources:
            result = self._check_source(source)
            results.append(result)
            status = result["status"]
            msg = result["message"]
            if status == "ok":
                logger.info("[%s] OK — %s", source.id, msg)
            else:
                logger.error("[%s] %s — %s", source.id, status.upper(), msg)
        return results

    def _check_source(self, source: SourceConfig) -> dict[str, Any]:
        """Check connectivity for a single source."""
        result: dict[str, Any] = {"source": source.id, "database": source.description}

        # Step 1: Network check
        host, port = _extract_host_port(source.dsn)
        if host:
            try:
                sock = socket.create_connection((host, port), timeout=5)
                sock.close()
            except (OSError, socket.timeout):
                result["status"] = "network_error"
                result["message"] = f"Cannot reach {host}:{port} — VPN connected?"
                return result

        # Step 2: Auth + query check
        guardrail = self._config.get_guardrail(source.id)
        try:
            conn = pyodbc.connect(source.dsn, autocommit=True, timeout=guardrail.query_timeout)
            conn.add_output_converter(-155, _handle_datetimeoffset)
            cursor = conn.cursor()
            cursor.execute("SELECT DB_NAME() AS db_name")
            row = cursor.fetchone()
            db_name = row[0] if row else "unknown"
            cursor.close()
            self._connections[source.id] = conn
            result["status"] = "ok"
            result["message"] = f"{db_name} ready"
        except pyodbc.InterfaceError as exc:
            result["status"] = "auth_error"
            result["message"] = (
                f"Authentication failed — check credentials in mcp-sqlserver.toml ({exc})"
            )
        except pyodbc.Error as exc:
            result["status"] = "query_error"
            result["message"] = f"Connected but query failed — check database permissions ({exc})"

        return result

    # ------------------------------------------------------------------
    # Query execution (async wrappers)
    # ------------------------------------------------------------------

    async def execute_query(
        self,
        sql: str,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a SQL query and return results as dict.

        Returns:
            {
                "columns": [...],
                "rows": [{...}, ...],
                "row_count": int,
                "execution_time_ms": int
            }
        """
        sid = source_id or self._default_source_id
        return await self._run_sync(partial(self._execute_query_sync, sql, sid))

    def _execute_query_sync(self, sql: str, source_id: str) -> dict[str, Any]:
        """Synchronous query execution."""
        conn = self._ensure_connected(source_id)
        cursor = conn.cursor()
        start = time.perf_counter()
        try:
            cursor.execute(sql)

            # Check if there are results (SELECT, EXEC with result set)
            if cursor.description:
                columns = [col[0] for col in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                return {
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                    "execution_time_ms": elapsed_ms,
                }

            # No result set (INSERT, UPDATE, DELETE, DDL)
            affected = cursor.rowcount
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return {
                "columns": [],
                "rows": [],
                "row_count": affected,
                "execution_time_ms": elapsed_ms,
                "message": f"{affected} row(s) affected",
            }
        except pyodbc.Error as exc:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            raise ValueError(
                f"SQL Error [{exc.args[0] if exc.args else 'unknown'}]: "
                f"{exc.args[1] if len(exc.args) > 1 else str(exc)}"
            ) from exc
        finally:
            cursor.close()

    async def execute_query_raw(
        self,
        sql: str,
        source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute query, return just the rows as list of dicts."""
        result = await self.execute_query(sql, source_id)
        return result.get("rows", [])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _default_source_id(self) -> str:
        """Get default source ID."""
        default = self._config.default_source
        if not default:
            raise ValueError("No database sources configured")
        return default.id

    @property
    def source_ids(self) -> list[str]:
        """List all configured source IDs."""
        return [s.id for s in self._config.sources]

    async def _run_sync(self, func: partial[Any]) -> Any:
        """Run blocking pyodbc call in thread executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _handle_datetimeoffset(dto_value: bytes) -> datetime:
    """Convert SQL Server datetimeoffset(7) binary to Python datetime."""
    tup = struct.unpack("<6hI2h", dto_value)
    return datetime(
        tup[0],
        tup[1],
        tup[2],
        tup[3],
        tup[4],
        tup[5],
        tup[6] // 1000,
        timezone(timedelta(hours=tup[7], minutes=tup[8])),
    )


def _extract_host_port(dsn: str) -> tuple[str | None, int]:
    """Extract host and port from a DSN or connection string.

    Handles both formats:
      - ODBC: "...;Server=host,port;..."
      - URI:  "sqlserver://user:pass@host:port/db"
    """
    # Try ODBC format: Server=host,port or Server=host
    dsn_lower = dsn.lower()
    for prefix in ("server=", "data source="):
        idx = dsn_lower.find(prefix)
        if idx >= 0:
            start = idx + len(prefix)
            end = dsn.find(";", start)
            server_part = dsn[start:end] if end > 0 else dsn[start:]
            server_part = server_part.strip()
            if "," in server_part:
                host, port_str = server_part.rsplit(",", 1)
                return host.strip(), int(port_str.strip())
            return server_part, 1433

    # Try URI format: sqlserver://user:pass@host:port/db
    if "://" in dsn:
        from urllib.parse import urlparse

        parsed = urlparse(dsn)
        if parsed.hostname:
            return parsed.hostname, parsed.port or 1433

    return None, 1433
