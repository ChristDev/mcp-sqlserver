"""MCP Tools — execute_sql (single tool for maximum token efficiency).

This is the ONLY tool in the server. Schema exploration is handled by Resources.
Tool schemas are sent to the LLM on every turn — keeping it to 1 tool minimizes
token overhead (~400 tokens/turn vs ~1,400 for 2 tools).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from fastmcp import Context, FastMCP

from mcp_sqlserver.config import AppConfig
from mcp_sqlserver.connection import ConnectionManager
from mcp_sqlserver.guardrails import apply_row_limit, validate_readonly

logger = logging.getLogger(__name__)


def register_tools(mcp: FastMCP) -> None:
    """Register all tools on the FastMCP server."""

    @mcp.tool
    async def execute_sql(query: str, database: str = "", ctx: Context = None) -> str:
        """Execute SQL query. Supports SELECT, INSERT, UPDATE, DELETE, EXEC, DDL.

        Args:
            query: SQL statement to execute. Multiple statements separated by ;
            database: Source ID for multi-connection (optional, uses default if empty)
        """
        conn_manager: ConnectionManager = ctx.lifespan_context["conn_manager"]
        config: AppConfig = ctx.lifespan_context["config"]

        # Resolve source
        source_id = database or conn_manager._default_source_id
        guardrail = config.get_guardrail(source_id)

        # Apply guardrails
        validate_readonly(query, guardrail)
        safe_query = apply_row_limit(query, guardrail)

        # Execute
        try:
            result = await conn_manager.execute_query(safe_query, source_id)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Query execution failed: {exc}") from exc

        # Format response — compact JSON for token efficiency
        return _format_result(result)


def _format_result(result: dict[str, Any]) -> str:
    """Format query result as compact, token-efficient text."""
    rows = result.get("rows", [])
    columns = result.get("columns", [])
    row_count = result.get("row_count", 0)
    elapsed = result.get("execution_time_ms", 0)
    message = result.get("message", "")

    # Non-SELECT (INSERT/UPDATE/DELETE/DDL)
    if not columns:
        return message or f"{row_count} row(s) affected ({elapsed}ms)"

    # Empty result set
    if not rows:
        return f"0 rows returned ({elapsed}ms)"

    # Format as JSON array — compact but readable
    serializable_rows = [_serialize_row(row) for row in rows]
    output = json.dumps(serializable_rows, ensure_ascii=False, default=str)

    # Add metadata footer
    footer = f"\n\n-- {row_count} row(s) ({elapsed}ms)"
    if row_count >= 1000:
        footer += " [row limit applied]"

    return output + footer


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert non-JSON-serializable types to strings."""
    result = {}
    for key, value in row.items():
        if isinstance(value, (datetime, date, time)):
            result[key] = value.isoformat()
        elif isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, bytes):
            result[key] = f"<binary {len(value)} bytes>"
        else:
            result[key] = value
    return result
