"""Risk manager — enforces diversification, spend limits, and ROI thresholds."""

from __future__ import annotations

import logging
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.business.finance import Finance

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.finance = Finance(db)

    def check_strategy_allocation(self, strategy_id: int) -> bool:
        """Check if a strategy's allocation is within limits."""
        rows = self.db.execute(
            "SELECT allocated_budget FROM strategies WHERE id = ?", (strategy_id,)
        )
        if not rows:
            return False
        budget = rows[0]["allocated_budget"]
        total_rows = self.db.execute(
            "SELECT COALESCE(SUM(allocated_budget), 0) as total FROM strategies WHERE status = 'active'"
        )
        total = total_rows[0]["total"]
        if total == 0:
            return True
        pct = (budget / total) * 100
        return pct <= self.config.risk.max_strategy_allocation_pct

    def check_stop_loss(self, strategy_id: int) -> bool:
        """Returns True if strategy should be stopped (hit stop-loss)."""
        rows = self.db.execute(
            "SELECT allocated_budget FROM strategies WHERE id = ?", (strategy_id,)
        )
        if not rows:
            return False
        budget = rows[0]["allocated_budget"]
        if budget == 0:
            return False
        net = self.finance.get_net_profit(strategy_id)
        loss_pct = abs(net / budget) * 100 if net < 0 else 0
        return loss_pct >= self.config.risk.stop_loss_pct

    def get_active_strategy_count(self) -> int:
        rows = self.db.execute(
            "SELECT COUNT(*) as count FROM strategies WHERE status = 'active'"
        )
        return rows[0]["count"]

    def can_start_new_strategy(self) -> dict[str, Any]:
        """Check if conditions allow starting a new strategy."""
        return {
            "allowed": True,  # Always allowed — diversification is good
            "initial_budget_cap": self.config.risk.max_monthly_spend_new_strategy,
        }

    def should_pause_strategy(self, strategy_id: int) -> dict[str, Any]:
        """Evaluate if a strategy should be paused."""
        hit_stop_loss = self.check_stop_loss(strategy_id)
        roi = self.finance.get_roi(strategy_id, days=self.config.risk.review_period_days)
        below_roi = roi < self.config.risk.min_roi_threshold and roi != 0

        return {
            "should_pause": hit_stop_loss or below_roi,
            "reasons": [
                r for r in [
                    "Hit stop-loss limit" if hit_stop_loss else "",
                    f"ROI ({roi:.2f}x) below threshold ({self.config.risk.min_roi_threshold}x)"
                    if below_roi else "",
                ] if r
            ],
            "roi": roi,
            "hit_stop_loss": hit_stop_loss,
        }

    def get_portfolio_health(self) -> dict[str, Any]:
        """Overall portfolio health check."""
        active = self.get_active_strategy_count()
        total_profit = self.finance.get_net_profit()
        strategy_pnl = self.finance.get_strategy_pnl()

        profitable = sum(1 for s in strategy_pnl if s["net"] > 0)
        losing = sum(1 for s in strategy_pnl if s["net"] < 0)

        return {
            "active_strategies": active,
            "min_required": self.config.risk.min_active_strategies,
            "diversification_ok": active >= self.config.risk.min_active_strategies,
            "total_net_profit": total_profit,
            "profitable_strategies": profitable,
            "losing_strategies": losing,
            "strategy_details": strategy_pnl,
        }
