"""Guardrails — read-only check, row limiting, timeout enforcement.

Applied before query execution to enforce per-source safety rules.
"""

from __future__ import annotations

import re

from mcp_sqlserver.config import GuardrailConfig

# SQL keywords allowed in read-only mode (SQL Server)
READONLY_KEYWORDS = frozenset(["select", "with", "explain", "showplan", "set"])


def validate_readonly(sql: str, guardrail: GuardrailConfig) -> None:
    """Check if SQL is allowed under read-only mode.

    Raises:
        ValueError: If query is not read-only and readonly mode is enabled.
    """
    if not guardrail.readonly:
        return

    stripped = _strip_comments(sql).strip().lower()
    if not stripped:
        raise ValueError("Empty query")

    first_keyword = stripped.split()[0] if stripped.split() else ""

    if first_keyword not in READONLY_KEYWORDS:
        raise ValueError(
            f"Read-only mode is enabled for source '{guardrail.source}'. "
            f"Only these SQL operations are allowed: {', '.join(sorted(READONLY_KEYWORDS))}. "
            f"Got: {first_keyword.upper()}"
        )


def apply_row_limit(sql: str, guardrail: GuardrailConfig) -> str:
    """Inject TOP N into SELECT queries if max_rows is configured.

    Only modifies SELECT statements. Does not touch INSERT, UPDATE, DELETE, EXEC.

    Examples:
        SELECT * FROM t  →  SELECT TOP 1000 * FROM t
        SELECT DISTINCT name FROM t  →  SELECT DISTINCT TOP 1000 name FROM t
        SELECT TOP 10 * FROM t  →  SELECT TOP 10 * FROM t  (unchanged, user limit is lower)
    """
    if guardrail.max_rows <= 0:
        return sql

    stripped = _strip_comments(sql).strip()
    stripped_lower = stripped.lower()

    # Only apply to SELECT statements
    if not stripped_lower.startswith(("select ", "select\t", "select\n")):
        return sql

    # Check if TOP already exists
    existing_top = _extract_existing_top(stripped_lower)
    if existing_top is not None:
        # User already has TOP — only override if their limit is higher
        if existing_top <= guardrail.max_rows:
            return sql
        # Replace with our limit
        return _replace_top(sql, guardrail.max_rows)

    # Inject TOP after SELECT [DISTINCT|ALL]
    return _inject_top(sql, guardrail.max_rows)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TOP_PATTERN = re.compile(
    r"(SELECT\s+(?:DISTINCT\s+|ALL\s+)?)(TOP\s+\d+\s*(?:\s+PERCENT\s*)?)",
    re.IGNORECASE,
)

_SELECT_PATTERN = re.compile(
    r"(SELECT\s+(?:DISTINCT\s+|ALL\s+)?)",
    re.IGNORECASE,
)


def _extract_existing_top(sql_lower: str) -> int | None:
    """Extract the numeric value of an existing TOP clause, or None."""
    match = re.search(r"\bselect\s+(?:distinct\s+|all\s+)?top\s+(\d+)", sql_lower)
    if match:
        return int(match.group(1))
    return None


def _replace_top(sql: str, max_rows: int) -> str:
    """Replace existing TOP N with new limit."""
    return _TOP_PATTERN.sub(rf"\1TOP {max_rows} ", sql, count=1)


def _inject_top(sql: str, max_rows: int) -> str:
    """Inject TOP N after SELECT [DISTINCT|ALL]."""
    match = _SELECT_PATTERN.match(sql)
    if match:
        prefix = match.group(1)
        rest = sql[match.end() :]
        return f"{prefix}TOP {max_rows} {rest}"
    return sql


def _strip_comments(sql: str) -> str:
    """Remove SQL comments (-- line comments and /* block comments */)."""
    # Remove block comments
    result = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # Remove line comments
    result = re.sub(r"--[^\n]*", " ", result)
    return result
