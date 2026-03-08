"""Finance module — tracks all money in and out."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from monai.db.database import Database


class Finance:
    def __init__(self, db: Database):
        self.db = db

    def get_total_revenue(self, strategy_id: int | None = None,
                          days: int | None = None) -> float:
        query = "SELECT COALESCE(SUM(amount), 0) as total FROM transactions WHERE type = 'revenue'"
        params: list = []
        if strategy_id:
            query += " AND strategy_id = ?"
            params.append(strategy_id)
        if days:
            since = (datetime.now() - timedelta(days=days)).isoformat()
            query += " AND created_at >= ?"
            params.append(since)
        rows = self.db.execute(query, tuple(params))
        return rows[0]["total"]

    def get_total_expenses(self, strategy_id: int | None = None,
                           days: int | None = None) -> float:
        query = "SELECT COALESCE(SUM(amount), 0) as total FROM transactions WHERE type = 'expense'"
        params: list = []
        if strategy_id:
            query += " AND strategy_id = ?"
            params.append(strategy_id)
        if days:
            since = (datetime.now() - timedelta(days=days)).isoformat()
            query += " AND created_at >= ?"
            params.append(since)
        rows = self.db.execute(query, tuple(params))
        return rows[0]["total"]

    def get_net_profit(self, strategy_id: int | None = None,
                       days: int | None = None) -> float:
        return self.get_total_revenue(strategy_id, days) - self.get_total_expenses(strategy_id, days)

    def get_strategy_pnl(self) -> list[dict[str, Any]]:
        rows = self.db.execute("""
            SELECT
                s.id, s.name, s.category,
                COALESCE(SUM(CASE WHEN t.type = 'revenue' THEN t.amount ELSE 0 END), 0) as revenue,
                COALESCE(SUM(CASE WHEN t.type = 'expense' THEN t.amount ELSE 0 END), 0) as expenses,
                COALESCE(SUM(CASE WHEN t.type = 'revenue' THEN t.amount ELSE -t.amount END), 0) as net
            FROM strategies s
            LEFT JOIN transactions t ON t.strategy_id = s.id
            GROUP BY s.id
            ORDER BY net DESC
        """)
        return [dict(r) for r in rows]

    def get_daily_summary(self, date: str | None = None) -> dict[str, float]:
        date = date or datetime.now().strftime("%Y-%m-%d")
        rows = self.db.execute(
            "SELECT type, COALESCE(SUM(amount), 0) as total "
            "FROM transactions WHERE DATE(created_at) = ? GROUP BY type",
            (date,),
        )
        result = {"revenue": 0.0, "expenses": 0.0, "net": 0.0}
        for row in rows:
            result[row["type"]] = row["total"]
        result["net"] = result["revenue"] - result["expenses"]
        return result

    def get_roi(self, strategy_id: int | None = None, days: int | None = None) -> float:
        expenses = self.get_total_expenses(strategy_id, days)
        if expenses == 0:
            return 0.0
        revenue = self.get_total_revenue(strategy_id, days)
        return revenue / expenses
