"""Bounded connection pool for a single database source.

pyodbc reports ``threadsafety = 1``: a connection may be handed to different
threads over time, but never used by two of them at once. This pool therefore
leases exactly one connection to exactly one complete blocking operation —
connect, cursor, execute, fetch, close — and bounds how many operations may be
in flight per source. Admission happens before a worker thread is claimed, so a
saturated source fails fast instead of queueing without limit.
"""

from __future__ import annotations

import logging
import queue
import time
from typing import Final

import anyio
import anyio.to_thread
import pyodbc

from mcp_sqlserver.dbtypes import (
    ConnectionFactory,
    DbConnection,
    DbCursor,
    PoolSpec,
    QueryRequest,
    QueryResult,
)
from mcp_sqlserver.errors import (
    DatabaseError,
    PoolExhaustedError,
    QueryExecutionError,
    QueryTimeoutError,
    SourceUnavailableError,
)

logger = logging.getLogger(__name__)

#: How long shutdown waits for each in-flight lease to come back.
SHUTDOWN_GRACE_SECONDS: Final = 30.0

#: SQLSTATEs that mean the statement was cancelled or timed out.
TIMEOUT_SQLSTATES: Final = frozenset({"HYT00", "HYT01"})

#: SQLSTATEs that leave the handle in an unknown state — never reuse it.
FATAL_SQLSTATES: Final = frozenset({"HY008", "HY010", "HYT00", "HYT01"})

#: SQLSTATE class 08 is "connection exception" in the ODBC specification.
CONNECTION_SQLSTATE_CLASS: Final = "08"


class SourcePool:
    """Leases connections for one source, one complete operation at a time."""

    def __init__(self, spec: PoolSpec, factory: ConnectionFactory) -> None:
        self._spec = spec
        self._factory = factory
        self._limiter = anyio.CapacityLimiter(spec.size)
        self._slots: queue.SimpleQueue[DbConnection | None] = queue.SimpleQueue()
        for _ in range(spec.size):
            self._slots.put(None)
        self._closing = False

    @property
    def spec(self) -> PoolSpec:
        """The configuration this pool was built with."""
        return self._spec

    async def execute(self, request: QueryRequest) -> QueryResult:
        """Run one statement on a leased connection, waiting only for admission."""
        self._reject_when_closing()
        await self._admit()
        try:
            return await anyio.to_thread.run_sync(self._run, request)
        finally:
            self._limiter.release()

    async def warm_up(self) -> None:
        """Open one connection so startup surfaces credential and network faults."""
        self._reject_when_closing()
        await self._admit()
        try:
            await anyio.to_thread.run_sync(self._warm_up_blocking)
        finally:
            self._limiter.release()

    async def aclose(self) -> None:
        """Stop admitting work, wait for in-flight leases, then close every handle."""
        if self._closing:
            return
        self._closing = True
        await anyio.to_thread.run_sync(self._drain_blocking)

    # -- admission ---------------------------------------------------------

    def _reject_when_closing(self) -> None:
        if self._closing:
            raise SourceUnavailableError(
                source_id=self._spec.source_id,
                detail="the server is shutting down",
            )

    async def _admit(self) -> None:
        try:
            with anyio.fail_after(self._spec.admission_timeout):
                await self._limiter.acquire()
        except TimeoutError as exc:
            raise PoolExhaustedError(
                source_id=self._spec.source_id,
                pool_size=self._spec.size,
                wait_seconds=self._spec.admission_timeout,
            ) from exc

    # -- blocking section, always inside a worker thread -------------------

    def _run(self, request: QueryRequest) -> QueryResult:
        slot = self._slots.get_nowait()
        connection: DbConnection | None = slot
        cursor: DbCursor | None = None
        keep: DbConnection | None = None
        try:
            if connection is None:
                connection = self._connect()
            connection.timeout = self._spec.query_timeout
            cursor = connection.cursor()
            result = self._collect(cursor, self._execute(cursor, request))
            self._close_cursor(cursor)
            cursor = None
            keep = connection
        except pyodbc.Error as exc:
            sqlstate = _sqlstate(exc)
            if connection is not None and not _is_fatal(sqlstate):
                keep = connection
            raise self._translate(sqlstate, exc) from exc
        else:
            return result
        finally:
            if cursor is not None:
                _close_cursor_quietly(cursor)
            self._release_slot(connection, keep)

    def _warm_up_blocking(self) -> None:
        slot = self._slots.get_nowait()
        connection: DbConnection | None = slot
        try:
            if connection is None:
                connection = self._connect()
        finally:
            self._release_slot(connection, connection)

    def _release_slot(self, connection: DbConnection | None, keep: DbConnection | None) -> None:
        """Return the slot, then discard whatever must not be reused."""
        retained = None if self._closing else keep
        evicted = connection if connection is not retained else None
        self._slots.put(retained)
        if evicted is not None:
            _close_quietly(evicted)

    def _connect(self) -> DbConnection:
        try:
            connection = self._factory()
        except pyodbc.Error as exc:
            raise SourceUnavailableError(
                source_id=self._spec.source_id,
                detail=_message(exc),
                sqlstate=_sqlstate(exc),
            ) from exc
        connection.timeout = self._spec.query_timeout
        return connection

    def _execute(self, cursor: DbCursor, request: QueryRequest) -> float:
        """Run the statement, returning the monotonic clock reading it started at."""
        started = time.perf_counter()
        if request.params:
            cursor.execute(request.sql, request.params)
        else:
            cursor.execute(request.sql)
        return started

    def _close_cursor(self, cursor: DbCursor) -> None:
        """Close the cursor after a successful statement.

        A failure here means the handle is in an unknown state. Raising a
        ``DatabaseError`` rather than a ``pyodbc.Error`` skips the reuse branch
        in :meth:`_run`, so the connection is evicted instead of pooled.
        """
        try:
            cursor.close()
        except pyodbc.Error as exc:
            logger.warning(
                "[%s] Discarding connection: cursor close failed: %s",
                self._spec.source_id,
                exc,
            )
            raise self._translate(_sqlstate(exc), exc) from exc

    def _collect(self, cursor: DbCursor, started: float) -> QueryResult:
        elapsed = int((time.perf_counter() - started) * 1000)
        description = cursor.description
        if description is None:
            affected = max(cursor.rowcount, 0)
            return QueryResult(
                columns=(),
                rows=(),
                row_count=affected,
                elapsed_ms=elapsed,
                truncated=False,
                message=f"{affected} row(s) affected",
            )

        columns = tuple(str(column[0]) for column in description)
        ceiling = self._spec.row_ceiling
        fetched = cursor.fetchmany(ceiling + 1)
        truncated = len(fetched) > ceiling
        kept = fetched[:ceiling]
        rows = tuple(dict(zip(columns, row, strict=False)) for row in kept)
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            truncated=truncated,
        )

    def _drain_blocking(self) -> None:
        for _ in range(self._spec.size):
            try:
                connection = self._slots.get(timeout=SHUTDOWN_GRACE_SECONDS)
            except queue.Empty:
                logger.warning(
                    "[%s] Shutdown gave up waiting for an in-flight query", self._spec.source_id
                )
                return
            if connection is not None:
                _close_quietly(connection)

    def _translate(self, sqlstate: str | None, exc: pyodbc.Error) -> DatabaseError:
        if sqlstate in TIMEOUT_SQLSTATES:
            return QueryTimeoutError(
                source_id=self._spec.source_id,
                timeout_seconds=self._spec.query_timeout,
            )
        if sqlstate is not None and sqlstate.startswith(CONNECTION_SQLSTATE_CLASS):
            return SourceUnavailableError(
                source_id=self._spec.source_id,
                detail=_message(exc),
                sqlstate=sqlstate,
            )
        return QueryExecutionError(
            source_id=self._spec.source_id,
            sqlstate=sqlstate,
            detail=_message(exc),
        )


def _sqlstate(exc: pyodbc.Error) -> str | None:
    """SQLSTATE reported by the driver, when it gave one."""
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return None


def _is_fatal(sqlstate: str | None) -> bool:
    """Whether the handle must be discarded rather than returned to the pool.

    An absent SQLSTATE counts as fatal: the worker cannot establish that the
    statement finished cleanly, so reusing the handle would risk cross-talk.
    """
    if sqlstate is None:
        return True
    return sqlstate.startswith(CONNECTION_SQLSTATE_CLASS) or sqlstate in FATAL_SQLSTATES


def _close_cursor_quietly(cursor: DbCursor) -> None:
    """Close a cursor while another error is already propagating."""
    try:
        cursor.close()
    except pyodbc.Error as exc:
        logger.debug("Error while closing a cursor during cleanup: %s", exc)


def _message(exc: pyodbc.Error) -> str:
    """Human-readable half of a pyodbc error."""
    if len(exc.args) > 1 and isinstance(exc.args[1], str):
        return exc.args[1]
    return str(exc)


def _close_quietly(connection: DbConnection) -> None:
    """Close a handle that is already being discarded."""
    try:
        connection.close()
    except pyodbc.Error as exc:
        logger.debug("Error while closing a discarded connection: %s", exc)
