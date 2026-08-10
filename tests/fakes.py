"""In-memory fake driver used to prove pool invariants without a database.

The fake raises real ``pyodbc`` exception types so the pool's error taxonomy is
exercised for real; only the transport is faked.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence

import pyodbc


class Recorder:
    """Shared counters across every fake connection of one pool.

    Mutation is the entire purpose: it accumulates observations made from
    worker threads while the pool is under load.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.opened = 0
        self.closed = 0
        self.active = 0
        self.max_active = 0
        self.timeouts: list[int] = []

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        with self._lock:
            self.active -= 1

    def opened_one(self) -> None:
        with self._lock:
            self.opened += 1

    def closed_one(self) -> None:
        with self._lock:
            self.closed += 1

    def record_timeout(self, seconds: int) -> None:
        with self._lock:
            self.timeouts.append(seconds)


class FakeCursor:
    """Cursor over a canned result set, with a hook to observe concurrency."""

    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self._rows: list[tuple[object, ...]] = []
        self._position = 0
        self.description: Sequence[tuple[str, object, object, object, object, object, object]] | None = None
        self.rowcount = -1
        self.closed = False

    def execute(self, sql: str, *params: object) -> FakeCursor:
        connection = self._connection
        connection.executed.append((sql, params))
        connection.recorder.enter()
        try:
            connection.on_execute(sql)
        finally:
            connection.recorder.leave()

        if connection.result_columns is None:
            self.description = None
            self.rowcount = connection.affected_rows
            return self

        self.description = [
            (name, str, None, 0, 0, 0, True) for name in connection.result_columns
        ]
        self._rows = list(connection.make_rows(sql))
        self._position = 0
        return self

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        chunk = self._rows[self._position : self._position + size]
        self._position += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True
        if self._connection.fail_cursor_close:
            raise pyodbc.Error("HY000", "[HY000] cursor close failed")


class FakeConnection:
    """Stand-in for ``pyodbc.Connection`` with observable lifecycle."""

    def __init__(self, recorder: Recorder, behaviour: FakeBehaviour) -> None:
        self.recorder = recorder
        self.on_execute = behaviour.on_execute
        self.result_columns = behaviour.result_columns
        self.make_rows = behaviour.make_rows
        self.affected_rows = behaviour.affected_rows
        self.fail_cursor_close = behaviour.fail_cursor_close
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False
        self.timeout = 0
        recorder.opened_one()

    def cursor(self) -> FakeCursor:
        self.recorder.record_timeout(self.timeout)
        return FakeCursor(self)

    def add_output_converter(self, sqltype: int, converter: Callable[[bytes], object]) -> None:
        """Accepted and ignored — the fake never returns driver-encoded values."""

    def close(self) -> None:
        self.closed = True
        self.recorder.closed_one()


class FakeBehaviour:
    """Knobs describing how every connection produced by a factory behaves."""

    def __init__(
        self,
        *,
        on_execute: Callable[[str], None] = lambda _sql: None,
        result_columns: tuple[str, ...] | None = ("value",),
        make_rows: Callable[[str], Sequence[tuple[object, ...]]] = lambda sql: [(sql,)],
        affected_rows: int = 0,
        fail_cursor_close: bool = False,
    ) -> None:
        self.on_execute = on_execute
        self.result_columns = result_columns
        self.make_rows = make_rows
        self.affected_rows = affected_rows
        self.fail_cursor_close = fail_cursor_close


class FakeFactory:
    """Connection factory handing out fresh :class:`FakeConnection` instances."""

    def __init__(self, behaviour: FakeBehaviour | None = None) -> None:
        self.behaviour = behaviour or FakeBehaviour()
        self.recorder = Recorder()
        self.connections: list[FakeConnection] = []
        self.connect_failures = 0
        self._lock = threading.Lock()

    def __call__(self) -> FakeConnection:
        with self._lock:
            should_fail = self.connect_failures > 0
            if should_fail:
                self.connect_failures -= 1
        if should_fail:
            raise pyodbc.OperationalError("08001", "[08001] cannot reach server")
        connection = FakeConnection(self.recorder, self.behaviour)
        with self._lock:
            self.connections.append(connection)
        return connection
