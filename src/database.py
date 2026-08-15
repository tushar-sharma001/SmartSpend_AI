"""
Data access layer.

Every function here owns its own connection lifecycle (open, use, close) via
a context manager instead of holding one long-lived connection in Streamlit's
global session state — this is what you'd want in any deployment target that
might run multiple worker threads or processes (Streamlit's own execution
model reruns the script on every interaction, so a per-call connection is
also just simpler to reason about than a shared one).

Every expense, budget, and chat message row is scoped to a user_id and every
query that touches user-owned data takes user_id as an explicit, required
argument — there is no "load everything" function left in this file for
those tables. Scoping happens here, once, rather than being left to each
caller to remember.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

import pandas as pd

from src.config import get_logger, settings

logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS expenses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    expense_date   TEXT NOT NULL,
    description    TEXT NOT NULL,
    amount         REAL NOT NULL CHECK (amount > 0),
    category       TEXT NOT NULL,
    is_anomaly     INTEGER NOT NULL DEFAULT 0,
    anomaly_reason TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS budgets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    category      TEXT NOT NULL,
    monthly_limit REAL NOT NULL CHECK (monthly_limit >= 0),
    UNIQUE(user_id, category)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_expenses_user_category ON expenses(user_id, category);
CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, expense_date);
CREATE INDEX IF NOT EXISTS idx_chat_user_created ON chat_messages(user_id, created_at);
"""


class DatabaseError(RuntimeError):
    """Raised when a database operation fails, so callers can catch one thing."""


class DuplicateEmailError(DatabaseError):
    """Raised specifically when a registration email is already taken."""


@dataclass(frozen=True)
class Expense:
    expense_date: str
    description: str
    amount: float
    category: str
    is_anomaly: bool = False
    anomaly_reason: Optional[str] = None


@contextmanager
def get_connection(readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Yield a connection, always closed after use.

    readonly=True opens the file in SQLite's URI read-only mode — any write
    attempt raises immediately at the driver level, which is the defense-in
    depth layer behind the Ask-AI query path (on top of, not instead of,
    the SQL validator).
    """
    if readonly:
        uri = f"file:{settings.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    except sqlite3.Error:
        if not readonly:
            conn.rollback()
        logger.exception("Database operation failed, rolled back")
        raise DatabaseError("A database operation failed")
    finally:
        conn.close()


def _migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    """Best-effort additive migration for a pre-auth database created by an
    earlier version of this app (expenses/budgets with no user_id column).

    Adds the missing column if needed. Pre-existing rows end up with a NULL
    user_id and are invisible to every user's scoped queries — for a demo
    app, deleting the old data/smartspend.db before first login is the
    simpler path (see README), but this keeps startup from crashing if
    someone upgrades in place instead.
    """
    for table in ("expenses", "budgets"):
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "user_id" not in cols:
            logger.warning("Migrating legacy '%s' table: adding user_id column", table)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")


def init_db() -> None:
    """Create tables/indexes if they don't already exist. Safe to call on every startup."""
    with get_connection() as conn:
        _migrate_legacy_schema(conn)
        conn.executescript(SCHEMA)
        conn.commit()
    logger.info("Database ready at %s", settings.db_path)


# --- Users -------------------------------------------------------------

def create_user(name: str, email: str, password_hash: str) -> int:
    """Insert a new user and return the new row id. Raises DuplicateEmailError
    if the email is already registered.

    Checks for an existing email before inserting rather than relying on
    catching the UNIQUE-constraint failure — get_connection wraps every
    sqlite3.Error into a generic DatabaseError, which would otherwise lose
    the distinction between "email taken" and "something else broke".
    A check-then-insert has a narrow race window, but for a single-process
    local SQLite app with no concurrent signups, that's an acceptable and
    far simpler tradeoff than threading the original exception type through.
    """
    if get_user_by_email(email) is not None:
        raise DuplicateEmailError("That email is already registered.")

    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
            (name, email.lower().strip(), password_hash),
        )
        conn.commit()
        user_id = cursor.lastrowid
    logger.info("Created user id=%s email=%s", user_id, email)
    return user_id


def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


# --- Expenses (user-scoped) --------------------------------------------

def insert_expense(user_id: int, expense: Expense) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO expenses
                (user_id, expense_date, description, amount, category, is_anomaly, anomaly_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                expense.expense_date,
                expense.description,
                expense.amount,
                expense.category,
                int(expense.is_anomaly),
                expense.anomaly_reason,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid
    logger.info("Inserted expense id=%s user_id=%s category=%s amount=%.2f",
                row_id, user_id, expense.category, expense.amount)
    return row_id


def delete_expense(user_id: int, expense_id: int) -> bool:
    """Delete an expense, scoped to the owner. Returns True if a row was deleted."""
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id)
        )
        conn.commit()
        return cursor.rowcount > 0


def load_expenses(user_id: int) -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql_query(
            "SELECT * FROM expenses WHERE user_id = ? ORDER BY expense_date DESC, id DESC",
            conn, params=(user_id,),
        )


def amounts_for_category(user_id: int, category: str) -> pd.Series:
    with get_connection() as conn:
        df = pd.read_sql_query(
            "SELECT amount FROM expenses WHERE user_id = ? AND category = ?",
            conn, params=(user_id, category),
        )
    return df["amount"]


# --- Budgets (user-scoped) ----------------------------------------------

def set_budget(user_id: int, category: str, monthly_limit: float) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO budgets (user_id, category, monthly_limit) VALUES (?, ?, ?)
            ON CONFLICT(user_id, category) DO UPDATE SET monthly_limit = excluded.monthly_limit
            """,
            (user_id, category, monthly_limit),
        )
        conn.commit()
    logger.info("Budget set: user_id=%s %s -> %.2f", user_id, category, monthly_limit)


def load_budgets(user_id: int) -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql_query(
            "SELECT category, monthly_limit FROM budgets WHERE user_id = ?", conn, params=(user_id,)
        )


# --- Chat messages (user-scoped) -----------------------------------------

def insert_chat_message(user_id: int, role: str, message: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, role, message) VALUES (?, ?, ?)",
            (user_id, role, message),
        )
        conn.commit()


def load_chat_messages(user_id: int, limit: Optional[int] = None) -> list[sqlite3.Row]:
    """Most recent messages last (chronological order), optionally capped to
    the most recent `limit` messages — used to bound how much history is
    ever sent to Gemini as context."""
    with get_connection() as conn:
        if limit is not None:
            rows = conn.execute(
                "SELECT * FROM (SELECT * FROM chat_messages WHERE user_id = ? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE user_id = ? ORDER BY id ASC", (user_id,)
            ).fetchall()
    return rows


def clear_chat_messages(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM chat_messages WHERE user_id = ?", (user_id,))
        conn.commit()


# --- Ask-AI: user-scoped read-only query execution -----------------------

def run_scoped_readonly_query(user_id: int, sql: str) -> pd.DataFrame:
    """Execute a caller-validated SELECT, scoped to one user's data only.

    Rather than trusting the (LLM-generated) SQL to correctly include a
    "WHERE user_id = ?" clause, this builds a throwaway in-memory SQLite
    database containing ONLY this user's expenses/budgets rows — copied
    from the real database over a read-only connection — and runs the
    query against that copy instead of the real one. Even a validated
    query that omits a user filter, or a validator that misses something,
    still can't see another user's data: the other users' rows were never
    copied into the scratch database in the first place.

    Callers are responsible for validating `sql` first (see
    src.sql_validator) — this function does not re-validate SQL syntax,
    only enforces data scope.
    """
    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    try:
        scratch.executescript(
            """
            CREATE TABLE expenses (
                id INTEGER, expense_date TEXT, description TEXT, amount REAL,
                category TEXT, is_anomaly INTEGER, anomaly_reason TEXT
            );
            CREATE TABLE budgets (category TEXT, monthly_limit REAL);
            """
        )
        with get_connection(readonly=True) as source:
            expense_rows = source.execute(
                "SELECT id, expense_date, description, amount, category, is_anomaly, anomaly_reason "
                "FROM expenses WHERE user_id = ?", (user_id,),
            ).fetchall()
            budget_rows = source.execute(
                "SELECT category, monthly_limit FROM budgets WHERE user_id = ?", (user_id,),
            ).fetchall()

        scratch.executemany(
            "INSERT INTO expenses VALUES (?, ?, ?, ?, ?, ?, ?)",
            [tuple(r) for r in expense_rows],
        )
        scratch.executemany("INSERT INTO budgets VALUES (?, ?)", [tuple(r) for r in budget_rows])
        scratch.commit()

        return pd.read_sql_query(sql, scratch)
    finally:
        scratch.close()
