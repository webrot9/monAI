"""Tests for SpendingGuard — spending cap enforcement."""

from unittest.mock import MagicMock

import pytest

from monai.business.spending_guard import SpendingGuard
from monai.config import ReinvestmentConfig


@pytest.fixture
def guard(db):
    config = MagicMock()
    config.reinvestment = ReinvestmentConfig(
        max_daily_spend=100.0,
        max_single_transaction=200.0,
        require_approval_above=500.0,
        max_strategy_boost=50.0,
    )
    return SpendingGuard(db, config)


class TestSpendingGuard:
    def test_allows_normal_transaction(self, guard):
        result = guard.check_and_record(25.0, "expense", description="API cost")
        assert result["allowed"] is True
        assert result.get("requires_approval") is False

    def test_blocks_negative_amount(self, guard):
        result = guard.check_and_record(-10.0, "expense")
        assert result["allowed"] is False
        assert "Invalid" in result["reason"]

    def test_blocks_zero_amount(self, guard):
        result = guard.check_and_record(0.0, "expense")
        assert result["allowed"] is False

    def test_blocks_single_transaction_over_cap(self, guard):
        result = guard.check_and_record(250.0, "expense", description="Big purchase")
        assert result["allowed"] is False
        assert "single transaction cap" in result["reason"]

    def test_blocks_daily_spend_over_cap(self, guard):
        # Spend 90 first (allowed)
        r1 = guard.check_and_record(90.0, "expense", description="Spend 1")
        assert r1["allowed"] is True

        # Try to spend 20 more (would exceed 100 daily cap)
        r2 = guard.check_and_record(20.0, "expense", description="Spend 2")
        assert r2["allowed"] is False
        assert "Daily spend" in r2["reason"]
        assert "cap of 100.00" in r2["reason"]

    def test_blocks_strategy_boost_over_cap(self, guard):
        # Boost strategy 1 by 45 (allowed)
        r1 = guard.check_and_record(
            45.0, "strategy_boost", strategy_id=1, description="Boost"
        )
        assert r1["allowed"] is True

        # Try to boost same strategy by 10 more (would exceed 50 cap)
        r2 = guard.check_and_record(
            10.0, "strategy_boost", strategy_id=1, description="Boost 2"
        )
        assert r2["allowed"] is False
        assert "Strategy 1" in r2["reason"]

    def test_strategy_boost_cap_per_strategy(self, guard):
        """Different strategies have independent boost caps."""
        r1 = guard.check_and_record(
            45.0, "strategy_boost", strategy_id=1, description="Boost S1"
        )
        assert r1["allowed"] is True

        # Different strategy should also be allowed
        r2 = guard.check_and_record(
            45.0, "strategy_boost", strategy_id=2, description="Boost S2"
        )
        assert r2["allowed"] is True

    def test_requires_approval_above_threshold(self, guard):
        """Transactions above require_approval_above should flag for approval."""
        # First, raise the single tx cap so it doesn't block
        guard.config.reinvestment.max_single_transaction = 1000.0
        guard.config.reinvestment.max_daily_spend = 1000.0

        result = guard.check_and_record(600.0, "expense", description="Big expense")
        assert result["allowed"] is True
        assert result["requires_approval"] is True
        assert "approval threshold" in result["reason"]

    def test_tracks_daily_spend(self, guard):
        guard.check_and_record(30.0, "expense")
        guard.check_and_record(20.0, "expense")

        assert guard.get_daily_spend() == 50.0

    def test_blocked_not_counted_in_daily(self, guard):
        """Blocked transactions shouldn't count toward daily total."""
        guard.check_and_record(90.0, "expense")
        guard.check_and_record(20.0, "expense")  # Blocked

        assert guard.get_daily_spend() == 90.0

    def test_spending_summary(self, guard):
        guard.check_and_record(30.0, "expense")
        guard.check_and_record(20.0, "reinvestment")
        guard.check_and_record(999.0, "expense")  # Blocked (>200 single cap)

        summary = guard.get_spending_summary()
        assert summary["total_spent"] == 50.0
        assert summary["total_transactions"] == 2
        assert summary["blocked_transactions"] == 1
        assert summary["limits"]["max_daily_spend"] == 100.0

    def test_exactly_at_daily_cap_allowed(self, guard):
        """Spending exactly the daily cap should be allowed."""
        result = guard.check_and_record(100.0, "expense")
        assert result["allowed"] is True

        # Next dollar should be blocked
        result = guard.check_and_record(0.01, "expense")
        assert result["allowed"] is False
