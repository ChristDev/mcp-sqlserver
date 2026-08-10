"""Catalog SQL used by the db:// resources.

Every statement is fully parameterised: schema, table and routine names arrive
as bound parameters, never as interpolated text. Object names are resolved with
``QUOTENAME`` so that identifiers containing dots, brackets or quotes cannot
change which object is addressed.
"""

from __future__ import annotations

from typing import Final

#: Resolves "schema.object" from two bound identifiers.
_OBJECT_ID: Final = "OBJECT_ID(QUOTENAME(?) + '.' + QUOTENAME(?))"

SCHEMAS: Final = """
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
"""

TABLES: Final = """
    SELECT
        t.name AS table_name,
        p.rows AS row_count,
        t.create_date,
        t.modify_date
    FROM sys.tables t
    INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
    INNER JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
    WHERE s.name = ?
    ORDER BY t.name
"""

#: Parameters: schema, table, schema, table, schema, table
TABLE_COLUMNS: Final = f"""
    SELECT
        c.COLUMN_NAME AS name,
        c.DATA_TYPE AS type,
        c.CHARACTER_MAXIMUM_LENGTH AS max_length,
        c.NUMERIC_PRECISION AS precision,
        c.NUMERIC_SCALE AS scale,
        c.IS_NULLABLE AS nullable,
        c.COLUMN_DEFAULT AS default_value,
        CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'YES' ELSE 'NO' END AS is_pk,
        COLUMNPROPERTY({_OBJECT_ID}, c.COLUMN_NAME, 'IsIdentity') AS is_identity
    FROM INFORMATION_SCHEMA.COLUMNS c
    LEFT JOIN (
        SELECT ku.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
            ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
            AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
    ) pk ON pk.COLUMN_NAME = c.COLUMN_NAME
    WHERE c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ?
    ORDER BY c.ORDINAL_POSITION
"""

#: Parameters: schema, table
TABLE_FOREIGN_KEYS: Final = f"""
    SELECT
        fk.name AS fk_name,
        COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS column_name,
        OBJECT_SCHEMA_NAME(fkc.referenced_object_id) AS ref_schema,
        OBJECT_NAME(fkc.referenced_object_id) AS ref_table,
        COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS ref_column
    FROM sys.foreign_keys fk
    JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
    WHERE fk.parent_object_id = {_OBJECT_ID}
"""

#: Parameters: schema, table
TABLE_INDEXES: Final = f"""
    SELECT
        i.name AS index_name,
        STRING_AGG(c.name, ', ') WITHIN GROUP (ORDER BY ic.key_ordinal) AS columns,
        i.is_unique,
        i.type_desc AS index_type
    FROM sys.indexes i
    JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
    JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
    WHERE i.object_id = {_OBJECT_ID}
        AND i.name IS NOT NULL
    GROUP BY i.name, i.is_unique, i.type_desc
"""

#: Parameters: schema, table
TABLE_INDEX_DETAIL: Final = f"""
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
            WHERE ic2.object_id = i.object_id AND ic2.index_id = i.index_id
                AND ic2.is_included_column = 1
            FOR XML PATH('')
        ), 1, 2, '') AS included_columns
    FROM sys.indexes i
    JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
        AND ic.is_included_column = 0
    JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
    WHERE i.object_id = {_OBJECT_ID}
        AND i.name IS NOT NULL
    GROUP BY i.name, i.is_unique, i.is_primary_key, i.type_desc, i.object_id, i.index_id
"""

PROCEDURES: Final = """
    SELECT
        p.name AS procedure_name,
        COUNT(pa.parameter_id) AS parameter_count,
        p.create_date,
        p.modify_date
    FROM sys.procedures p
    JOIN sys.schemas s ON p.schema_id = s.schema_id
    LEFT JOIN sys.parameters pa ON p.object_id = pa.object_id AND pa.parameter_id > 0
    WHERE s.name = ?
    GROUP BY p.name, p.create_date, p.modify_date
    ORDER BY p.name
"""

#: Parameters: schema, procedure
PROCEDURE_PARAMETERS: Final = """
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
    WHERE s.name = ? AND p.name = ? AND pa.parameter_id > 0
    ORDER BY pa.parameter_id
"""

FUNCTIONS: Final = """
    SELECT
        o.name AS function_name,
        o.type_desc AS function_type,
        TYPE_NAME(ISNULL(
            (SELECT TOP 1 pa.user_type_id FROM sys.parameters pa
             WHERE pa.object_id = o.object_id AND pa.parameter_id = 0),
            0
        )) AS return_type,
        o.create_date,
        o.modify_date
    FROM sys.objects o
    JOIN sys.schemas s ON o.schema_id = s.schema_id
    WHERE s.name = ?
        AND o.type IN ('FN', 'IF', 'TF', 'AF')
    ORDER BY o.name
"""

VIEWS: Final = """
    SELECT
        v.name AS view_name,
        v.create_date,
        v.modify_date
    FROM sys.views v
    JOIN sys.schemas s ON v.schema_id = s.schema_id
    WHERE s.name = ?
    ORDER BY v.name
"""

#: Source text of any schema-scoped object. Parameters: schema, object name.
OBJECT_SOURCE: Final = f"SELECT OBJECT_DEFINITION({_OBJECT_ID}) AS definition"
