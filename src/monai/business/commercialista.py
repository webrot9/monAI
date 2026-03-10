"""Commercialista (accountant) — tracks every euro in and out.

Two levels:
1. Per-agent commercialista: each agent's own cost/revenue tracking
2. Senior commercialista: project-level P&L, budget enforcement, sustainability check

The system MUST become self-sustaining. API costs must be covered by revenue.
Initial budget: €500.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import get_cost_tracker

logger = logging.getLogger(__name__)

COMMERCIALISTA_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    initial_amount REAL NOT NULL,
    current_balance REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    cost_type TEXT NOT NULL,      -- api_call, subscription, tool, platform_fee, other
    model TEXT,                   -- for api_call: which model
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_eur REAL NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Commercialista:
    """Senior project accountant — manages the entire financial picture."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._init_schema()
        self._ensure_budget()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(COMMERCIALISTA_SCHEMA)

    def _ensure_budget(self):
        """Initialize budget if not set."""
        rows = self.db.execute("SELECT * FROM budget LIMIT 1")
        if not rows:
            initial = self.config.initial_capital
            self.db.execute_insert(
                "INSERT INTO budget (initial_amount, current_balance, currency) VALUES (?, ?, ?)",
                (initial, initial, self.config.currency),
            )
            logger.info(f"Budget initialized: €{initial:.2f}")

    # ── Budget Management ───────────────────────────────────────

    def get_budget(self) -> dict[str, Any]:
        """Get current budget status."""
        rows = self.db.execute("SELECT * FROM budget LIMIT 1")
        if not rows:
            return {"initial": 0, "balance": 0, "currency": "EUR"}
        b = dict(rows[0])

        # Calculate actual balance from transactions
        revenue = self._sum_transactions("revenue")
        expenses = self._sum_transactions("expense")
        actual_balance = b["initial_amount"] + revenue - expenses

        return {
            "initial": b["initial_amount"],
            "balance": round(actual_balance, 2),
            "revenue": round(revenue, 2),
            "expenses": round(expenses, 2),
            "net_profit": round(revenue - expenses, 2),
            "currency": b["currency"],
            "self_sustaining": revenue >= expenses,
            "burn_rate_daily": round(self._get_daily_burn_rate(), 4),
            "days_until_broke": self._days_until_broke(actual_balance),
        }

    def can_spend(self, amount: float) -> bool:
        """Check if we can afford to spend this amount."""
        budget = self.get_budget()
        return budget["balance"] >= amount

    def get_remaining_budget(self) -> float:
        """Get remaining budget in EUR."""
        return self.get_budget()["balance"]

    # ── Cost Logging ────────────────────────────────────────────

    def log_api_cost(self, agent_name: str, model: str, input_tokens: int,
                     output_tokens: int, cost_eur: float, description: str = ""):
        """Log an API call cost."""
        self.db.execute_insert(
            "INSERT INTO cost_log (agent_name, cost_type, model, input_tokens, "
            "output_tokens, cost_eur, description) VALUES (?, 'api_call', ?, ?, ?, ?, ?)",
            (agent_name, model, input_tokens, output_tokens, cost_eur, description),
        )

    def log_expense(self, agent_name: str, cost_type: str, cost_eur: float,
                    description: str = ""):
        """Log a non-API expense."""
        self.db.execute_insert(
            "INSERT INTO cost_log (agent_name, cost_type, cost_eur, description) "
            "VALUES (?, ?, ?, ?)",
            (agent_name, cost_type, cost_eur, description),
        )

    # ── Reports ─────────────────────────────────────────────────

    def get_cost_by_agent(self, days: int | None = None) -> list[dict[str, Any]]:
        """Get total costs broken down by agent."""
        query = """
            SELECT agent_name,
                   COUNT(*) as calls,
                   SUM(cost_eur) as total_cost,
                   SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output
            FROM cost_log
        """
        params: list = []
        if days:
            query += " WHERE created_at >= ?"
            params.append((datetime.now() - timedelta(days=days)).isoformat())
        query += " GROUP BY agent_name ORDER BY total_cost DESC"
        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def get_cost_by_model(self, days: int | None = None) -> list[dict[str, Any]]:
        """Get costs broken down by model."""
        query = """
            SELECT model, COUNT(*) as calls, SUM(cost_eur) as total_cost,
                   SUM(input_tokens) as total_input, SUM(output_tokens) as total_output
            FROM cost_log WHERE model IS NOT NULL
        """
        params: list = []
        if days:
            query += " AND created_at >= ?"
            params.append((datetime.now() - timedelta(days=days)).isoformat())
        query += " GROUP BY model ORDER BY total_cost DESC"
        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def get_daily_costs(self, days: int = 30) -> list[dict[str, Any]]:
        """Get daily cost totals."""
        rows = self.db.execute(
            "SELECT DATE(created_at) as date, SUM(cost_eur) as cost, COUNT(*) as calls "
            "FROM cost_log WHERE created_at >= ? "
            "GROUP BY DATE(created_at) ORDER BY date DESC",
            ((datetime.now() - timedelta(days=days)).isoformat(),),
        )
        return [dict(r) for r in rows]

    def get_full_report(self) -> dict[str, Any]:
        """Complete financial report for the creator."""
        budget = self.get_budget()
        api_tracker = get_cost_tracker().get_summary()

        return {
            "budget": budget,
            "api_costs_session": api_tracker,
            "costs_by_agent": self.get_cost_by_agent(),
            "costs_by_model": self.get_cost_by_model(),
            "daily_costs": self.get_daily_costs(days=7),
            "sustainability": {
                "self_sustaining": budget["self_sustaining"],
                "burn_rate_daily_eur": budget["burn_rate_daily"],
                "days_until_broke": budget["days_until_broke"],
                "recommendation": self._get_recommendation(budget),
            },
        }

    # ── Internal ────────────────────────────────────────────────

    def _sum_transactions(self, tx_type: str) -> float:
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM transactions WHERE type = ?",
            (tx_type,),
        )
        return rows[0]["total"]

    def _get_daily_burn_rate(self) -> float:
        """Average daily expense over last 30 days."""
        rows = self.db.execute(
            "SELECT COALESCE(SUM(cost_eur), 0) as total FROM cost_log "
            "WHERE created_at >= ?",
            ((datetime.now() - timedelta(days=30)).isoformat(),),
        )
        total = rows[0]["total"]
        return total / 30

    def _days_until_broke(self, balance: float) -> int | None:
        """Estimate days until budget runs out at current burn rate."""
        burn = self._get_daily_burn_rate()
        if burn <= 0:
            return None  # Not burning money
        if balance <= 0:
            return 0
        return int(balance / burn)

    def _get_recommendation(self, budget: dict) -> str:
        if budget["balance"] <= 0:
            return "CRITICAL: Budget exhausted. Pause all spending. Focus on revenue."
        if budget.get("days_until_broke") and budget["days_until_broke"] < 7:
            return "WARNING: Less than 7 days of budget left. Reduce API usage, prioritize revenue."
        if budget["self_sustaining"]:
            return "HEALTHY: Revenue covers expenses. Continue scaling."
        if budget["balance"] > budget["initial"] * 0.5:
            return "OK: Spending within budget. Focus on generating first revenue."
        return "CAUTION: Over 50% of budget spent. Prioritize revenue-generating activities."

    # ── Auto-Reinvestment Engine ─────────────────────────────────

    def compute_reinvestment(self) -> dict[str, Any]:
        """Compute profit allocation: reinvest, reserve, and creator sweep.

        Uses config.reinvestment settings to split net profit into:
        - reinvest_pct → goes back to strategy budgets
        - reserve_pct → kept as safety buffer
        - creator_pct → swept to creator wallet/bank
        """
        reinv = self.config.reinvestment
        if not reinv.enabled:
            return {"status": "disabled"}

        budget = self.get_budget()
        net_profit = budget["net_profit"]

        if net_profit < reinv.min_profit_to_reinvest:
            return {
                "status": "below_threshold",
                "net_profit": net_profit,
                "threshold": reinv.min_profit_to_reinvest,
            }

        # Split the profit
        reinvest_amount = round(net_profit * (reinv.reinvest_pct / 100), 2)
        reserve_amount = round(net_profit * (reinv.reserve_pct / 100), 2)
        creator_amount = round(net_profit * (reinv.creator_pct / 100), 2)

        return {
            "status": "ready",
            "net_profit": net_profit,
            "reinvest": reinvest_amount,
            "reserve": reserve_amount,
            "creator_sweep": creator_amount,
            "split_pct": {
                "reinvest": reinv.reinvest_pct,
                "reserve": reinv.reserve_pct,
                "creator": reinv.creator_pct,
            },
        }

    def allocate_to_strategies(self, reinvest_amount: float,
                               strategy_performance: list[dict]) -> list[dict[str, Any]]:
        """Allocate reinvestment budget across strategies based on performance.

        Args:
            reinvest_amount: Total EUR to distribute
            strategy_performance: List of dicts with keys:
                - name: strategy name
                - revenue: total revenue
                - expenses: total expenses
                - roi: return on investment ratio

        Returns:
            List of allocations: [{strategy, amount, reason}]
        """
        reinv = self.config.reinvestment
        if not strategy_performance:
            return []

        allocations = []
        remaining = reinvest_amount

        # Separate winners and losers
        winners = sorted(
            [s for s in strategy_performance if s.get("roi", 0) > 1.0],
            key=lambda s: s.get("roi", 0),
            reverse=True,
        )
        losers = [s for s in strategy_performance if s.get("roi", 0) < 0.5]
        neutral = [s for s in strategy_performance
                   if 0.5 <= s.get("roi", 0) <= 1.0]

        # Cut losers first (free up budget)
        if reinv.cut_losers and losers:
            for loser in losers:
                allocations.append({
                    "strategy": loser["name"],
                    "amount": 0,
                    "action": "reduce",
                    "reason": f"ROI {loser.get('roi', 0):.2f}x — below threshold",
                })

        # Scale winners
        if reinv.scale_winners and winners and remaining > 0:
            # Weight by ROI — higher ROI gets proportionally more
            total_roi = sum(w.get("roi", 1) for w in winners)
            for winner in winners:
                share = (winner.get("roi", 1) / total_roi) * remaining
                amount = min(share, reinv.max_strategy_boost)
                amount = round(amount, 2)
                allocations.append({
                    "strategy": winner["name"],
                    "amount": amount,
                    "action": "boost",
                    "reason": f"ROI {winner.get('roi', 0):.2f}x — scaling up",
                })
                remaining -= amount

        # Distribute remainder evenly to neutral strategies
        if neutral and remaining > 0:
            per_neutral = round(remaining / len(neutral), 2)
            for n in neutral:
                amount = min(per_neutral, reinv.max_strategy_boost)
                allocations.append({
                    "strategy": n["name"],
                    "amount": amount,
                    "action": "maintain",
                    "reason": f"ROI {n.get('roi', 0):.2f}x — steady growth",
                })

        logger.info(f"Reinvestment allocated: €{reinvest_amount:.2f} across "
                    f"{len(allocations)} strategies")
        return allocations
