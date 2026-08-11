"""Tests for the typed errors — they must survive the interpreter's own handling.

Regression: every error below was a ``@dataclass(frozen=True, slots=True)``.
Frozen generates a ``__setattr__`` that refuses every assignment, and the
interpreter assigns to an exception as it travels: ``exc.__traceback__ = tb``
while propagating, ``add_note()`` when a library annotates it. With ``slots``
on top, the decorator returns a NEW class object while that ``__setattr__``
still closes over the original, so the refusal surfaced as
``TypeError: super(type, obj): obj must be an instance or subtype of type``.

The visible effect was that EVERY typed error — timeout, exhausted pool, bad
credentials, read-only violation — reached the MCP client as that one opaque
TypeError, with its real message destroyed. These tests fail if ``frozen=True``
is ever put back.
"""

import pytest

from mcp_sqlserver.errors import (
    DatabaseError,
    InvalidConfigError,
    PoolExhaustedError,
    QueryExecutionError,
    QueryTimeoutError,
    ReadOnlyViolationError,
    SourceUnavailableError,
    UnknownSourceError,
)

# One built instance per type, so every error is exercised, not just the first.
ERRORS = [
    InvalidConfigError(field="server.port", value="'x'", expected="an integer >= 1"),
    UnknownSourceError(source_id="nope", known_sources=("a", "b")),
    PoolExhaustedError(source_id="s", pool_size=4, wait_seconds=5.0),
    SourceUnavailableError(source_id="s", detail="login failed", sqlstate="28000"),
    QueryTimeoutError(source_id="s", timeout_seconds=30),
    QueryExecutionError(source_id="s", sqlstate="10741", detail="TOP with OFFSET"),
    ReadOnlyViolationError(source_id="s", reason="write verb"),
]

IDS = [type(e).__name__ for e in ERRORS]


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_accepts_a_traceback(error: Exception):
    """contextlib does exactly this while unwinding; frozen made it explode."""
    error.__traceback__ = None


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_accepts_a_note(error: Exception):
    error.add_note("annotated by a library")


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_survives_being_raised_through_a_context_manager(error: Exception):
    """The exact path that broke: an exception crossing a contextmanager exit.

    ``contextlib`` reassigns ``__traceback__`` on the way out, which is where
    the opaque TypeError was produced instead of this error.
    """
    from contextlib import contextmanager

    @contextmanager
    def passthrough():
        yield

    with pytest.raises(type(error)) as caught:
        with passthrough():
            raise error

    assert caught.value is error


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_message_is_written_for_a_person(error: Exception):
    """The message is the product here — it is what the MCP client shows."""
    message = str(error)
    assert message
    assert message != repr(error)
    assert "object at 0x" not in message


def test_database_errors_share_one_base():
    """Callers catch DatabaseError; InvalidConfigError is config, not access."""
    for error in ERRORS:
        if isinstance(error, InvalidConfigError):
            continue
        assert isinstance(error, DatabaseError)
