"""Typed errors for database access.

Every failure this server can surface to an MCP client is one of these types.
Callers match on the type instead of parsing message strings, and the message
that reaches the user is written for the user, not for a stack trace.
"""

from __future__ import annotations

from dataclasses import dataclass


class DatabaseError(Exception):
    """Base for every database access failure."""


@dataclass(frozen=True, slots=True)
class InvalidConfigError(Exception):
    """A configured value is outside the range the server can honour."""

    field: str
    value: str
    expected: str

    def __str__(self) -> str:
        return f"Invalid config: {self.field}={self.value} — expected {self.expected}"


@dataclass(frozen=True, slots=True)
class UnknownSourceError(DatabaseError):
    """A query named a source id that is not configured."""

    source_id: str
    known_sources: tuple[str, ...]

    def __str__(self) -> str:
        known = ", ".join(self.known_sources) or "none configured"
        return f"Unknown database source {self.source_id!r}. Configured sources: {known}"


@dataclass(frozen=True, slots=True)
class PoolExhaustedError(DatabaseError):
    """Every connection slot for the source was busy for longer than the admission wait."""

    source_id: str
    pool_size: int
    wait_seconds: float

    def __str__(self) -> str:
        return (
            f"Database {self.source_id!r} is busy: {self.pool_size} queries are already "
            f"running and no slot became available within {self.wait_seconds:g}s. "
            f"Retry shortly or raise pool_size for this source."
        )


@dataclass(frozen=True, slots=True)
class SourceUnavailableError(DatabaseError):
    """The source could not be reached or authenticated."""

    source_id: str
    detail: str
    sqlstate: str | None = None

    def __str__(self) -> str:
        return f"Cannot connect to database {self.source_id!r}: {self.detail}"


@dataclass(frozen=True, slots=True)
class QueryTimeoutError(DatabaseError):
    """The statement ran longer than the configured query timeout."""

    source_id: str
    timeout_seconds: int

    def __str__(self) -> str:
        return (
            f"Query on {self.source_id!r} exceeded the {self.timeout_seconds}s timeout "
            f"and was cancelled. Narrow the query or raise query_timeout for this source."
        )


@dataclass(frozen=True, slots=True)
class QueryExecutionError(DatabaseError):
    """The server executed the statement and SQL Server rejected it."""

    source_id: str
    sqlstate: str | None
    detail: str

    def __str__(self) -> str:
        state = f" [{self.sqlstate}]" if self.sqlstate else ""
        return f"SQL error{state} on {self.source_id!r}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ReadOnlyViolationError(DatabaseError):
    """A write statement was rejected because the source is configured read-only."""

    source_id: str
    reason: str

    def __str__(self) -> str:
        return f"Read-only mode is enabled for source {self.source_id!r}: {self.reason}"
