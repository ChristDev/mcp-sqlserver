"""MCP Resources — schema exploration via URI templates.

Resources are fetched on-demand by the LLM. Unlike tools, resource definitions
do NOT consume tokens on every turn — only when the LLM reads them.

URI scheme: db://{schema}/tables, db://{schema}/{table}, etc.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastmcp import Context, FastMCP

from mcp_sqlserver.connection import ConnectionManager

logger = logging.getLogger(__name__)


def register_resources(mcp: FastMCP) -> None:
    """Register all resources on the FastMCP server."""

    # ------------------------------------------------------------------
    # db://connections — list configured sources
    # ------------------------------------------------------------------

    @mcp.resource("db://connections")
    async def list_connections(ctx: Context) -> str:
        """List all configured database connections."""
        from mcp_sqlserver.config import AppConfig

        config: AppConfig = ctx.lifespan_context["config"]
        connections = []
        for s in config.sources:
            g = config.get_guardrail(s.id)
            connections.append(
                {
                    "id": s.id,
                    "description": s.description,
                    "readonly": g.readonly,
                    "max_rows": g.max_rows,
                    "query_timeout": g.query_timeout,
                }
            )
        return json.dumps(connections, indent=2)

    # ------------------------------------------------------------------
    # db://schemas — list all schemas
    # ------------------------------------------------------------------

    @mcp.resource("db://schemas")
    async def list_schemas(ctx: Context) -> str:
        """List all database schemas with table and procedure counts."""
        conn = _get_conn(ctx)
        rows = await conn.execute_query_raw("""
            SELECT
                s.name AS schema_name,
                COUNT(DISTINCT t.object_id) AS table_count,
                COUNT(DISTINCT p.object_id) AS procedure_count
            FROM sys.schemas s
            LEFT JOIN sys.tables t ON t.schema_id = s.schema_id
            LEFT JOIN sys.procedures p ON p.schema_id = s.schema_id
            WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')
            GROUP BY s.name
            HAVING COUNT(DISTINCT t.object_id) > 0 OR COUNT(DISTINCT p.object_id) > 0
            ORDER BY s.name
        """)
        return json.dumps(rows, indent=2, default=str)

    # ------------------------------------------------------------------
    # db://{schema}/tables — list tables in schema
    # ------------------------------------------------------------------

    @mcp.resource("db://{schema}/tables")
    async def list_tables(schema: str, ctx: Context) -> str:
        """List all tables in a schema with row counts."""
        conn = _get_conn(ctx)
        rows = await conn.execute_query_raw(f"""
            SELECT
                t.name AS table_name,
                p.rows AS row_count,
                t.create_date,
                t.modify_date
            FROM sys.tables t
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
            WHERE s.name = '{_safe(schema)}'
            ORDER BY t.name
        """)
        return json.dumps(rows, indent=2, default=str)

    # ------------------------------------------------------------------
    # db://{schema}/{table} — full table structure
    # ------------------------------------------------------------------

    @mcp.resource("db://{schema}/{table}")
    async def get_table_schema(schema: str, table: str, ctx: Context) -> str:
        """Get complete table structure: columns, types, PKs, FKs, indexes, constraints."""
        conn = _get_conn(ctx)
        s_schema = _safe(schema)
        s_table = _safe(table)

        # Columns
        columns = await conn.execute_query_raw(f"""
            SELECT
                c.COLUMN_NAME AS name,
                c.DATA_TYPE AS type,
                c.CHARACTER_MAXIMUM_LENGTH AS max_length,
                c.NUMERIC_PRECISION AS precision,
                c.NUMERIC_SCALE AS scale,
                c.IS_NULLABLE AS nullable,
                c.COLUMN_DEFAULT AS default_value,
                CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'YES' ELSE 'NO' END AS is_pk,
                COLUMNPROPERTY(OBJECT_ID('{s_schema}.{s_table}'), c.COLUMN_NAME, 'IsIdentity') AS is_identity
            FROM INFORMATION_SCHEMA.COLUMNS c
            LEFT JOIN (
                SELECT ku.COLUMN_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
                    ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
                WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                    AND tc.TABLE_SCHEMA = '{s_schema}' AND tc.TABLE_NAME = '{s_table}'
            ) pk ON pk.COLUMN_NAME = c.COLUMN_NAME
            WHERE c.TABLE_SCHEMA = '{s_schema}' AND c.TABLE_NAME = '{s_table}'
            ORDER BY c.ORDINAL_POSITION
        """)

        # Foreign keys
        fks = await conn.execute_query_raw(f"""
            SELECT
                fk.name AS fk_name,
                COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS column_name,
                OBJECT_SCHEMA_NAME(fkc.referenced_object_id) AS ref_schema,
                OBJECT_NAME(fkc.referenced_object_id) AS ref_table,
                COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS ref_column
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
            WHERE fk.parent_object_id = OBJECT_ID('{s_schema}.{s_table}')
        """)

        # Indexes
        indexes = await conn.execute_query_raw(f"""
            SELECT
                i.name AS index_name,
                STRING_AGG(c.name, ', ') WITHIN GROUP (ORDER BY ic.key_ordinal) AS columns,
                i.is_unique,
                i.type_desc AS index_type
            FROM sys.indexes i
            JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            WHERE i.object_id = OBJECT_ID('{s_schema}.{s_table}')
                AND i.name IS NOT NULL
            GROUP BY i.name, i.is_unique, i.type_desc
        """)

        result = {
            "schema": schema,
            "table": table,
            "columns": columns,
            "foreign_keys": fks,
            "indexes": indexes,
        }
        return json.dumps(result, indent=2, default=str)

    # ------------------------------------------------------------------
    # db://{schema}/{table}/indexes — table indexes
    # ------------------------------------------------------------------

    @mcp.resource("db://{schema}/{table}/indexes")
    async def get_table_indexes(schema: str, table: str, ctx: Context) -> str:
        """Get indexes for a table with columns and type info."""
        conn = _get_conn(ctx)
        rows = await conn.execute_query_raw(f"""
            SELECT
                i.name AS index_name,
                STRING_AGG(c.name, ', ') WITHIN GROUP (ORDER BY ic.key_ordinal) AS columns,
                i.is_unique,
                i.is_primary_key,
                i.type_desc AS index_type,
                STUFF((
                    SELECT ', ' + c2.name
                    FROM sys.index_columns ic2
                    JOIN sys.columns c2 ON ic2.object_id = c2.object_id AND ic2.column_id = c2.column_id
                    WHERE ic2.object_id = i.object_id AND ic2.index_id = i.index_id AND ic2.is_included_column = 1
                    FOR XML PATH('')
                ), 1, 2, '') AS included_columns
            FROM sys.indexes i
            JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id AND ic.is_included_column = 0
            JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            WHERE i.object_id = OBJECT_ID('{_safe(schema)}.{_safe(table)}')
                AND i.name IS NOT NULL
            GROUP BY i.name, i.is_unique, i.is_primary_key, i.type_desc, i.object_id, i.index_id
        """)
        return json.dumps(rows, indent=2, default=str)

    # ------------------------------------------------------------------
    # db://{schema}/procedures — list stored procedures
    # ------------------------------------------------------------------

    @mcp.resource("db://{schema}/procedures")
    async def list_procedures(schema: str, ctx: Context) -> str:
        """List stored procedures in a schema with parameter counts."""
        conn = _get_conn(ctx)
        rows = await conn.execute_query_raw(f"""
            SELECT
                p.name AS procedure_name,
                COUNT(pa.parameter_id) AS parameter_count,
                p.create_date,
                p.modify_date
            FROM sys.procedures p
            JOIN sys.schemas s ON p.schema_id = s.schema_id
            LEFT JOIN sys.parameters pa ON p.object_id = pa.object_id AND pa.parameter_id > 0
            WHERE s.name = '{_safe(schema)}'
            GROUP BY p.name, p.create_date, p.modify_date
            ORDER BY p.name
        """)
        return json.dumps(rows, indent=2, default=str)

    # ------------------------------------------------------------------
    # db://{schema}/procedures/{name} — SP source code
    # ------------------------------------------------------------------

    @mcp.resource("db://{schema}/procedures/{name}")
    async def get_procedure_definition(schema: str, name: str, ctx: Context) -> str:
        """Get stored procedure source code and parameters."""
        conn = _get_conn(ctx)
        s_schema = _safe(schema)
        s_name = _safe(name)

        # Parameters
        params = await conn.execute_query_raw(f"""
            SELECT
                pa.name AS parameter_name,
                TYPE_NAME(pa.user_type_id) AS data_type,
                pa.max_length,
                pa.is_output,
                pa.has_default_value,
                pa.default_value
            FROM sys.parameters pa
            JOIN sys.procedures p ON pa.object_id = p.object_id
            JOIN sys.schemas s ON p.schema_id = s.schema_id
            WHERE s.name = '{s_schema}' AND p.name = '{s_name}' AND pa.parameter_id > 0
            ORDER BY pa.parameter_id
        """)

        # Source code
        definition_rows = await conn.execute_query_raw(f"""
            SELECT OBJECT_DEFINITION(OBJECT_ID('{s_schema}.{s_name}')) AS definition
        """)
        definition = (
            definition_rows[0]["definition"]
            if definition_rows and definition_rows[0]["definition"]
            else "[Encrypted or not found]"
        )

        result = {
            "schema": schema,
            "procedure": name,
            "parameters": params,
            "definition": definition,
        }
        return json.dumps(result, indent=2, default=str)

    # ------------------------------------------------------------------
    # db://{schema}/functions — list functions
    # ------------------------------------------------------------------

    @mcp.resource("db://{schema}/functions")
    async def list_functions(schema: str, ctx: Context) -> str:
        """List user-defined functions in a schema."""
        conn = _get_conn(ctx)
        rows = await conn.execute_query_raw(f"""
            SELECT
                o.name AS function_name,
                o.type_desc AS function_type,
                TYPE_NAME(ISNULL(
                    (SELECT TOP 1 pa.user_type_id FROM sys.parameters pa WHERE pa.object_id = o.object_id AND pa.parameter_id = 0),
                    0
                )) AS return_type,
                o.create_date,
                o.modify_date
            FROM sys.objects o
            JOIN sys.schemas s ON o.schema_id = s.schema_id
            WHERE s.name = '{_safe(schema)}'
                AND o.type IN ('FN', 'IF', 'TF', 'AF')
            ORDER BY o.name
        """)
        return json.dumps(rows, indent=2, default=str)

    # ------------------------------------------------------------------
    # db://{schema}/functions/{name} — function source code
    # ------------------------------------------------------------------

    @mcp.resource("db://{schema}/functions/{name}")
    async def get_function_definition(schema: str, name: str, ctx: Context) -> str:
        """Get function source code."""
        conn = _get_conn(ctx)
        rows = await conn.execute_query_raw(f"""
            SELECT OBJECT_DEFINITION(OBJECT_ID('{_safe(schema)}.{_safe(name)}')) AS definition
        """)
        definition = (
            rows[0]["definition"] if rows and rows[0]["definition"] else "[Encrypted or not found]"
        )
        return definition

    # ------------------------------------------------------------------
    # db://{schema}/views — list views
    # ------------------------------------------------------------------

    @mcp.resource("db://{schema}/views")
    async def list_views(schema: str, ctx: Context) -> str:
        """List views in a schema."""
        conn = _get_conn(ctx)
        rows = await conn.execute_query_raw(f"""
            SELECT
                v.name AS view_name,
                v.create_date,
                v.modify_date
            FROM sys.views v
            JOIN sys.schemas s ON v.schema_id = s.schema_id
            WHERE s.name = '{_safe(schema)}'
            ORDER BY v.name
        """)
        return json.dumps(rows, indent=2, default=str)

    # ------------------------------------------------------------------
    # db://{schema}/views/{name} — view definition
    # ------------------------------------------------------------------

    @mcp.resource("db://{schema}/views/{name}")
    async def get_view_definition(schema: str, name: str, ctx: Context) -> str:
        """Get view source code."""
        conn = _get_conn(ctx)
        rows = await conn.execute_query_raw(f"""
            SELECT OBJECT_DEFINITION(OBJECT_ID('{_safe(schema)}.{_safe(name)}')) AS definition
        """)
        definition = (
            rows[0]["definition"] if rows and rows[0]["definition"] else "[Encrypted or not found]"
        )
        return definition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_conn(ctx: Context) -> ConnectionManager:
    """Get ConnectionManager from lifespan context."""
    return ctx.lifespan_context["conn_manager"]


def _safe(value: str) -> str:
    """Basic SQL injection prevention for identifiers.

    Removes characters that could break out of single-quoted strings.
    This is used for schema/table/procedure names in metadata queries.
    """
    return value.replace("'", "").replace(";", "").replace("--", "").replace("/*", "")
