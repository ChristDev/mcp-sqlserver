"""Tests for tools — result formatting and serialization."""

from datetime import date, datetime, time, timezone
from decimal import Decimal

from mcp_sqlserver.dbtypes import QueryResult, Row
from mcp_sqlserver.tools import _format_result, _serialize_row


def make_result(
    columns: tuple[str, ...] = (),
    rows: tuple[Row, ...] = (),
    *,
    elapsed_ms: int = 0,
    truncated: bool = False,
    message: str | None = None,
) -> QueryResult:
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows) if columns else 0,
        elapsed_ms=elapsed_ms,
        truncated=truncated,
        message=message,
    )


class TestSerializeRow:
    """Test row serialization for JSON output."""

    def test_basic_types(self):
        row = {"id": 1, "name": "test", "active": True, "score": 3.14}
        result = _serialize_row(row)
        assert result == row

    def test_datetime(self):
        row = {"created": datetime(2026, 1, 15, 10, 30, 0)}
        result = _serialize_row(row)
        assert result["created"] == "2026-01-15T10:30:00"

    def test_date(self):
        row = {"birthday": date(1990, 5, 20)}
        result = _serialize_row(row)
        assert result["birthday"] == "1990-05-20"

    def test_time(self):
        row = {"start_time": time(14, 30, 0)}
        result = _serialize_row(row)
        assert result["start_time"] == "14:30:00"

    def test_datetime_with_timezone(self):
        row = {"created": datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)}
        result = _serialize_row(row)
        assert "2026-01-15T10:30:00" in result["created"]

    def test_decimal(self):
        row = {"amount": Decimal("123.45")}
        result = _serialize_row(row)
        assert result["amount"] == 123.45
        assert isinstance(result["amount"], float)

    def test_bytes(self):
        row = {"data": b"\x00\x01\x02\x03"}
        result = _serialize_row(row)
        assert result["data"] == "<binary 4 bytes>"

    def test_none_value(self):
        row = {"nullable_col": None}
        result = _serialize_row(row)
        assert result["nullable_col"] is None


class TestFormatResult:
    """Test query result formatting."""

    def test_select_with_rows(self):
        result = make_result(
            columns=("id", "name"),
            rows=({"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}),
            elapsed_ms=50,
        )
        output = _format_result(result)
        assert '"Alice"' in output
        assert '"Bob"' in output
        assert "2 row(s)" in output
        assert "50ms" in output

    def test_select_empty(self):
        result = make_result(columns=("id", "name"), elapsed_ms=10)
        output = _format_result(result)
        assert "0 rows returned" in output

    def test_insert_update_delete(self):
        result = make_result(elapsed_ms=20, message="5 row(s) affected")
        output = _format_result(result)
        assert "5 row(s) affected" in output

    def test_truncation_is_reported(self):
        result = make_result(
            columns=("id",),
            rows=tuple({"id": index} for index in range(1000)),
            elapsed_ms=100,
            truncated=True,
        )
        output = _format_result(result)
        assert "truncated" in output

    def test_complete_result_has_no_truncation_notice(self):
        result = make_result(columns=("id",), rows=({"id": 1},), elapsed_ms=5)
        output = _format_result(result)
        assert "truncated" not in output
