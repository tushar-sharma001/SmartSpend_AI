"""
Deterministic spending insights and notifications.

Everything here is plain pandas/Python arithmetic, deliberately with no
Gemini calls — totals, percentages, and month-over-month comparisons don't
need an LLM, and calling one for them would only add latency, cost, and a
failure point for something a spreadsheet formula already does correctly.
"""

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from src.database import load_budgets, load_expenses


@dataclass(frozen=True)
class Notification:
    level: str  # "info" | "warning" | "critical"
    icon: str
    message: str


def _month_str(offset_months: int = 0) -> str:
    now = datetime.now()
    month = now.month - offset_months
    year = now.year
    while month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}"


def _filter_month(df: pd.DataFrame, month_str: str) -> pd.DataFrame:
    if df.empty:
        return df
    return df[pd.to_datetime(df["expense_date"]).dt.strftime("%Y-%m") == month_str]


def get_notifications(user_id: int) -> list[Notification]:
    """Budget-threshold and anomaly notifications for the current month."""
    notifications: list[Notification] = []
    df = load_expenses(user_id)
    budgets = load_budgets(user_id)
    this_month = _filter_month(df, _month_str())

    budget_map = dict(zip(budgets["category"], budgets["monthly_limit"])) if not budgets.empty else {}
    for category, limit in budget_map.items():
        if limit <= 0:
            continue
        spent = this_month[this_month["category"] == category]["amount"].sum() if not this_month.empty else 0
        pct = spent / limit
        if pct > 1.0:
            notifications.append(Notification(
                "critical", "🚨",
                f"Your {category} budget has been exceeded by ₹{spent - limit:,.0f}."
            ))
        elif pct >= 0.9:
            notifications.append(Notification(
                "warning", "⚠️", f"You've used {pct * 100:.0f}% of your {category} budget."
            ))
        elif pct >= 0.7:
            notifications.append(Notification(
                "info", "🔔", f"You've used {pct * 100:.0f}% of your {category} budget."
            ))

    if not this_month.empty:
        anomalies = this_month[this_month["is_anomaly"] == 1]
        for _, row in anomalies.iterrows():
            notifications.append(Notification(
                "warning", "🔔",
                f"Unusual expense detected: ₹{row['amount']:,.0f} — {row['category']}"
            ))

    return notifications


def get_insights(user_id: int) -> list[str]:
    """A handful of short, deterministic observations about this user's spending."""
    insights: list[str] = []
    df = load_expenses(user_id)
    if df.empty:
        return insights

    this_month_str, last_month_str = _month_str(), _month_str(1)
    this_month = _filter_month(df, this_month_str)
    last_month = _filter_month(df, last_month_str)

    if not this_month.empty:
        top_category = this_month.groupby("category")["amount"].sum().idxmax()
        insights.append(f"💡 {top_category} is currently your highest spending category this month.")

    if not this_month.empty and not last_month.empty:
        this_by_cat = this_month.groupby("category")["amount"].sum()
        last_by_cat = last_month.groupby("category")["amount"].sum()
        for category in this_by_cat.index:
            if category in last_by_cat.index and last_by_cat[category] > 0:
                change_pct = (this_by_cat[category] - last_by_cat[category]) / last_by_cat[category] * 100
                if change_pct > 15:
                    insights.append(f"💡 {category} spending is {change_pct:.0f}% higher than last month.")
                elif change_pct < -15:
                    insights.append(f"💡 {category} spending is {abs(change_pct):.0f}% lower than last month.")

        total_this, total_last = this_by_cat.sum(), last_by_cat.sum()
        if total_last > 0:
            overall_change = (total_this - total_last) / total_last * 100
            if abs(overall_change) > 10:
                direction = "up" if overall_change > 0 else "down"
                insights.append(f"💡 Overall spending is {direction} {abs(overall_change):.0f}% vs. last month.")

    budgets = load_budgets(user_id)
    if not budgets.empty and not this_month.empty:
        total_budget = budgets["monthly_limit"].sum()
        total_spent = this_month["amount"].sum()
        if total_budget > 0:
            pct = total_spent / total_budget * 100
            insights.append(f"💡 You've used {pct:.0f}% of your total monthly budget.")

    return insights[:5]  # keep the dashboard readable, not a wall of bullet points
