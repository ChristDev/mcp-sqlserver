"""Guardrails — read-only check, row limiting, timeout enforcement.

Applied before query execution to enforce per-source safety rules.
"""

from __future__ import annotations

import re

from mcp_sqlserver.config import GuardrailConfig
from mcp_sqlserver.errors import ReadOnlyViolationError

# SQL keywords a read-only statement may start with (SQL Server)
READONLY_KEYWORDS = frozenset(["select", "with", "explain", "showplan", "set"])

# Verbs that mutate data, schema or server state. Checking only the first
# keyword is not enough: `WITH x AS (...) DELETE FROM t` starts with WITH.
WRITE_KEYWORDS = frozenset(
    [
        "insert",
        "update",
        "delete",
        "merge",
        "drop",
        "alter",
        "create",
        "truncate",
        "exec",
        "execute",
        "grant",
        "revoke",
        "deny",
        "backup",
        "restore",
        "shutdown",
        "reconfigure",
        "openrowset",
        "openquery",
    ]
)

# Statements that start here may not smuggle a `SELECT ... INTO new_table`.
_PROJECTION_KEYWORDS = frozenset(["select", "with"])


def validate_readonly(sql: str, guardrail: GuardrailConfig) -> None:
    """Check if SQL is allowed under read-only mode.

    Fails closed: the statement must begin with a read verb, contain no write
    verb anywhere outside string literals, be a single statement, and not
    materialise a new table with SELECT ... INTO.

    Raises:
        ReadOnlyViolationError: If the source is read-only and the statement is not.
    """
    if not guardrail.readonly:
        return

    scrubbed = _strip_literals(_strip_comments(sql)).strip().lower()
    if not scrubbed:
        raise ReadOnlyViolationError(source_id=guardrail.source, reason="the query is empty")

    words = _words(scrubbed)
    first_keyword = words[0] if words else ""

    if first_keyword not in READONLY_KEYWORDS:
        raise ReadOnlyViolationError(
            source_id=guardrail.source,
            reason=(
                f"only {', '.join(sorted(READONLY_KEYWORDS))} statements are allowed, "
                f"got {first_keyword.upper()}"
            ),
        )

    forbidden = sorted(WRITE_KEYWORDS.intersection(words))
    if forbidden:
        raise ReadOnlyViolationError(
            source_id=guardrail.source,
            reason=f"the statement contains write keywords: {', '.join(forbidden).upper()}",
        )

    if _has_multiple_statements(scrubbed):
        raise ReadOnlyViolationError(
            source_id=guardrail.source,
            reason="only a single statement is allowed, and this one is batched with ';'",
        )

    if first_keyword in _PROJECTION_KEYWORDS and "into" in words:
        raise ReadOnlyViolationError(
            source_id=guardrail.source,
            reason="SELECT ... INTO creates a table, which read-only mode forbids",
        )


def apply_row_limit(sql: str, guardrail: GuardrailConfig) -> str:
    """Inject TOP N into SELECT queries if max_rows is configured.

    Only modifies SELECT statements. Does not touch INSERT, UPDATE, DELETE, EXEC.

    Examples:
        SELECT * FROM t  →  SELECT TOP 1000 * FROM t
        SELECT DISTINCT name FROM t  →  SELECT DISTINCT TOP 1000 name FROM t
        SELECT TOP 10 * FROM t  →  SELECT TOP 10 * FROM t  (unchanged, user limit is lower)
        SELECT * FROM t ORDER BY c OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY  (unchanged)
    """
    if guardrail.max_rows <= 0:
        return sql

    stripped = _strip_comments(sql).strip()
    stripped_lower = stripped.lower()

    # Only apply to SELECT statements
    if not stripped_lower.startswith(("select ", "select\t", "select\n")):
        return sql

    # A paginated statement already carries its own limit, and SQL Server
    # refuses to see TOP next to OFFSET at all.
    if _paginates(stripped_lower):
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

# `OFFSET n ROWS [FETCH NEXT m ROWS ONLY]` is SQL Server's own row limit, and
# the engine rejects any query carrying both it and TOP: "A TOP can not be used
# in the same query or sub-query as a OFFSET" (Msg 10741). Injecting TOP turned
# a correct statement into invalid SQL, so callers had to rewrite valid
# pagination by hand.
#
# The match is deliberately loose. A false positive only skips the injection,
# and the pool still caps the fetch at max_rows, so the row limit holds either
# way. A false negative produces SQL the engine refuses. The asymmetry decides
# the trade-off.
_OFFSET_FETCH_PATTERN = re.compile(r"\boffset\b.+?\brows?\b", re.IGNORECASE | re.DOTALL)


def _paginates(scrubbed_sql: str) -> bool:
    """Whether the statement already limits its rows with OFFSET."""
    return bool(_OFFSET_FETCH_PATTERN.search(_strip_literals(scrubbed_sql)))


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


def _strip_literals(sql: str) -> str:
    """Blank out single-quoted strings so their contents are never scanned.

    Without this, `WHERE action = 'delete'` would look like a write statement.
    """
    return re.sub(r"'(?:[^']|'')*'", " '' ", sql)


def _words(scrubbed_sql: str) -> list[str]:
    """Identifier-like words of a comment-free, literal-free statement."""
    return re.findall(r"[a-z_][a-z0-9_]*", scrubbed_sql)


def _has_multiple_statements(scrubbed_sql: str) -> bool:
    """Whether anything follows a statement separator."""
    head, separator, tail = scrubbed_sql.partition(";")
    return bool(separator) and bool(tail.strip())
