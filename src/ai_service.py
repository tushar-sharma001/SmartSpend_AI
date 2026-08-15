"""
AI service layer.

Wraps every Gemini call behind a narrow interface (categorize, answer_question)
so the rest of the app never touches the SDK directly. Two things this buys:

1. If the LLM call fails or times out, callers get a deterministic fallback
   instead of an exception bubbling into the UI.
2. Everything the model is allowed to affect (a category enum, a read-only
   SQL query) is validated before it's trusted — the model's output is
   treated as untrusted input, not as code to execute blindly.
"""

import re
from dataclasses import dataclass
from typing import Optional

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.config import CATEGORIES, get_logger, settings
from src.database import amounts_for_category, run_scoped_readonly_query
from src.sql_validator import SQLValidationError, validate_and_prepare

logger = get_logger(__name__)

_model = None
if settings.ai_enabled:
    try:
        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        _model = genai.GenerativeModel(settings.gemini_model)
        logger.info("Gemini model '%s' initialized", settings.gemini_model)
    except Exception:
        logger.exception("Failed to initialize Gemini — falling back to offline mode")
        _model = None

AI_AVAILABLE = _model is not None

KEYWORD_MAP: dict[str, list[str]] = {
    "Food": ["restaurant", "food", "lunch", "dinner", "breakfast", "cafe", "coffee", "swiggy", "zomato", "grocery"],
    "Transport": ["uber", "ola", "taxi", "fuel", "petrol", "diesel", "metro", "bus", "train", "flight", "cab"],
    "Shopping": ["amazon", "flipkart", "clothes", "shoes", "shopping", "myntra", "mall"],
    "Bills": ["electricity", "wifi", "internet", "rent", "bill", "recharge", "emi"],
    "Entertainment": ["movie", "netflix", "spotify", "concert", "game", "party"],
    "Health": ["medicine", "doctor", "hospital", "pharmacy", "gym", "health"],
}

_RETRYABLE = retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type(Exception),
)


def keyword_categorize(description: str) -> str:
    """Deterministic fallback categorizer — no network call, always available."""
    text = description.lower()
    for category, keywords in KEYWORD_MAP.items():
        if any(keyword in text for keyword in keywords):
            return category
    return "Other"


@_RETRYABLE
def _call_gemini_categorize(description: str) -> str:
    prompt = (
        f"Classify this expense description into exactly one of these categories: "
        f"{', '.join(CATEGORIES)}.\n\n"
        f'Expense description: "{description}"\n\n'
        f"Reply with ONLY the category name, nothing else."
    )
    response = _model.generate_content(prompt)
    return response.text.strip()


def categorize(description: str) -> str:
    """Categorize an expense description, using the LLM when available."""
    if not AI_AVAILABLE:
        return keyword_categorize(description)
    try:
        raw = _call_gemini_categorize(description)
        for category in CATEGORIES:
            if category.lower() in raw.lower():
                return category
        logger.warning("Gemini returned an unrecognized category %r, falling back", raw)
        return keyword_categorize(description)
    except Exception:
        logger.exception("Gemini categorization failed, falling back to keyword matcher")
        return keyword_categorize(description)


def detect_anomaly(user_id: int, category: str, amount: float, min_history: int = 3) -> tuple[bool, Optional[str]]:
    """Flag an expense as unusual using a simple mean + std-dev threshold,
    scoped to this user's own history in this category.

    Deliberately not LLM-based: there's no labeled dataset of "actually
    anomalous" expenses to justify a learned model, and a transparent
    statistical rule is easier to explain and audit. Handles the empty- and
    small-history cases explicitly (a std-dev of 0 or 1-2 data points isn't
    enough to call anything "unusual" yet) and guards the amount == 0 case
    so a zero-average category can never divide/compare into a spurious flag.
    """
    history = amounts_for_category(user_id, category)
    if len(history) < min_history:
        return False, None

    avg = history.mean()
    if avg <= 0:
        return False, None
    std = history.std() or 0.0
    threshold = avg + (2 * std if std > 0 else avg)

    if amount > threshold and amount > avg * 1.5:
        return True, f"₹{amount:,.0f} is well above your usual {category} spend (avg ₹{avg:,.0f})"
    return False, None


SCHEMA_DESCRIPTION = """
Table: expenses
Columns:
  id INTEGER
  expense_date TEXT (format YYYY-MM-DD)
  description TEXT
  amount REAL
  category TEXT (one of: Food, Transport, Shopping, Bills, Entertainment, Health, Other)
  is_anomaly INTEGER (0 or 1)
  anomaly_reason TEXT

Table: budgets
Columns:
  category TEXT
  monthly_limit REAL

Note: both tables are already scoped to the current user — never filter or
reference any user/account/owner column, none exists in this view.
"""


@_RETRYABLE
def _call_gemini_sql(question: str) -> str:
    prompt = (
        f"You write SQLite queries. Given this schema:\n{SCHEMA_DESCRIPTION}\n\n"
        f'Write ONE read-only SQLite SELECT query that answers this question:\n"{question}"\n\n'
        f"Rules:\n"
        f"- Only output the raw SQL query, nothing else — no markdown, no explanation.\n"
        f"- Only use SELECT statements against the expenses and/or budgets tables.\n"
        f"- Never use INSERT, UPDATE, DELETE, DROP, ALTER, PRAGMA, or ATTACH.\n"
        f"- Never write more than one statement."
    )
    response = _model.generate_content(prompt)
    return response.text.strip()


@_RETRYABLE
def _call_gemini_summarize(question: str, result_df: pd.DataFrame) -> str:
    table_text = result_df.to_string(index=False) if not result_df.empty else "(no rows)"
    prompt = (
        f'The user asked: "{question}"\n'
        f"The query result (as a table) is:\n{table_text}\n\n"
        f"Answer the user's question in one short, natural sentence based only on this data."
    )
    return _model.generate_content(prompt).text.strip()


@dataclass
class QAResult:
    sql: Optional[str]
    result_df: Optional[pd.DataFrame]
    summary: Optional[str]
    error: Optional[str] = None


def answer_question(user_id: int, question: str) -> QAResult:
    """Answer a natural-language question about this user's expenses/budgets.

    Pipeline: question -> LLM generates SQL -> validated (sql_validator:
    single read-only SELECT, allowed tables only, row-limited) -> executed
    against a copy of ONLY this user's data (database.run_scoped_readonly_query)
    -> LLM summarizes the result in plain English. Every stage fails safely,
    and user-data isolation is enforced by the database layer regardless of
    what SQL the model generates — see run_scoped_readonly_query's docstring.
    """
    if not AI_AVAILABLE:
        return QAResult(sql=None, result_df=None, summary=None,
                         error="AI features are disabled — no GEMINI_API_KEY configured.")

    try:
        raw_sql = _call_gemini_sql(question)
        raw_sql = re.sub(r"^```sql|^```|```$", "", raw_sql, flags=re.MULTILINE).strip()
    except Exception:
        logger.exception("Failed to generate SQL for question: %s", question)
        return QAResult(sql=None, result_df=None, summary=None, error="Couldn't process that question right now.")

    try:
        safe_sql = validate_and_prepare(raw_sql)
    except SQLValidationError as e:
        logger.warning("Rejected generated query (%s): %s", e, raw_sql)
        return QAResult(sql=raw_sql, result_df=None, summary=None,
                         error=f"Couldn't safely run that query ({e}) — try rephrasing your question.")

    try:
        result_df = run_scoped_readonly_query(user_id, safe_sql)
    except Exception:
        logger.exception("Validated query failed to execute: %s", safe_sql)
        return QAResult(sql=safe_sql, result_df=None, summary=None, error="That query didn't run cleanly.")

    try:
        summary = _call_gemini_summarize(question, result_df)
    except Exception:
        logger.exception("Failed to summarize query result")
        summary = None

    return QAResult(sql=safe_sql, result_df=result_df, summary=summary)


# --- AI Financial Assistant (conversational chatbot) ----------------------

_CHAT_SYSTEM_INSTRUCTION = """You are SmartSpend AI's budgeting assistant. You help the
user understand and improve their own spending based only on the summary
data provided below. Keep replies short (2-4 sentences), specific, and
grounded only in the numbers given — never invent figures.

You are NOT a licensed financial advisor. If asked about stock picks,
specific investments, tax filing, loans, insurance products, or legal
matters, say briefly that's outside what you can help with, and steer the
conversation back to their spending and budgeting."""


@_RETRYABLE
def _call_gemini_chat(context: str, history_text: str, message: str) -> str:
    prompt = (
        f"{_CHAT_SYSTEM_INSTRUCTION}\n\n"
        f"--- User's current financial summary ---\n{context}\n\n"
        f"--- Recent conversation ---\n{history_text}\n\n"
        f"User: {message}\nAssistant:"
    )
    return _model.generate_content(prompt).text.strip()


def generate_chat_reply(context: str, history_text: str, message: str) -> str:
    """Generate one chatbot reply. Falls back to a fixed, honest message if
    Gemini is unavailable or fails — the chat UI should never appear to hang
    or silently drop a message."""
    if not AI_AVAILABLE:
        return ("AI chat is currently unavailable (no GEMINI_API_KEY configured), so I can't "
                "have a conversation right now — but your expenses, budgets, and the Dashboard "
                "insights still work without it.")
    try:
        return _call_gemini_chat(context, history_text, message)
    except Exception:
        logger.exception("Chat reply generation failed")
        return "Sorry, I couldn't process that just now — please try asking again."
