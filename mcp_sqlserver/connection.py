"""Connection manager — one bounded pool per source, plus health reporting.

The manager owns a :class:`~mcp_sqlserver.pool.SourcePool` per configured
source and routes every query to the right one. It never hands a connection to
callers, so no two requests can share a pyodbc handle.
"""

from __future__ import annotations

import logging
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

import anyio
import anyio.to_thread
import pyodbc

from mcp_sqlserver.config import AppConfig, SourceConfig
from mcp_sqlserver.dbtypes import (
    ConnectionFactory,
    DbConnection,
    PoolSpec,
    QueryRequest,
    QueryResult,
    Row,
    SqlParams,
)
from mcp_sqlserver.errors import DatabaseError, SourceUnavailableError, UnknownSourceError
from mcp_sqlserver.pool import SourcePool

logger = logging.getLogger(__name__)

NETWORK_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_SQL_PORT = 1433
DATETIMEOFFSET_ODBC_TYPE = -155
AUTH_SQLSTATE_CLASS = "28"
THREAD_HEADROOM = 4


class HealthState(StrEnum):
    """Outcome of probing one source."""

    OK = "ok"
    DEFERRED = "deferred"
    NETWORK_ERROR = "network_error"
    AUTH_ERROR = "auth_error"
    QUERY_ERROR = "query_error"


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """What one source reported when probed."""

    source_id: str
    state: HealthState
    message: str

    @property
    def healthy(self) -> bool:
        """Whether this source can serve queries right now."""
        return self.state in {HealthState.OK, HealthState.DEFERRED}


class ConnectionManager:
    """Routes queries to per-source pools and reports their health."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._pools = {
            source.id: SourcePool(_spec_for(config, source), _factory_for(source))
            for source in config.sources
        }

    @property
    def default_source_id(self) -> str:
        """Id of the source used when a caller does not name one."""
        default = self._config.default_source
        if default is None:
            raise UnknownSourceError(source_id="", known_sources=())
        return default.id

    @property
    def source_ids(self) -> tuple[str, ...]:
        """Every configured source id, in declaration order."""
        return tuple(self._pools)

    async def start(self) -> tuple[HealthStatus, ...]:
        """Size the worker thread pool, then probe every non-lazy source."""
        _widen_thread_limiter(sum(pool.spec.size for pool in self._pools.values()))
        return await self.health_check()

    async def health_check(self) -> tuple[HealthStatus, ...]:
        """Probe every source in parallel; one failure never blocks the others."""
        collected: dict[str, HealthStatus] = {}

        async def probe(source: SourceConfig) -> None:
            collected[source.id] = await self._probe(source)

        async with anyio.create_task_group() as tg:
            for source in self._config.sources:
                tg.start_soon(probe, source)

        statuses = tuple(collected[source.id] for source in self._config.sources)
        for status in statuses:
            _log_status(status)
        return statuses

    async def aclose(self) -> None:
        """Drain and close every pool."""
        async with anyio.create_task_group() as tg:
            for pool in self._pools.values():
                tg.start_soon(pool.aclose)
        logger.info("All database connections closed")

    async def execute_query(
        self,
        sql: str,
        params: SqlParams = (),
        *,
        source_id: str | None = None,
    ) -> QueryResult:
        """Run one statement on the named source and return the full result."""
        pool = self._pool_for(source_id)
        return await pool.execute(QueryRequest(sql=sql, params=params))

    async def execute_query_raw(
        self,
        sql: str,
        params: SqlParams = (),
        *,
        source_id: str | None = None,
    ) -> list[Row]:
        """Run one statement and return just its rows."""
        result = await self.execute_query(sql, params, source_id=source_id)
        return list(result.rows)

    def _pool_for(self, source_id: str | None) -> SourcePool:
        resolved = source_id or self.default_source_id
        pool = self._pools.get(resolved)
        if pool is None:
            raise UnknownSourceError(source_id=resolved, known_sources=self.source_ids)
        return pool

    async def _probe(self, source: SourceConfig) -> HealthStatus:
        if source.lazy:
            return HealthStatus(
                source_id=source.id,
                state=HealthState.DEFERRED,
                message="lazy — connects on first use",
            )

        host, port = _extract_host_port(source.dsn)
        if host is not None and not await _reachable(host, port):
            return HealthStatus(
                source_id=source.id,
                state=HealthState.NETWORK_ERROR,
                message=f"Cannot reach {host}:{port} — VPN connected?",
            )

        try:
            result = await self._pools[source.id].execute(
                QueryRequest(sql="SELECT DB_NAME() AS db_name")
            )
        except SourceUnavailableError as exc:
            return HealthStatus(
                source_id=source.id,
                state=_state_for_sqlstate(exc.sqlstate),
                message=str(exc),
            )
        except DatabaseError as exc:
            return HealthStatus(
                source_id=source.id,
                state=HealthState.QUERY_ERROR,
                message=str(exc),
            )

        database = str(result.rows[0]["db_name"]) if result.rows else source.id
        return HealthStatus(
            source_id=source.id,
            state=HealthState.OK,
            message=f"{database} ready",
        )


def _spec_for(config: AppConfig, source: SourceConfig) -> PoolSpec:
    """Merge source and guardrail settings into the pool's contract."""
    guardrail = config.get_guardrail(source.id)
    return PoolSpec(
        source_id=source.id,
        size=config.pool_size_for(source),
        admission_timeout=source.pool_timeout,
        query_timeout=guardrail.query_timeout,
        max_rows=guardrail.max_rows,
    )


def _factory_for(source: SourceConfig) -> ConnectionFactory:
    """Build the callable a pool uses to open one connection to this source."""

    def connect() -> DbConnection:
        connection = pyodbc.connect(
            source.dsn,
            autocommit=True,
            timeout=source.connect_timeout,
        )
        connection.add_output_converter(DATETIMEOFFSET_ODBC_TYPE, _handle_datetimeoffset)
        return connection

    return connect


def _widen_thread_limiter(required: int) -> None:
    """Ensure anyio can run every pooled query at once instead of queueing them."""
    limiter = anyio.to_thread.current_default_thread_limiter()
    wanted = required + THREAD_HEADROOM
    if limiter.total_tokens < wanted:
        limiter.total_tokens = wanted


async def _reachable(host: str, port: int) -> bool:
    """Whether a TCP connection to the database host can be opened."""
    return await anyio.to_thread.run_sync(_reachable_blocking, host, port)


def _reachable_blocking(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=NETWORK_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _state_for_sqlstate(sqlstate: str | None) -> HealthState:
    """Tell a rejected login apart from an unreachable server."""
    if sqlstate is not None and sqlstate.startswith(AUTH_SQLSTATE_CLASS):
        return HealthState.AUTH_ERROR
    return HealthState.NETWORK_ERROR


def _log_status(status: HealthStatus) -> None:
    if status.state is HealthState.OK:
        logger.info("[%s] OK — %s", status.source_id, status.message)
        return
    if status.state is HealthState.DEFERRED:
        logger.info("[%s] %s", status.source_id, status.message)
        return
    logger.error("[%s] %s — %s", status.source_id, status.state.upper(), status.message)


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
            return server_part, DEFAULT_SQL_PORT

    if "://" in dsn:
        from urllib.parse import urlparse

        parsed = urlparse(dsn)
        if parsed.hostname:
            return parsed.hostname, parsed.port or DEFAULT_SQL_PORT

    return None, DEFAULT_SQL_PORT
