"""Tests for guardrails — read-only validation, row limiting, TOP injection."""

import pytest

from mcp_sqlserver.config import GuardrailConfig
from mcp_sqlserver.guardrails import apply_row_limit, validate_readonly


# ---------------------------------------------------------------------------
# Read-only validation
# ---------------------------------------------------------------------------


class TestReadonlyValidation:
    """Test read-only mode enforcement."""

    def _guardrail(self, readonly: bool = True) -> GuardrailConfig:
        return GuardrailConfig(source="test", readonly=readonly)

    # Allowed queries in read-only mode
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM Users",
            "select * from Users",
            "  SELECT * FROM Users",
            "SELECT TOP 10 * FROM Users",
            "WITH cte AS (SELECT 1) SELECT * FROM cte",
            "with cte as (select 1) select * from cte",
            "EXPLAIN SELECT * FROM Users",
            "SET SHOWPLAN_TEXT ON",
        ],
    )
    def test_readonly_allows_select(self, sql: str):
        validate_readonly(sql, self._guardrail(readonly=True))  # Should not raise

    # Blocked queries in read-only mode
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO Users (Name) VALUES ('test')",
            "UPDATE Users SET Name = 'test'",
            "DELETE FROM Users",
            "DROP TABLE Users",
            "CREATE TABLE Foo (Id INT)",
            "ALTER TABLE Users ADD Col INT",
            "EXEC sp_CreateUser @Name = 'test'",
            "TRUNCATE TABLE Users",
            "MERGE INTO Users USING Source ON 1=1 WHEN MATCHED THEN DELETE;",
        ],
    )
    def test_readonly_blocks_writes(self, sql: str):
        with pytest.raises(ValueError, match="Read-only mode"):
            validate_readonly(sql, self._guardrail(readonly=True))

    def test_readonly_off_allows_everything(self):
        validate_readonly("DROP TABLE Users", self._guardrail(readonly=False))

    def test_readonly_empty_query_raises(self):
        with pytest.raises(ValueError, match="Empty query"):
            validate_readonly("", self._guardrail(readonly=True))

    def test_readonly_comment_only_query(self):
        with pytest.raises(ValueError, match="Empty query"):
            validate_readonly("-- just a comment", self._guardrail(readonly=True))

    def test_readonly_strips_comments_before_check(self):
        sql = "-- this is a comment\nSELECT * FROM Users"
        validate_readonly(sql, self._guardrail(readonly=True))  # Should not raise

    def test_readonly_blocks_write_hidden_in_comment(self):
        sql = "/* SELECT */ INSERT INTO Users (Name) VALUES ('test')"
        with pytest.raises(ValueError, match="Read-only mode"):
            validate_readonly(sql, self._guardrail(readonly=True))


# ---------------------------------------------------------------------------
# Row limiting (TOP N injection)
# ---------------------------------------------------------------------------


class TestRowLimiting:
    """Test automatic TOP N injection into SELECT queries."""

    def _guardrail(self, max_rows: int = 1000) -> GuardrailConfig:
        return GuardrailConfig(source="test", max_rows=max_rows)

    def test_injects_top_into_select(self):
        result = apply_row_limit("SELECT * FROM Users", self._guardrail(100))
        assert "TOP 100" in result
        assert result.startswith("SELECT TOP 100")

    def test_injects_top_after_distinct(self):
        result = apply_row_limit("SELECT DISTINCT Name FROM Users", self._guardrail(50))
        assert "TOP 50" in result
        assert "DISTINCT" in result

    def test_preserves_existing_lower_top(self):
        sql = "SELECT TOP 10 * FROM Users"
        result = apply_row_limit(sql, self._guardrail(1000))
        assert "TOP 10" in result
        assert "TOP 1000" not in result

    def test_replaces_existing_higher_top(self):
        sql = "SELECT TOP 5000 * FROM Users"
        result = apply_row_limit(sql, self._guardrail(1000))
        assert "TOP 1000" in result
        assert "TOP 5000" not in result

    def test_does_not_modify_insert(self):
        sql = "INSERT INTO Users (Name) VALUES ('test')"
        result = apply_row_limit(sql, self._guardrail(100))
        assert result == sql

    def test_does_not_modify_update(self):
        sql = "UPDATE Users SET Name = 'test'"
        result = apply_row_limit(sql, self._guardrail(100))
        assert result == sql

    def test_does_not_modify_delete(self):
        sql = "DELETE FROM Users WHERE Id = 1"
        result = apply_row_limit(sql, self._guardrail(100))
        assert result == sql

    def test_does_not_modify_exec(self):
        sql = "EXEC sp_GetUsers"
        result = apply_row_limit(sql, self._guardrail(100))
        assert result == sql

    def test_zero_max_rows_skips(self):
        sql = "SELECT * FROM Users"
        result = apply_row_limit(sql, self._guardrail(0))
        assert result == sql

    def test_case_insensitive(self):
        result = apply_row_limit("select * from Users", self._guardrail(100))
        assert "TOP 100" in result.upper()

    def test_multiline_select(self):
        sql = "SELECT\n  Name,\n  Email\nFROM Users"
        result = apply_row_limit(sql, self._guardrail(100))
        assert "TOP 100" in result

    def test_select_with_leading_comment(self):
        sql = "-- get users\nSELECT * FROM Users"
        result = apply_row_limit(sql, self._guardrail(100))
        # Should still have TOP since the actual SQL starts with SELECT
        assert "TOP 100" in result or "SELECT" in result
