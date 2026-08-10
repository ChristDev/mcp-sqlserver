"""Value objects and driver protocols shared by the pool and its callers.

Kept separate from :mod:`mcp_sqlserver.pool` so that the pool module owns only
lease and admission behaviour, and so callers can depend on the data shapes
without importing the machinery.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as clock_time
from decimal import Decimal
from typing import Final, Protocol, TypeAlias

SqlValue: TypeAlias = str | int | float | bool | bytes | date | datetime | clock_time | Decimal | None
SqlParams: TypeAlias = tuple[SqlValue, ...]
Row: TypeAlias = dict[str, SqlValue]

#: Hard ceiling applied when a source disables its own row limit, so that a
#: single careless query can never exhaust the server's memory.
ABSOLUTE_MAX_ROWS: Final = 100_000


class DbCursor(Protocol):
    """The slice of ``pyodbc.Cursor`` the pool depends on."""

    description: Sequence[Sequence[object]] | None
    rowcount: int

    def execute(self, sql: str, *params: object) -> object: ...
    def fetchmany(self, size: int) -> list[Sequence[object]]: ...
    def close(self) -> None: ...


class DbConnection(Protocol):
    """The slice of ``pyodbc.Connection`` the pool depends on."""

    timeout: int

    def cursor(self) -> DbCursor: ...
    def close(self) -> None: ...


ConnectionFactory: TypeAlias = Callable[[], DbConnection]


@dataclass(frozen=True, slots=True)
class PoolSpec:
    """Everything a pool needs to know about the source it serves."""

    source_id: str
    size: int
    admission_timeout: float
    query_timeout: int
    max_rows: int

    @property
    def row_ceiling(self) -> int:
        """Rows to materialise at most, honouring the absolute safety ceiling."""
        return self.max_rows if self.max_rows > 0 else ABSOLUTE_MAX_ROWS


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """A statement plus its bound parameters."""

    sql: str
    params: SqlParams = ()


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Outcome of one statement."""

    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    row_count: int
    elapsed_ms: int
    truncated: bool
    message: str | None = None
