"""Tests for tools — result formatting and serialization."""

from datetime import date, datetime, time, timezone
from decimal import Decimal

from mcp_sqlserver.tools import _format_result, _serialize_row


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
        result = {
            "columns": ["id", "name"],
            "rows": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
            "row_count": 2,
            "execution_time_ms": 50,
        }
        output = _format_result(result)
        assert '"Alice"' in output
        assert '"Bob"' in output
        assert "2 row(s)" in output
        assert "50ms" in output

    def test_select_empty(self):
        result = {
            "columns": ["id", "name"],
            "rows": [],
            "row_count": 0,
            "execution_time_ms": 10,
        }
        output = _format_result(result)
        assert "0 rows returned" in output

    def test_insert_update_delete(self):
        result = {
            "columns": [],
            "rows": [],
            "row_count": 5,
            "execution_time_ms": 20,
            "message": "5 row(s) affected",
        }
        output = _format_result(result)
        assert "5 row(s) affected" in output

    def test_row_limit_indicator(self):
        result = {
            "columns": ["id"],
            "rows": [{"id": i} for i in range(1000)],
            "row_count": 1000,
            "execution_time_ms": 100,
        }
        output = _format_result(result)
        assert "row limit applied" in output

    def test_below_limit_no_indicator(self):
        result = {
            "columns": ["id"],
            "rows": [{"id": 1}],
            "row_count": 1,
            "execution_time_ms": 5,
        }
        output = _format_result(result)
        assert "row limit" not in output
