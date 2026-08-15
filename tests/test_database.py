"""
Tests for the database layer, run against a temporary SQLite file per test
so nothing here touches a real user's data. Covers CRUD, constraints, and —
critically — that one user's data is never visible to another user's
scoped queries (see test_user_isolation_*).
"""

import importlib

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Re-import src.database pointed at a throwaway DB file for each test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SMARTSPEND_DB_PATH", str(db_path))

    import src.config as config_module
    importlib.reload(config_module)
    import src.database as database_module
    importlib.reload(database_module)

    database_module.init_db()
    return database_module


@pytest.fixture
def two_users(db):
    """Two registered users, for isolation tests."""
    u1 = db.create_user("Alice", "alice@example.com", "hash1")
    u2 = db.create_user("Bob", "bob@example.com", "hash2")
    return u1, u2


def test_init_db_creates_tables(db):
    u1 = db.create_user("Alice", "alice@example.com", "hash1")
    assert db.load_expenses(u1).empty


def test_create_user_and_lookup(db):
    user_id = db.create_user("Alice", "Alice@Example.com", "hash1")
    row = db.get_user_by_email("alice@example.com")  # lowercased on lookup
    assert row is not None
    assert row["id"] == user_id
    assert row["name"] == "Alice"


def test_create_user_rejects_duplicate_email(db):
    db.create_user("Alice", "alice@example.com", "hash1")
    with pytest.raises(db.DuplicateEmailError):
        db.create_user("Someone Else", "alice@example.com", "hash2")


def test_insert_and_load_expense(db, two_users):
    u1, _ = two_users
    expense = db.Expense(
        expense_date="2026-08-01", description="Dinner at Barbeque Nation",
        amount=1200.0, category="Food",
    )
    row_id = db.insert_expense(u1, expense)
    assert row_id == 1

    df = db.load_expenses(u1)
    assert len(df) == 1
    assert df.iloc[0]["category"] == "Food"
    assert df.iloc[0]["amount"] == 1200.0
    assert df.iloc[0]["user_id"] == u1


def test_amount_check_constraint_rejects_non_positive(db, two_users):
    u1, _ = two_users
    bad_expense = db.Expense(expense_date="2026-08-01", description="broken", amount=-5.0, category="Other")
    with pytest.raises(db.DatabaseError):
        db.insert_expense(u1, bad_expense)


def test_amount_check_constraint_rejects_zero(db, two_users):
    u1, _ = two_users
    bad_expense = db.Expense(expense_date="2026-08-01", description="broken", amount=0, category="Other")
    with pytest.raises(db.DatabaseError):
        db.insert_expense(u1, bad_expense)


def test_budgets_upsert_per_user(db, two_users):
    u1, _ = two_users
    db.set_budget(u1, "Food", 5000.0)
    db.set_budget(u1, "Food", 6000.0)  # should update, not duplicate

    budgets = db.load_budgets(u1)
    assert len(budgets) == 1
    assert budgets.iloc[0]["monthly_limit"] == 6000.0


def test_amounts_for_category(db, two_users):
    u1, _ = two_users
    for amount in (100.0, 200.0, 300.0):
        db.insert_expense(u1, db.Expense("2026-08-01", "item", amount, "Food"))

    amounts = db.amounts_for_category(u1, "Food")
    assert list(amounts) == [100.0, 200.0, 300.0]
    assert db.amounts_for_category(u1, "Transport").empty


def test_delete_expense_scoped_to_owner(db, two_users):
    u1, u2 = two_users
    expense_id = db.insert_expense(u1, db.Expense("2026-08-01", "item", 100.0, "Food"))

    # user 2 cannot delete user 1's expense
    deleted = db.delete_expense(u2, expense_id)
    assert deleted is False
    assert len(db.load_expenses(u1)) == 1

    # user 1 can delete their own
    deleted = db.delete_expense(u1, expense_id)
    assert deleted is True
    assert len(db.load_expenses(u1)) == 0


# --- User isolation — the most important tests in this file ---------------

def test_user_isolation_expenses(db, two_users):
    u1, u2 = two_users
    db.insert_expense(u1, db.Expense("2026-08-01", "alice's expense", 100.0, "Food"))
    db.insert_expense(u2, db.Expense("2026-08-01", "bob's expense", 200.0, "Food"))

    df1, df2 = db.load_expenses(u1), db.load_expenses(u2)
    assert len(df1) == 1 and df1.iloc[0]["description"] == "alice's expense"
    assert len(df2) == 1 and df2.iloc[0]["description"] == "bob's expense"


def test_user_isolation_budgets(db, two_users):
    u1, u2 = two_users
    db.set_budget(u1, "Food", 1000.0)
    db.set_budget(u2, "Food", 9000.0)

    assert db.load_budgets(u1).iloc[0]["monthly_limit"] == 1000.0
    assert db.load_budgets(u2).iloc[0]["monthly_limit"] == 9000.0


def test_user_isolation_chat_messages(db, two_users):
    u1, u2 = two_users
    db.insert_chat_message(u1, "user", "alice's message")
    db.insert_chat_message(u2, "user", "bob's message")

    m1, m2 = db.load_chat_messages(u1), db.load_chat_messages(u2)
    assert len(m1) == 1 and m1[0]["message"] == "alice's message"
    assert len(m2) == 1 and m2[0]["message"] == "bob's message"


def test_user_isolation_scoped_readonly_query(db, two_users):
    u1, u2 = two_users
    db.insert_expense(u1, db.Expense("2026-08-01", "alice's expense", 100.0, "Food"))
    db.insert_expense(u2, db.Expense("2026-08-01", "bob's expense", 999.0, "Food"))

    # even an unfiltered "SELECT * FROM expenses" only ever sees the calling
    # user's own data, because the scratch copy never contained the other
    # user's rows in the first place
    result_u1 = db.run_scoped_readonly_query(u1, "SELECT * FROM expenses")
    result_u2 = db.run_scoped_readonly_query(u2, "SELECT * FROM expenses")

    assert len(result_u1) == 1 and result_u1.iloc[0]["amount"] == 100.0
    assert len(result_u2) == 1 and result_u2.iloc[0]["amount"] == 999.0


def test_chat_history_limit_and_ordering(db, two_users):
    u1, _ = two_users
    for i in range(5):
        db.insert_chat_message(u1, "user", f"message {i}")

    limited = db.load_chat_messages(u1, limit=2)
    assert [m["message"] for m in limited] == ["message 3", "message 4"]  # most recent 2, chronological order


def test_clear_chat_messages_scoped_to_user(db, two_users):
    u1, u2 = two_users
    db.insert_chat_message(u1, "user", "alice's message")
    db.insert_chat_message(u2, "user", "bob's message")

    db.clear_chat_messages(u1)
    assert len(db.load_chat_messages(u1)) == 0
    assert len(db.load_chat_messages(u2)) == 1  # bob's history untouched
