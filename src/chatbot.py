"""
AI Financial Assistant — a conversational layer on top of ai_service.py's
raw Gemini call, distinct from the structured Ask-AI (text-to-SQL) feature.

This module owns three things ai_service.py deliberately doesn't:
1. Building a compact, user-scoped context summary to send as grounding
   (never the raw database — see build_context).
2. A deterministic, pre-LLM safety redirect for out-of-scope financial
   topics (stocks, tax, loans, legal) — cheaper and more reliable than
   asking the model to police itself, and it means those questions never
   even cost an API call.
3. Persisting the conversation, scoped to the user, with a bounded amount
   of history ever sent back to the model as context.
"""

from datetime import datetime

from src import ai_service
from src.config import get_logger
from src.database import (
    insert_chat_message,
    load_budgets,
    load_chat_messages,
    load_expenses,
)

logger = get_logger(__name__)

MAX_HISTORY_MESSAGES = 20  # ~10 turns of conversation sent to Gemini as context

_OFF_TOPIC_KEYWORDS = (
    "stock", "shares", "invest", "investment", "mutual fund", "crypto",
    "bitcoin", "tax", "itr", "loan", "emi rate", "insurance policy",
    "legal advice", "lawsuit", "sue",
)

_OFF_TOPIC_REPLY = (
    "That's outside what I can help with — I'm not a financial advisor, so I can't "
    "give investment, tax, loan, or legal guidance. I'm glad to help you understand "
    "your spending, though — want to know how a category is trending, or where you "
    "might be able to cut back?"
)


def _is_off_topic(message: str) -> bool:
    text = message.lower()
    return any(keyword in text for keyword in _OFF_TOPIC_KEYWORDS)


def build_context(user_id: int) -> str:
    """A short, human-readable financial summary — the ONLY thing about the
    user's data that ever reaches the model. Never the raw expense rows."""
    df = load_expenses(user_id)
    budgets = load_budgets(user_id)

    if df.empty:
        return "This user hasn't logged any expenses yet."

    this_month_str = datetime.now().strftime("%Y-%m")
    this_month = df[df["expense_date"].str.startswith(this_month_str)]

    lines = [f"Total spending this month: ₹{this_month['amount'].sum():,.0f}"]

    if not this_month.empty:
        by_category = this_month.groupby("category")["amount"].sum().sort_values(ascending=False)
        lines.append("Spending by category this month:")
        for category, total in by_category.items():
            lines.append(f"  - {category}: ₹{total:,.0f}")

    if not budgets.empty:
        lines.append("Monthly budgets:")
        for _, row in budgets.iterrows():
            spent = this_month[this_month["category"] == row["category"]]["amount"].sum() if not this_month.empty else 0
            lines.append(f"  - {row['category']}: ₹{spent:,.0f} spent of ₹{row['monthly_limit']:,.0f} budget")

    anomalies = this_month[this_month["is_anomaly"] == 1] if not this_month.empty else this_month
    if not anomalies.empty:
        lines.append(f"Unusual expenses flagged this month: {len(anomalies)}")

    return "\n".join(lines)


def _format_history(user_id: int) -> str:
    rows = load_chat_messages(user_id, limit=MAX_HISTORY_MESSAGES)
    if not rows:
        return "(no prior messages)"
    lines = [f"{'User' if r['role'] == 'user' else 'Assistant'}: {r['message']}" for r in rows]
    return "\n".join(lines)


def send_message(user_id: int, message: str) -> str:
    """Persist the user's message, generate a reply, persist and return it.

    Off-topic financial questions are caught before any Gemini call — both
    cheaper and more reliable than relying on the model to redirect itself
    every time.
    """
    message = message.strip()
    if not message:
        return "Please type a question."

    insert_chat_message(user_id, "user", message)

    if _is_off_topic(message):
        reply = _OFF_TOPIC_REPLY
    else:
        context = build_context(user_id)
        history_text = _format_history(user_id)
        reply = ai_service.generate_chat_reply(context, history_text, message)

    insert_chat_message(user_id, "assistant", reply)
    return reply


def get_history(user_id: int) -> list:
    return load_chat_messages(user_id)


def clear_history(user_id: int) -> None:
    from src.database import clear_chat_messages
    clear_chat_messages(user_id)
    logger.info("Cleared chat history for user_id=%s", user_id)
