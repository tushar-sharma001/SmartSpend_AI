"""
Tests for the SQL safety validator that sits between LLM-generated SQL and
the database — this is the single most security-critical piece of logic
in the app, so it gets the most thorough test coverage.
"""

import pytest

from src.sql_validator import MAX_ROWS, SQLValidationError, validate_and_prepare


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM expenses",
        "SELECT category, SUM(amount) FROM expenses GROUP BY category",
        "select amount from expenses where category = 'Food'",
        "SELECT e.category, b.monthly_limit FROM expenses e JOIN budgets b ON e.category = b.category",
        "SELECT * FROM expenses LIMIT 10",
    ],
)
def test_allows_plain_selects_on_allowed_tables(sql):
    result = validate_and_prepare(sql)
    assert result.strip().upper().startswith("SELECT")


@pytest.mark.parametrize(
    "sql,expected_message_fragment",
    [
        ("DROP TABLE expenses", "SELECT queries"),
        ("DELETE FROM expenses", "SELECT queries"),
        ("UPDATE expenses SET amount = 0", "SELECT queries"),
        ("INSERT INTO expenses VALUES (1,2,3)", "SELECT queries"),
        ("PRAGMA table_info(expenses)", "SELECT queries"),
        ("ATTACH DATABASE 'other.db' AS other", "SELECT queries"),
        ("CREATE TABLE evil (x INT)", "SELECT queries"),
        ("VACUUM", "SELECT queries"),
    ],
)
def test_rejects_non_select_statements(sql, expected_message_fragment):
    with pytest.raises(SQLValidationError, match=expected_message_fragment):
        validate_and_prepare(sql)


def test_rejects_multiple_statements():
    with pytest.raises(SQLValidationError, match="single query"):
        validate_and_prepare("SELECT * FROM expenses; DROP TABLE expenses;")


def test_rejects_multiple_statements_even_both_selects():
    """Stacking two harmless-looking SELECTs is still rejected — the point
    is bounding the query to exactly one statement, not judging each one."""
    with pytest.raises(SQLValidationError, match="single query"):
        validate_and_prepare("SELECT * FROM expenses; SELECT * FROM budgets")


def test_rejects_table_not_in_allowlist():
    with pytest.raises(SQLValidationError, match="table that isn't allowed"):
        validate_and_prepare("SELECT * FROM users")


def test_rejects_sqlite_internal_table():
    with pytest.raises(SQLValidationError, match="table that isn't allowed"):
        validate_and_prepare("SELECT * FROM sqlite_master")


@pytest.mark.parametrize("sql", ["", "   ", "\n\t"])
def test_rejects_empty_query(sql):
    with pytest.raises(SQLValidationError, match="empty query"):
        validate_and_prepare(sql)


def test_appends_limit_when_absent():
    result = validate_and_prepare("SELECT * FROM expenses")
    assert f"LIMIT {MAX_ROWS}" in result


def test_caps_limit_when_too_large():
    result = validate_and_prepare("SELECT * FROM expenses LIMIT 999999")
    assert f"LIMIT {MAX_ROWS}" in result
    assert "999999" not in result


def test_preserves_limit_when_within_bounds():
    result = validate_and_prepare("SELECT * FROM expenses LIMIT 10")
    assert "LIMIT 10" in result
