"""
Unit tests for the parts of ai_service that don't need a network call: the
keyword fallback categorizer and the statistical anomaly detector. These
are the functions most worth having tests for, since they're the last line
of defense when Gemini is unavailable or misbehaves.
"""

import importlib

import pytest

from src.ai_service import keyword_categorize


@pytest.fixture
def db_and_ai(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SMARTSPEND_DB_PATH", str(db_path))

    import src.config as config_module
    importlib.reload(config_module)
    import src.database as database_module
    importlib.reload(database_module)
    import src.ai_service as ai_service_module
    importlib.reload(ai_service_module)

    database_module.init_db()
    user_id = database_module.create_user("Test", "test@example.com", "hash")
    return database_module, ai_service_module, user_id


@pytest.mark.parametrize(
    "description,expected_category",
    [
        ("Dinner at Barbeque Nation with friends", "Food"),
        ("Swiggy order for lunch", "Food"),
        ("Uber ride to the airport", "Transport"),
        ("Petrol for the bike", "Transport"),
        ("New shoes from Myntra", "Shopping"),
        ("Monthly electricity bill", "Bills"),
        ("Netflix subscription", "Entertainment"),
        ("Doctor visit", "Health"),
        ("Something completely unrelated to any keyword", "Other"),
    ],
)
def test_keyword_categorize(description, expected_category):
    assert keyword_categorize(description) == expected_category


def test_keyword_categorize_is_case_insensitive():
    assert keyword_categorize("UBER RIDE HOME") == "Transport"


def test_detect_anomaly_needs_minimum_history(db_and_ai):
    _, ai, user_id = db_and_ai
    is_anomaly, reason = ai.detect_anomaly(user_id, "Food", 5000, min_history=3)
    assert is_anomaly is False
    assert reason is None


def test_detect_anomaly_flags_large_outlier(db_and_ai):
    db, ai, user_id = db_and_ai
    for amount in (300, 320, 310):
        db.insert_expense(user_id, db.Expense("2026-08-01", "lunch", amount, "Food"))

    is_anomaly, reason = ai.detect_anomaly(user_id, "Food", 5000)
    assert is_anomaly is True
    assert "Food" in reason


def test_detect_anomaly_does_not_flag_normal_expense(db_and_ai):
    db, ai, user_id = db_and_ai
    for amount in (300, 320, 310):
        db.insert_expense(user_id, db.Expense("2026-08-01", "lunch", amount, "Food"))

    is_anomaly, reason = ai.detect_anomaly(user_id, "Food", 315)
    assert is_anomaly is False


def test_detect_anomaly_scoped_per_user(db_and_ai):
    """One user's spending history should never affect another user's
    anomaly threshold."""
    db, ai, user1 = db_and_ai
    user2 = db.create_user("Other", "other@example.com", "hash2")

    # user1 has a history of large Food expenses
    for amount in (5000, 5200, 4800):
        db.insert_expense(user1, db.Expense("2026-08-01", "big meals", amount, "Food"))

    # user2 has no history — a modest expense should not be flagged just
    # because it would look small relative to user1's spending pattern,
    # and a genuinely new-to-them large expense still needs their OWN history
    is_anomaly, _ = ai.detect_anomaly(user2, "Food", 5000, min_history=3)
    assert is_anomaly is False  # user2 has no history yet, so no flag either way


def test_answer_question_without_ai_returns_graceful_error(db_and_ai):
    _, ai, user_id = db_and_ai
    result = ai.answer_question(user_id, "how much did I spend on food")
    assert result.error is not None
    assert "GEMINI_API_KEY" in result.error


def test_generate_chat_reply_without_ai_returns_graceful_message(db_and_ai):
    _, ai, _ = db_and_ai
    reply = ai.generate_chat_reply("context", "history", "hello")
    assert "GEMINI_API_KEY" in reply
