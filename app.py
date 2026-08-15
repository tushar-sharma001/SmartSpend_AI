"""
SmartSpend AI — Streamlit UI.

This file renders pages and wires widgets to the src/ modules; it holds no
SQL, no prompt strings, and (deliberately) no business logic beyond simple
UI state — auth, categorization, anomaly detection, insights, and the
chatbot all live in src/ and are independently tested (see tests/).

Look/feel: a custom CSS layer is injected on top of Streamlit's own theming
to get a fintech-SaaS card/sidebar look without introducing a second
frontend framework — see the README for why this stayed in Streamlit
rather than becoming a separate Flask + HTML/JS app.
"""

from datetime import date, datetime

import pandas as pd
import plotly.express as px
import streamlit as st

from src import ai_service, auth, chatbot, database, insights
from src.config import CATEGORIES, get_logger, settings

logger = get_logger(__name__)

st.set_page_config(page_title="SmartSpend AI", page_icon="💸", layout="wide", initial_sidebar_state="expanded")
database.init_db()

APP_VERSION = "v2.0"

# ---------------------------------------------------------------------------
# Theme / CSS
# ---------------------------------------------------------------------------
if "theme" not in st.session_state:
    st.session_state.theme = "light"

_THEMES = {
    "light": {
        "bg": "#F7F8FA", "card_bg": "#FFFFFF", "text": "#1A1D29", "muted": "#6B7280",
        "border": "#E5E7EB", "primary": "#2E7D32", "primary_soft": "#E8F5E9",
        "sidebar_bg": "#FFFFFF", "shadow": "0 1px 3px rgba(0,0,0,0.06)",
    },
    "dark": {
        "bg": "#0F1115", "card_bg": "#1A1D24", "text": "#E8E9EC", "muted": "#9CA3AF",
        "border": "#2A2E37", "primary": "#4CAF50", "primary_soft": "#1E3A21",
        "sidebar_bg": "#15171D", "shadow": "0 1px 3px rgba(0,0,0,0.4)",
    },
}
_t = _THEMES[st.session_state.theme]

st.markdown(f"""
<style>
:root {{
    --bg: {_t['bg']}; --card-bg: {_t['card_bg']}; --text: {_t['text']};
    --muted: {_t['muted']}; --border: {_t['border']}; --primary: {_t['primary']};
    --primary-soft: {_t['primary_soft']}; --sidebar-bg: {_t['sidebar_bg']}; --shadow: {_t['shadow']};
}}
.stApp {{ background: var(--bg); }}
[data-testid="stSidebar"] {{ background: var(--sidebar-bg); border-right: 1px solid var(--border); }}
h1, h2, h3, p, span, label, div {{ color: var(--text); }}

.ss-brand {{ font-size: 1.35rem; font-weight: 700; color: var(--primary); padding: 0.5rem 0 1rem 0; }}
.ss-card {{
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 14px;
    padding: 1.1rem 1.3rem; box-shadow: var(--shadow); margin-bottom: 0.9rem;
}}
.ss-metric-label {{ color: var(--muted); font-size: 0.82rem; font-weight: 500; text-transform: uppercase; letter-spacing: 0.03em; }}
.ss-metric-value {{ font-size: 1.7rem; font-weight: 700; color: var(--text); margin-top: 0.15rem; }}
.ss-badge {{
    display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600;
}}
.ss-badge-normal {{ background: var(--primary-soft); color: var(--primary); }}
.ss-badge-unusual {{ background: #FDECEA; color: #C62828; }}
.ss-notification {{
    border-radius: 10px; padding: 0.7rem 1rem; margin-bottom: 0.5rem; font-size: 0.92rem;
    border-left: 4px solid var(--border);
}}
.ss-notification-info {{ background: var(--primary-soft); border-left-color: var(--primary); }}
.ss-notification-warning {{ background: #FFF8E1; border-left-color: #F9A825; color: #7A5B00; }}
.ss-notification-critical {{ background: #FDECEA; border-left-color: #C62828; color: #8B1E1E; }}
.ss-empty-state {{ text-align: center; padding: 3rem 1rem; color: var(--muted); }}
.ss-empty-state h3 {{ color: var(--muted); }}
.ss-profile {{ font-size: 0.85rem; color: var(--muted); padding: 0.5rem 0; }}
.ss-version {{ font-size: 0.72rem; color: var(--muted); opacity: 0.7; }}

div[data-testid="stForm"] {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 14px; padding: 1.5rem; }}
.stButton > button[kind="primary"] {{ background: var(--primary); border: none; border-radius: 8px; }}
</style>
""", unsafe_allow_html=True)


def card(label: str, value: str) -> str:
    return f'<div class="ss-card"><div class="ss-metric-label">{label}</div><div class="ss-metric-value">{value}</div></div>'


def empty_state(title: str, body: str) -> None:
    st.markdown(f'<div class="ss-empty-state"><h3>{title}</h3><p>{body}</p></div>', unsafe_allow_html=True)


def budget_status(pct: float) -> tuple[str, str]:
    """Returns (css_class, label) per the spec's budget-state thresholds."""
    if pct > 1.0:
        return "critical", "Over Budget"
    if pct > 0.9:
        return "critical", "Critical"
    if pct >= 0.7:
        return "warning", "Warning"
    return "normal", "Normal"


def current_month_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    this_month = datetime.now().strftime("%Y-%m")
    return df[pd.to_datetime(df["expense_date"]).dt.strftime("%Y-%m") == this_month]


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------
if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    st.markdown('<div class="ss-brand">💸 SmartSpend AI</div>', unsafe_allow_html=True)
    login_tab, register_tab = st.tabs(["Login", "Create account"])

    with login_tab:
        st.subheader("Welcome back 👋")
        with st.form("login_form"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", type="primary", width='stretch')
        if submitted:
            try:
                user = auth.authenticate(email, password)
                st.session_state.user = {"id": user.id, "name": user.name, "email": user.email}
                st.rerun()
            except auth.AuthError as e:
                st.error(str(e))

    with register_tab:
        st.subheader("Create your account")
        with st.form("register_form"):
            name = st.text_input("Name")
            reg_email = st.text_input("Email", key="reg_email")
            reg_password = st.text_input("Password", type="password", key="reg_password")
            confirm_password = st.text_input("Confirm Password", type="password")
            reg_submitted = st.form_submit_button("Create Account", type="primary", width='stretch')
        if reg_submitted:
            try:
                user = auth.register(name, reg_email, reg_password, confirm_password)
                st.session_state.user = {"id": user.id, "name": user.name, "email": user.email}
                st.success("Account created!")
                st.rerun()
            except auth.AuthError as e:
                st.error(str(e))

    st.stop()

user_id = st.session_state.user["id"]

if not settings.ai_enabled:
    st.info(
        "Running in **offline/demo mode** (no `GEMINI_API_KEY` set) — categorization uses a keyword "
        "fallback, and Ask AI / AI Assistant are disabled. See `.env.example`.", icon="ℹ️",
    )
elif not ai_service.AI_AVAILABLE:
    st.warning("A `GEMINI_API_KEY` was found but Gemini failed to initialize — check the logs.", icon="⚠️")

# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown('<div class="ss-brand">💸 SmartSpend AI</div>', unsafe_allow_html=True)
    page = st.radio(
        "Navigate",
        ["📊 Dashboard", "➕ Add Expense", "📋 Transactions", "🎯 Budgets",
         "📈 Analytics", "🤖 Ask AI", "💬 AI Assistant", "⚙️ Settings"],
        label_visibility="collapsed",
    )
    st.markdown("<br>" * 3, unsafe_allow_html=True)
    st.divider()
    theme_label = "🌙 Dark Mode" if st.session_state.theme == "light" else "☀️ Light Mode"
    if st.button(theme_label, width='stretch'):
        st.session_state.theme = "dark" if st.session_state.theme == "light" else "light"
        st.rerun()
    st.markdown(
        f'<div class="ss-profile"><strong>{st.session_state.user["name"]}</strong><br>{st.session_state.user["email"]}</div>',
        unsafe_allow_html=True,
    )
    if st.button("Logout", width='stretch'):
        st.session_state.user = None
        st.rerun()
    st.markdown(f'<div class="ss-version">SmartSpend AI {APP_VERSION}</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
if page == "📊 Dashboard":
    st.title("Dashboard")
    df = database.load_expenses(user_id)
    budgets = database.load_budgets(user_id)

    if df.empty:
        empty_state("No expenses yet", "Start tracking your spending to see personalized insights here.")
    else:
        this_month = current_month_df(df)
        total_budget = budgets["monthly_limit"].sum() if not budgets.empty else 0
        spent = this_month["amount"].sum()
        remaining = max(total_budget - spent, 0)
        unusual = int(this_month["is_anomaly"].sum())

        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(card("Monthly Spending", f"₹{spent:,.0f}"), unsafe_allow_html=True)
        c2.markdown(card("Monthly Budget", f"₹{total_budget:,.0f}"), unsafe_allow_html=True)
        c3.markdown(card("Remaining", f"₹{remaining:,.0f}"), unsafe_allow_html=True)
        c4.markdown(card("Unusual Expenses", str(unusual)), unsafe_allow_html=True)

        col_left, col_right = st.columns([3, 2])
        with col_left:
            st.subheader("Spending by Category")
            by_cat = df.groupby("category", as_index=False)["amount"].sum()
            st.plotly_chart(px.pie(by_cat, names="category", values="amount", hole=0.45),
                             width='stretch')

            st.subheader("Recent Transactions")
            recent = df.head(6)[["expense_date", "description", "amount", "category", "is_anomaly"]].rename(
                columns={"expense_date": "Date", "description": "Description", "amount": "Amount (₹)",
                          "category": "Category", "is_anomaly": "Status"}
            )
            recent["Status"] = recent["Status"].map({1: "Unusual", 0: "Normal"})
            st.dataframe(recent, width='stretch', hide_index=True)

        with col_right:
            st.subheader("Notifications")
            notes = insights.get_notifications(user_id)
            if not notes:
                st.caption("No notifications right now.")
            for n in notes:
                st.markdown(f'<div class="ss-notification ss-notification-{n.level}">{n.icon} {n.message}</div>',
                             unsafe_allow_html=True)

            st.subheader("Insights")
            for i in insights.get_insights(user_id):
                st.markdown(f"- {i}")

# ---------------------------------------------------------------------------
# Add Expense
# ---------------------------------------------------------------------------
elif page == "➕ Add Expense":
    st.title("Add Expense")

    if "suggested_category" not in st.session_state:
        st.session_state.suggested_category = None

    col1, col2 = st.columns(2)
    with col1:
        expense_date = st.date_input("Date", value=date.today())
        amount = st.number_input("Amount (₹)", min_value=0.0, step=10.0)
    with col2:
        description = st.text_input("Description", placeholder="e.g. Uber from college to home", max_chars=200)
        notes = st.text_input("Notes (optional)", max_chars=200)

    ai_col1, ai_col2 = st.columns([1, 3])
    with ai_col1:
        if st.button("✨ Get AI Suggestion", disabled=not description.strip()):
            with st.spinner("Analyzing expense..."):
                st.session_state.suggested_category = ai_service.categorize(description.strip())
    if st.session_state.suggested_category:
        st.markdown(
            f'<span class="ss-badge ss-badge-normal">🏷️ AI suggests: {st.session_state.suggested_category}</span>',
            unsafe_allow_html=True,
        )

    category = st.selectbox(
        "Category",
        CATEGORIES,
        index=CATEGORIES.index(st.session_state.suggested_category) if st.session_state.suggested_category in CATEGORIES else 0,
    )

    if st.button("Add Expense", type="primary"):
        description = description.strip()
        if not description:
            st.error("Please enter a description.")
        elif amount <= 0:
            st.error("Amount must be greater than 0.")
        else:
            try:
                is_anomaly, reason = ai_service.detect_anomaly(user_id, category, amount)
                full_description = f"{description} ({notes})" if notes.strip() else description
                database.insert_expense(
                    user_id,
                    database.Expense(
                        expense_date=str(expense_date), description=full_description, amount=amount,
                        category=category, is_anomaly=is_anomaly, anomaly_reason=reason,
                    ),
                )
                st.success(f"Added — categorized as **{category}**" + (f" ⚠️ {reason}" if is_anomaly else ""))
                st.session_state.suggested_category = None
                st.rerun()
            except database.DatabaseError:
                logger.exception("Failed to add expense")
                st.error("Something went wrong while saving that expense. Please try again.")

# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
elif page == "📋 Transactions":
    st.title("Transactions")
    df = database.load_expenses(user_id)

    if df.empty:
        empty_state("No expenses yet", "Start tracking your spending to see your transactions here.")
    else:
        f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
        with f1:
            search = st.text_input("Search description")
        with f2:
            cat_filter = st.selectbox("Category", ["All"] + CATEGORIES)
        with f3:
            sort_by = st.selectbox("Sort by", ["Date (newest)", "Date (oldest)", "Amount (high-low)", "Amount (low-high)"])
        with f4:
            st.write("")
            st.write("")
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv, "smartspend_expenses.csv", "text/csv", width='stretch')

        filtered = df.copy()
        if search:
            filtered = filtered[filtered["description"].str.contains(search, case=False, na=False)]
        if cat_filter != "All":
            filtered = filtered[filtered["category"] == cat_filter]
        if sort_by == "Date (newest)":
            filtered = filtered.sort_values("expense_date", ascending=False)
        elif sort_by == "Date (oldest)":
            filtered = filtered.sort_values("expense_date", ascending=True)
        elif sort_by == "Amount (high-low)":
            filtered = filtered.sort_values("amount", ascending=False)
        else:
            filtered = filtered.sort_values("amount", ascending=True)

        display = filtered[["expense_date", "description", "category", "amount", "is_anomaly"]].rename(
            columns={"expense_date": "Date", "description": "Description", "category": "Category",
                      "amount": "Amount (₹)", "is_anomaly": "Status"}
        )
        display["Status"] = display["Status"].map({1: "Unusual", 0: "Normal"})
        st.caption(f"{len(display)} of {len(df)} transactions")
        st.dataframe(display, width='stretch', hide_index=True)

# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
elif page == "🎯 Budgets":
    st.title("Budgets")
    budgets_df = database.load_budgets(user_id)
    budget_map = dict(zip(budgets_df["category"], budgets_df["monthly_limit"])) if not budgets_df.empty else {}

    with st.form("budget_form"):
        c1, c2 = st.columns(2)
        with c1:
            b_cat = st.selectbox("Category", CATEGORIES)
        with c2:
            b_limit = st.number_input("Monthly limit (₹)", min_value=0.0, step=500.0,
                                       value=float(budget_map.get(b_cat, 0)))
        if st.form_submit_button("Save Budget", type="primary"):
            database.set_budget(user_id, b_cat, b_limit)
            st.success(f"Budget for {b_cat} set to ₹{b_limit:,.0f}/month")
            st.rerun()

    st.divider()
    if not budget_map:
        empty_state("No budgets set yet", "Set a monthly limit for a category above to start tracking it.")
    else:
        df = database.load_expenses(user_id)
        this_month = current_month_df(df)
        for cat, limit in budget_map.items():
            spent = this_month[this_month["category"] == cat]["amount"].sum() if not this_month.empty else 0
            pct = spent / limit if limit > 0 else 0
            css_class, label = budget_status(pct)
            st.markdown(
                f'<div class="ss-card"><strong>{cat}</strong> '
                f'<span class="ss-badge ss-badge-{"unusual" if css_class == "critical" else "normal"}">{label}</span>'
                f'<br>₹{spent:,.0f} / ₹{limit:,.0f} ({pct*100:.0f}%)</div>',
                unsafe_allow_html=True,
            )
            st.progress(min(pct, 1.0))

# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
elif page == "📈 Analytics":
    st.title("Analytics")
    df = database.load_expenses(user_id)

    if df.empty:
        empty_state("No data yet", "Add some expenses to see analytics here.")
    else:
        df["month"] = pd.to_datetime(df["expense_date"]).dt.strftime("%Y-%m")
        by_month = df.groupby("month", as_index=False)["amount"].sum().sort_values("month")
        st.subheader("Monthly Spending Trend")
        st.plotly_chart(px.bar(by_month, x="month", y="amount"), width='stretch')

        st.subheader("Top 5 Largest Expenses")
        top5 = df.nlargest(5, "amount")[["expense_date", "description", "category", "amount"]].rename(
            columns={"expense_date": "Date", "description": "Description", "category": "Category", "amount": "Amount (₹)"}
        )
        st.dataframe(top5, width='stretch', hide_index=True)

        st.subheader("Insights")
        ins = insights.get_insights(user_id)
        if not ins:
            st.caption("Not enough history yet for month-over-month insights.")
        for i in ins:
            st.markdown(f"- {i}")

# ---------------------------------------------------------------------------
# Ask AI (text-to-SQL)
# ---------------------------------------------------------------------------
elif page == "🤖 Ask AI":
    st.title("Ask AI")
    st.caption('Try: "How much did I spend on food this month?" or "What was my biggest expense?"')

    if not ai_service.AI_AVAILABLE:
        st.warning("This feature needs a working `GEMINI_API_KEY`.")
    else:
        question = st.text_input("Your question")
        if st.button("Ask") and question.strip():
            with st.spinner("Thinking..."):
                result = ai_service.answer_question(user_id, question.strip())
            if result.error:
                st.error(result.error)
            if result.summary:
                st.success(result.summary)
            if result.sql:
                with st.expander("See the query I ran"):
                    st.code(result.sql, language="sql")
            if result.result_df is not None and not result.result_df.empty:
                st.dataframe(result.result_df, width='stretch', hide_index=True)

# ---------------------------------------------------------------------------
# AI Assistant (chatbot)
# ---------------------------------------------------------------------------
elif page == "💬 AI Assistant":
    st.title("🤖 AI Financial Assistant")

    history = chatbot.get_history(user_id)

    if not history:
        st.caption("Ask me anything about your spending — try one of these:")
        suggestions = ["How am I doing this month?", "Where am I overspending?",
                        "Show me my biggest expenses.", "Give me 3 ways to save money."]
        cols = st.columns(len(suggestions))
        for col, s in zip(cols, suggestions):
            if col.button(s, width='stretch'):
                with st.spinner("Thinking..."):
                    chatbot.send_message(user_id, s)
                st.rerun()
    else:
        for msg in history:
            avatar = "🧑" if msg["role"] == "user" else "🤖"
            with st.chat_message(msg["role"], avatar=avatar):
                st.write(msg["message"])
                st.caption(msg["created_at"])

        if st.button("🗑️ Clear Conversation"):
            chatbot.clear_history(user_id)
            st.rerun()

    prompt = st.chat_input("Ask about your spending...")
    if prompt:
        with st.spinner("Thinking..."):
            chatbot.send_message(user_id, prompt)
        st.rerun()

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
elif page == "⚙️ Settings":
    st.title("Settings")
    st.markdown(
        f'<div class="ss-card"><strong>{st.session_state.user["name"]}</strong><br>'
        f'{st.session_state.user["email"]}</div>',
        unsafe_allow_html=True,
    )
    st.caption(f"AI features: {'enabled' if ai_service.AI_AVAILABLE else 'disabled (no GEMINI_API_KEY)'}")
    st.caption(f"SmartSpend AI {APP_VERSION}")
