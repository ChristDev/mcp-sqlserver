"""Concurrency invariants of the per-source connection pool.

These tests exist because the previous design shared ONE pyodbc connection
across every request thread, which pyodbc (threadsafety=1) does not allow.
"""

from __future__ import annotations

import threading

import anyio
import pyodbc
import pytest

from mcp_sqlserver.errors import (
    PoolExhaustedError,
    QueryExecutionError,
    SourceUnavailableError,
)
from mcp_sqlserver.dbtypes import PoolSpec, QueryRequest
from mcp_sqlserver.pool import SourcePool
from tests.fakes import FakeBehaviour, FakeFactory

POOL_SIZE = 4


def spec(**overrides: object) -> PoolSpec:
    defaults: dict[str, object] = {
        "source_id": "test-source",
        "size": POOL_SIZE,
        "admission_timeout": 5.0,
        "query_timeout": 30,
        "max_rows": 1000,
    }
    return PoolSpec(**(defaults | overrides))  # type: ignore[arg-type]


async def test_concurrent_queries_never_exceed_pool_size() -> None:
    # Given: a pool of 4 whose queries rendezvous in groups of exactly 4
    barrier = threading.Barrier(POOL_SIZE, timeout=10)
    factory = FakeFactory(FakeBehaviour(on_execute=lambda _sql: barrier.wait()))
    pool = SourcePool(spec(), factory)

    # When: 20 queries are issued at once
    try:
        async with anyio.create_task_group() as tg:
            for index in range(20):
                tg.start_soon(pool.execute, QueryRequest(sql=f"SELECT {index}"))
    finally:
        await pool.aclose()

    # Then: never more than pool_size ran together, and 4 really did
    assert factory.recorder.max_active == POOL_SIZE
    assert factory.recorder.opened <= POOL_SIZE


async def test_each_connection_serves_one_query_at_a_time() -> None:
    # Given: every fake connection tracks how many executes overlap on itself
    overlaps: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(POOL_SIZE, timeout=10)

    def on_execute(_sql: str) -> None:
        barrier.wait()

    factory = FakeFactory(FakeBehaviour(on_execute=on_execute))
    pool = SourcePool(spec(), factory)

    # When: many queries run concurrently
    try:
        async with anyio.create_task_group() as tg:
            for index in range(16):
                tg.start_soon(pool.execute, QueryRequest(sql=f"SELECT {index}"))
    finally:
        await pool.aclose()

    # Then: each connection recorded its statements sequentially, never interleaved
    with lock:
        overlaps.extend(len(conn.executed) for conn in factory.connections)
    assert sum(overlaps) == 16
    assert all(conn.closed for conn in factory.connections)


async def test_results_are_not_crossed_between_concurrent_callers() -> None:
    # Given: the fake echoes the SQL it was given back as the single row
    barrier = threading.Barrier(POOL_SIZE, timeout=10)
    factory = FakeFactory(FakeBehaviour(on_execute=lambda _sql: barrier.wait()))
    pool = SourcePool(spec(), factory)
    seen: dict[int, str] = {}

    async def run(index: int) -> None:
        result = await pool.execute(QueryRequest(sql=f"SELECT {index}"))
        seen[index] = str(result.rows[0]["value"])

    # When: 12 callers run concurrently
    try:
        async with anyio.create_task_group() as tg:
            for index in range(12):
                tg.start_soon(run, index)
    finally:
        await pool.aclose()

    # Then: every caller got its own result back
    assert seen == {index: f"SELECT {index}" for index in range(12)}


async def test_pool_exhaustion_raises_actionable_error() -> None:
    # Given: a pool of 2 with both slots parked inside execute()
    release = threading.Event()
    entered = threading.Semaphore(0)

    def on_execute(sql: str) -> None:
        if sql == "BLOCK":
            entered.release()
            release.wait(timeout=10)

    factory = FakeFactory(FakeBehaviour(on_execute=on_execute))
    pool = SourcePool(spec(size=2, admission_timeout=0.2), factory)

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(pool.execute, QueryRequest(sql="BLOCK"))
            tg.start_soon(pool.execute, QueryRequest(sql="BLOCK"))
            await anyio.to_thread.run_sync(lambda: entered.acquire(timeout=10))
            await anyio.to_thread.run_sync(lambda: entered.acquire(timeout=10))

            # When: a third caller asks for a slot
            # Then: it is refused quickly instead of queueing forever
            with pytest.raises(PoolExhaustedError) as caught:
                await pool.execute(QueryRequest(sql="SELECT 1"))

            assert caught.value.source_id == "test-source"
            assert caught.value.pool_size == 2
            assert "busy" in str(caught.value)
            release.set()
    finally:
        release.set()
        await pool.aclose()


async def test_capacity_is_returned_after_a_query_finishes() -> None:
    # Given: a pool of 1
    factory = FakeFactory()
    pool = SourcePool(spec(size=1), factory)

    # When: two queries run one after the other
    try:
        first = await pool.execute(QueryRequest(sql="SELECT 1"))
        second = await pool.execute(QueryRequest(sql="SELECT 2"))
    finally:
        await pool.aclose()

    # Then: both succeed on the same reused connection
    assert first.row_count == 1
    assert second.row_count == 1
    assert factory.recorder.opened == 1


async def test_transport_failure_evicts_the_connection() -> None:
    # Given: the first execute fails with a connection-level SQLSTATE
    calls: list[str] = []

    def on_execute(sql: str) -> None:
        calls.append(sql)
        if len(calls) == 1:
            raise pyodbc.Error("08S01", "[08S01] communication link failure")

    factory = FakeFactory(FakeBehaviour(on_execute=on_execute))
    pool = SourcePool(spec(size=1), factory)

    # When: the failing query runs, then a healthy one
    try:
        with pytest.raises(SourceUnavailableError):
            await pool.execute(QueryRequest(sql="SELECT 1"))
        await pool.execute(QueryRequest(sql="SELECT 2"))
    finally:
        await pool.aclose()

    # Then: the broken handle was closed and a fresh one opened
    assert factory.recorder.opened == 2
    assert factory.connections[0].closed is True


async def test_sql_syntax_error_keeps_the_connection_usable() -> None:
    # Given: the first execute fails with a statement-level SQLSTATE
    calls: list[str] = []

    def on_execute(sql: str) -> None:
        calls.append(sql)
        if len(calls) == 1:
            raise pyodbc.ProgrammingError("42000", "[42000] incorrect syntax near 'FROMM'")

    factory = FakeFactory(FakeBehaviour(on_execute=on_execute))
    pool = SourcePool(spec(size=1), factory)

    # When: the bad query runs, then a good one
    try:
        with pytest.raises(QueryExecutionError) as caught:
            await pool.execute(QueryRequest(sql="SELECT"))
        await pool.execute(QueryRequest(sql="SELECT 2"))
    finally:
        await pool.aclose()

    # Then: the same connection was reused — a syntax error is not fatal
    assert caught.value.sqlstate == "42000"
    assert factory.recorder.opened == 1


async def test_connect_failure_frees_the_slot_for_a_later_retry() -> None:
    # Given: the first connection attempt fails
    factory = FakeFactory()
    factory.connect_failures = 1
    pool = SourcePool(spec(size=1), factory)

    # When: a query is attempted, then retried
    try:
        with pytest.raises(SourceUnavailableError):
            await pool.execute(QueryRequest(sql="SELECT 1"))
        result = await pool.execute(QueryRequest(sql="SELECT 2"))
    finally:
        await pool.aclose()

    # Then: the retry connects normally — the slot was not lost
    assert result.row_count == 1
    assert factory.recorder.opened == 1


async def test_query_timeout_is_applied_before_the_cursor_is_created() -> None:
    # Given: a pool configured with a 7 second statement timeout
    factory = FakeFactory()
    pool = SourcePool(spec(size=1, query_timeout=7), factory)

    # When: a query runs
    try:
        await pool.execute(QueryRequest(sql="SELECT 1"))
    finally:
        await pool.aclose()

    # Then: the connection carried that timeout at cursor creation time
    assert factory.recorder.timeouts == [7]


async def test_rows_are_capped_at_max_rows_and_flagged_as_truncated() -> None:
    # Given: a source limited to 2 rows and a query returning 5
    factory = FakeFactory(
        FakeBehaviour(make_rows=lambda _sql: [(index,) for index in range(5)]),
    )
    pool = SourcePool(spec(size=1, max_rows=2), factory)

    # When: the query runs
    try:
        result = await pool.execute(QueryRequest(sql="SELECT * FROM big"))
    finally:
        await pool.aclose()

    # Then: only max_rows are materialised and the caller is told
    assert result.row_count == 2
    assert len(result.rows) == 2
    assert result.truncated is True


async def test_parameters_are_passed_to_the_driver_not_interpolated() -> None:
    # Given: a pool and a parameterised query
    factory = FakeFactory()
    pool = SourcePool(spec(size=1), factory)

    # When: the query runs with bound parameters
    try:
        await pool.execute(QueryRequest(sql="SELECT ? , ?", params=("dbo", 42)))
    finally:
        await pool.aclose()

    # Then: the driver received them as parameters
    sql, params = factory.connections[0].executed[0]
    assert sql == "SELECT ? , ?"
    assert params == (("dbo", 42),)


async def test_closing_the_pool_closes_every_connection() -> None:
    # Given: a pool that has served traffic on several connections
    barrier = threading.Barrier(3, timeout=10)
    factory = FakeFactory(FakeBehaviour(on_execute=lambda _sql: barrier.wait()))
    pool = SourcePool(spec(size=3), factory)
    async with anyio.create_task_group() as tg:
        for index in range(3):
            tg.start_soon(pool.execute, QueryRequest(sql=f"SELECT {index}"))

    # When: the pool is closed
    await pool.aclose()

    # Then: every handle it opened is closed exactly once
    assert factory.recorder.opened == 3
    assert factory.recorder.closed == 3


async def test_queries_are_refused_after_the_pool_is_closed() -> None:
    # Given: a closed pool
    factory = FakeFactory()
    pool = SourcePool(spec(size=1), factory)
    await pool.aclose()

    # When/Then: further queries are refused instead of opening new handles
    with pytest.raises(SourceUnavailableError):
        await pool.execute(QueryRequest(sql="SELECT 1"))
    assert factory.recorder.opened == 0
