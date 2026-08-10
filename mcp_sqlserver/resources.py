"""MCP Resources — schema exploration via URI templates.

Resources are fetched on-demand by the LLM. Unlike tools, resource definitions
do NOT consume tokens on every turn — only when the LLM reads them.

URI scheme: db://{schema}/tables, db://{schema}/{table}, etc.

Every statement here is parameterised (see :mod:`mcp_sqlserver.queries`), so a
schema or object name supplied by a client is data to SQL Server, never text
spliced into a query.
"""

from __future__ import annotations

import json
import logging

from fastmcp import Context, FastMCP

from mcp_sqlserver import queries
from mcp_sqlserver.connection import ConnectionManager
from mcp_sqlserver.dbtypes import Row

logger = logging.getLogger(__name__)

NOT_AVAILABLE = "[Encrypted or not found]"


def register_resources(mcp: FastMCP) -> None:
    """Register all resources on the FastMCP server."""

    @mcp.resource("db://connections")
    async def list_connections(ctx: Context) -> str:
        """List all configured database connections."""
        from mcp_sqlserver.config import AppConfig

        config: AppConfig = ctx.lifespan_context["config"]
        connections = []
        for source in config.sources:
            guardrail = config.get_guardrail(source.id)
            connections.append(
                {
                    "id": source.id,
                    "description": source.description,
                    "readonly": guardrail.readonly,
                    "max_rows": guardrail.max_rows,
                    "query_timeout": guardrail.query_timeout,
                    "pool_size": config.pool_size_for(source),
                }
            )
        return json.dumps(connections, indent=2)

    @mcp.resource("db://schemas")
    async def list_schemas(ctx: Context) -> str:
        """List all database schemas with table and procedure counts."""
        return await _rows(ctx, queries.SCHEMAS)

    @mcp.resource("db://{schema}/tables")
    async def list_tables(schema: str, ctx: Context) -> str:
        """List all tables in a schema with row counts."""
        return await _rows(ctx, queries.TABLES, (schema,))

    @mcp.resource("db://{schema}/{table}")
    async def get_table_schema(schema: str, table: str, ctx: Context) -> str:
        """Get complete table structure: columns, types, PKs, FKs, indexes."""
        manager = _manager(ctx)
        columns = await manager.execute_query_raw(
            queries.TABLE_COLUMNS,
            (schema, table, schema, table, schema, table),
        )
        foreign_keys = await manager.execute_query_raw(queries.TABLE_FOREIGN_KEYS, (schema, table))
        indexes = await manager.execute_query_raw(queries.TABLE_INDEXES, (schema, table))
        return _dump(
            {
                "schema": schema,
                "table": table,
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
            }
        )

    @mcp.resource("db://{schema}/{table}/indexes")
    async def get_table_indexes(schema: str, table: str, ctx: Context) -> str:
        """Get indexes for a table with key and included columns."""
        return await _rows(ctx, queries.TABLE_INDEX_DETAIL, (schema, table))

    @mcp.resource("db://{schema}/procedures")
    async def list_procedures(schema: str, ctx: Context) -> str:
        """List stored procedures in a schema with parameter counts."""
        return await _rows(ctx, queries.PROCEDURES, (schema,))

    @mcp.resource("db://{schema}/procedures/{name}")
    async def get_procedure_definition(schema: str, name: str, ctx: Context) -> str:
        """Get stored procedure source code and parameters."""
        manager = _manager(ctx)
        parameters = await manager.execute_query_raw(queries.PROCEDURE_PARAMETERS, (schema, name))
        return _dump(
            {
                "schema": schema,
                "procedure": name,
                "parameters": parameters,
                "definition": await _source(ctx, schema, name),
            }
        )

    @mcp.resource("db://{schema}/functions")
    async def list_functions(schema: str, ctx: Context) -> str:
        """List user-defined functions in a schema."""
        return await _rows(ctx, queries.FUNCTIONS, (schema,))

    @mcp.resource("db://{schema}/functions/{name}")
    async def get_function_definition(schema: str, name: str, ctx: Context) -> str:
        """Get function source code."""
        return await _source(ctx, schema, name)

    @mcp.resource("db://{schema}/views")
    async def list_views(schema: str, ctx: Context) -> str:
        """List views in a schema."""
        return await _rows(ctx, queries.VIEWS, (schema,))

    @mcp.resource("db://{schema}/views/{name}")
    async def get_view_definition(schema: str, name: str, ctx: Context) -> str:
        """Get view source code."""
        return await _source(ctx, schema, name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manager(ctx: Context) -> ConnectionManager:
    """Get ConnectionManager from lifespan context."""
    return ctx.lifespan_context["conn_manager"]


def _dump(payload: object) -> str:
    """Render a resource body as indented JSON."""
    return json.dumps(payload, indent=2, default=str)


async def _rows(ctx: Context, sql: str, params: tuple[str, ...] = ()) -> str:
    """Run a catalog query and render its rows."""
    rows: list[Row] = await _manager(ctx).execute_query_raw(sql, params)
    return _dump(rows)


async def _source(ctx: Context, schema: str, name: str) -> str:
    """Fetch the source text of a schema-scoped object."""
    rows = await _manager(ctx).execute_query_raw(queries.OBJECT_SOURCE, (schema, name))
    if not rows:
        return NOT_AVAILABLE
    definition = rows[0]["definition"]
    return str(definition) if definition else NOT_AVAILABLE
