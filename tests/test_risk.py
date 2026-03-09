"""Tests for monai.business.risk."""

import pytest

from monai.business.risk import RiskManager


class TestRiskManager:
    @pytest.fixture
    def risk(self, config, db):
        return RiskManager(config, db)

    def _add_strategy(self, db, name, budget, status="active"):
        return db.execute_insert(
            "INSERT INTO strategies (name, category, allocated_budget, status) VALUES (?, ?, ?, ?)",
            (name, "test", budget, status),
        )

    def _add_transaction(self, db, tx_type, amount, strategy_id=None):
        db.execute_insert(
            "INSERT INTO transactions (strategy_id, type, category, amount) VALUES (?, ?, 'misc', ?)",
            (strategy_id, tx_type, amount),
        )

    def test_allocation_within_limits(self, risk, db):
        s1 = self._add_strategy(db, "s1", 100)
        s2 = self._add_strategy(db, "s2", 100)
        s3 = self._add_strategy(db, "s3", 100)
        s4 = self._add_strategy(db, "s4", 100)
        # Each is 25% of 400 total — under 30% limit
        assert risk.check_strategy_allocation(s1) is True

    def test_allocation_exceeds_limit(self, risk, db):
        s1 = self._add_strategy(db, "s1", 350)
        self._add_strategy(db, "s2", 50)
        self._add_strategy(db, "s3", 50)
        # s1 = 350/450 ≈ 78% — exceeds 30% limit
        assert risk.check_strategy_allocation(s1) is False

    def test_stop_loss_not_triggered(self, risk, db):
        sid = self._add_strategy(db, "safe", 100)
        self._add_transaction(db, "expense", 5, sid)
        # 5% loss — under 15% stop-loss
        assert risk.check_stop_loss(sid) is False

    def test_stop_loss_triggered(self, risk, db):
        sid = self._add_strategy(db, "losing", 100)
        self._add_transaction(db, "expense", 20, sid)
        # 20% loss — exceeds 15% stop-loss
        assert risk.check_stop_loss(sid) is True

    def test_active_strategy_count(self, risk, db):
        self._add_strategy(db, "a1", 10)
        self._add_strategy(db, "a2", 10)
        self._add_strategy(db, "paused", 10, status="paused")
        assert risk.get_active_strategy_count() == 2

    def test_can_start_new_strategy(self, risk):
        result = risk.can_start_new_strategy()
        assert result["allowed"] is True
        assert result["initial_budget_cap"] == 10.0

    def test_should_pause_strategy_healthy(self, risk, db):
        sid = self._add_strategy(db, "healthy", 100)
        self._add_transaction(db, "revenue", 200, sid)
        self._add_transaction(db, "expense", 10, sid)
        result = risk.should_pause_strategy(sid)
        assert result["should_pause"] is False

    def test_portfolio_health(self, risk, db):
        self._add_strategy(db, "s1", 100)
        self._add_strategy(db, "s2", 100)
        health = risk.get_portfolio_health()
        assert health["active_strategies"] == 2
        assert health["min_required"] == 3
        assert health["diversification_ok"] is False  # Only 2 < 3 required

    def test_nonexistent_strategy(self, risk):
        assert risk.check_strategy_allocation(9999) is False
        assert risk.check_stop_loss(9999) is False
