"""Spending Guard — enforces financial limits from ReinvestmentConfig.

Tracks daily spending and blocks transactions that exceed configured caps:
- max_daily_spend: Hard daily cap across all strategies
- max_single_transaction: Max per individual transaction
- require_approval_above: Alert creator via Telegram above this threshold
- max_strategy_boost: Max reinvestment per strategy per cycle
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

SPENDING_GUARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS spending_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    category TEXT NOT NULL,       -- reinvestment, expense, strategy_boost, sweep
    strategy_id INTEGER,
    brand TEXT,
    description TEXT,
    approved INTEGER DEFAULT 1,   -- 1 = auto-approved, 0 = blocked
    block_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_spending_log_date
    ON spending_log(created_at);
CREATE INDEX IF NOT EXISTS idx_spending_log_category
    ON spending_log(category);
"""


class SpendingGuard:
    """Enforces spending limits defined in ReinvestmentConfig.

    Every outgoing transaction (reinvestment, expense, strategy boost)
    must pass through check_and_record() before execution.
    """

    def __init__(self, db: Database, config: Any):
        self.db = db
        self.config = config
        self._init_schema()

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(SPENDING_GUARD_SCHEMA)

    @property
    def _reinvest_cfg(self):
        return self.config.reinvestment

    def check_and_record(
        self,
        amount: float,
        category: str,
        currency: str = "EUR",
        strategy_id: int | None = None,
        brand: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Check if a transaction is within limits and record it.

        Returns:
            Dict with 'allowed' (bool), 'reason' (str if blocked),
            and 'requires_approval' (bool if above approval threshold).
        """
        # Validate amount
        if amount <= 0:
            return {"allowed": False, "reason": f"Invalid amount: {amount}"}

        # Check single transaction cap
        max_single = self._reinvest_cfg.max_single_transaction
        if amount > max_single:
            self._log_blocked(
                amount, currency, category, strategy_id, brand,
                description, f"Exceeds max_single_transaction ({max_single})",
            )
            return {
                "allowed": False,
                "reason": f"Amount {amount:.2f} exceeds single transaction cap of {max_single:.2f}",
            }

        # Check daily cap
        daily_total = self.get_daily_spend()
        max_daily = self._reinvest_cfg.max_daily_spend
        if daily_total + amount > max_daily:
            remaining = max(0, max_daily - daily_total)
            self._log_blocked(
                amount, currency, category, strategy_id, brand,
                description,
                f"Would exceed max_daily_spend ({max_daily}). "
                f"Already spent: {daily_total:.2f}, remaining: {remaining:.2f}",
            )
            return {
                "allowed": False,
                "reason": (
                    f"Daily spend would be {daily_total + amount:.2f}, "
                    f"exceeding cap of {max_daily:.2f}. "
                    f"Remaining today: {remaining:.2f}"
                ),
            }

        # Check strategy boost cap (if this is a strategy reinvestment)
        if category == "strategy_boost" and strategy_id is not None:
            max_boost = self._reinvest_cfg.max_strategy_boost
            strategy_boosted = self.get_strategy_boost_today(strategy_id)
            if strategy_boosted + amount > max_boost:
                self._log_blocked(
                    amount, currency, category, strategy_id, brand,
                    description,
                    f"Would exceed max_strategy_boost ({max_boost}) for strategy {strategy_id}",
                )
                return {
                    "allowed": False,
                    "reason": (
                        f"Strategy {strategy_id} boost would be "
                        f"{strategy_boosted + amount:.2f}, "
                        f"exceeding cap of {max_boost:.2f}"
                    ),
                }

        # Check approval threshold
        requires_approval = amount > self._reinvest_cfg.require_approval_above

        # Record the approved spend
        self.db.execute_insert(
            "INSERT INTO spending_log "
            "(amount, currency, category, strategy_id, brand, description, approved) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (amount, currency, category, strategy_id, brand, description),
        )

        result: dict[str, Any] = {"allowed": True, "requires_approval": requires_approval}
        if requires_approval:
            result["reason"] = (
                f"Amount {amount:.2f} exceeds approval threshold "
                f"of {self._reinvest_cfg.require_approval_above:.2f}. "
                f"Creator notification required."
            )
            logger.warning(
                f"SpendingGuard: transaction {amount:.2f} {currency} requires creator approval"
            )

        return result

    def get_daily_spend(self, date: str | None = None) -> float:
        """Get total approved spend for a given day (default: today)."""
        date = date or datetime.now().strftime("%Y-%m-%d")
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM spending_log "
            "WHERE DATE(created_at) = ? AND approved = 1",
            (date,),
        )
        return float(rows[0]["total"]) if rows else 0.0

    def get_strategy_boost_today(self, strategy_id: int) -> float:
        """Get total strategy boost spend for today."""
        today = datetime.now().strftime("%Y-%m-%d")
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM spending_log "
            "WHERE DATE(created_at) = ? AND strategy_id = ? "
            "AND category = 'strategy_boost' AND approved = 1",
            (today, strategy_id),
        )
        return float(rows[0]["total"]) if rows else 0.0

    def get_spending_summary(self, days: int = 7) -> dict[str, Any]:
        """Get spending summary for the last N days."""
        rows = self.db.execute(
            "SELECT DATE(created_at) as date, "
            "SUM(amount) as total, COUNT(*) as transactions "
            "FROM spending_log WHERE approved = 1 "
            "AND created_at >= datetime('now', ?) "
            "GROUP BY DATE(created_at) ORDER BY date DESC",
            (f"-{days} days",),
        )
        daily = [dict(r) for r in rows]

        blocked = self.db.execute(
            "SELECT COUNT(*) as cnt FROM spending_log "
            "WHERE approved = 0 AND created_at >= datetime('now', ?)",
            (f"-{days} days",),
        )
        blocked_count = blocked[0]["cnt"] if blocked else 0

        return {
            "daily_breakdown": daily,
            "total_spent": sum(d["total"] for d in daily),
            "total_transactions": sum(d["transactions"] for d in daily),
            "blocked_transactions": blocked_count,
            "limits": {
                "max_daily_spend": self._reinvest_cfg.max_daily_spend,
                "max_single_transaction": self._reinvest_cfg.max_single_transaction,
                "require_approval_above": self._reinvest_cfg.require_approval_above,
                "max_strategy_boost": self._reinvest_cfg.max_strategy_boost,
            },
        }

    def _log_blocked(
        self,
        amount: float,
        currency: str,
        category: str,
        strategy_id: int | None,
        brand: str,
        description: str,
        reason: str,
    ) -> None:
        """Log a blocked transaction."""
        self.db.execute_insert(
            "INSERT INTO spending_log "
            "(amount, currency, category, strategy_id, brand, "
            "description, approved, block_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (amount, currency, category, strategy_id, brand, description, reason),
        )
        logger.warning(
            f"SpendingGuard BLOCKED: {amount:.2f} {currency} "
            f"({category}) — {reason}"
        )
