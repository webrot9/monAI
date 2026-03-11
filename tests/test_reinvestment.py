"""Tests for the Commercialista reinvestment engine."""

import json
from unittest.mock import MagicMock

import pytest

from monai.config import Config, ReinvestmentConfig
from monai.db.database import Database
from monai.business.commercialista import Commercialista


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def config():
    return Config(initial_capital=500.0)


@pytest.fixture
def commercialista(config, db):
    return Commercialista(config, db)


def _seed_transactions(db, revenue: float, expense: float):
    """Helper: seed the transactions table with revenue and expense."""
    # Ensure transactions table exists (created by Database.__init__)
    if revenue > 0:
        db.execute_insert(
            "INSERT INTO transactions (type, category, amount, description) "
            "VALUES ('revenue', 'payment', ?, 'test revenue')",
            (revenue,),
        )
    if expense > 0:
        db.execute_insert(
            "INSERT INTO transactions (type, category, amount, description) "
            "VALUES ('expense', 'api_cost', ?, 'test expense')",
            (expense,),
        )


class TestReinvestmentSchema:
    def test_creates_reinvestment_log_table(self, commercialista, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reinvestment_log'"
        )
        assert len(rows) == 1


class TestComputeReinvestment:
    def test_disabled(self, config, db):
        config.reinvestment = ReinvestmentConfig(enabled=False)
        c = Commercialista(config, db)
        assert c.compute_reinvestment() == {"status": "disabled"}

    def test_below_threshold_no_profit(self, commercialista):
        result = commercialista.compute_reinvestment()
        assert result["status"] == "below_threshold"

    def test_below_threshold_small_profit(self, config, db):
        _seed_transactions(db, revenue=8.0, expense=0.0)
        c = Commercialista(config, db)
        result = c.compute_reinvestment()
        assert result["status"] == "below_threshold"
        assert result["threshold"] == 10.0

    def test_ready_with_profit(self, config, db):
        _seed_transactions(db, revenue=100.0, expense=0.0)
        c = Commercialista(config, db)
        result = c.compute_reinvestment()
        assert result["status"] == "ready"
        # Default: 40% reinvest, 30% reserve, 30% creator
        assert result["reinvest"] == 40.0
        assert result["reserve"] == 30.0
        assert result["creator_sweep"] == 30.0
        assert result["net_profit"] == 100.0

    def test_custom_split(self, config, db):
        config.reinvestment = ReinvestmentConfig(
            enabled=True,
            reinvest_pct=60.0,
            reserve_pct=20.0,
            creator_pct=20.0,
            min_profit_to_reinvest=5.0,
        )
        _seed_transactions(db, revenue=50.0, expense=0.0)
        c = Commercialista(config, db)
        result = c.compute_reinvestment()
        assert result["status"] == "ready"
        assert result["reinvest"] == 30.0  # 60% of 50
        assert result["reserve"] == 10.0  # 20% of 50
        assert result["creator_sweep"] == 10.0

    def test_net_profit_is_revenue_minus_expense(self, config, db):
        _seed_transactions(db, revenue=200.0, expense=180.0)
        c = Commercialista(config, db)
        result = c.compute_reinvestment()
        assert result["status"] == "ready"
        assert result["unreinvested"] == 20.0
        assert result["reinvest"] == 8.0  # 40% of 20

    def test_does_not_double_reinvest(self, config, db):
        """After recording a reinvestment, the same profit shouldn't be reinvested again."""
        _seed_transactions(db, revenue=100.0, expense=0.0)
        c = Commercialista(config, db)

        # First reinvestment
        result1 = c.compute_reinvestment()
        assert result1["status"] == "ready"
        assert result1["unreinvested"] == 100.0

        # Record it
        c.record_reinvestment(
            reinvest=result1["reinvest"],
            reserve=result1["reserve"],
            creator=result1["creator_sweep"],
        )

        # Second call — no new profit, so below threshold
        result2 = c.compute_reinvestment()
        assert result2["status"] == "below_threshold"
        assert result2["unreinvested"] == 0.0

    def test_incremental_reinvestment(self, config, db):
        """New revenue after a reinvestment should be reinvestable."""
        _seed_transactions(db, revenue=50.0, expense=0.0)
        c = Commercialista(config, db)

        result1 = c.compute_reinvestment()
        c.record_reinvestment(result1["reinvest"], result1["reserve"], result1["creator_sweep"])

        # Add more revenue
        db.execute_insert(
            "INSERT INTO transactions (type, category, amount) VALUES ('revenue', 'payment', 30.0)"
        )

        result2 = c.compute_reinvestment()
        assert result2["status"] == "ready"
        assert result2["unreinvested"] == 30.0
        assert result2["reinvest"] == 12.0  # 40% of 30


class TestRecordReinvestment:
    def test_stores_record(self, commercialista, db):
        _seed_transactions(db, revenue=100.0, expense=0.0)
        commercialista.record_reinvestment(40.0, 30.0, 30.0)

        rows = db.execute("SELECT * FROM reinvestment_log")
        assert len(rows) == 1
        assert rows[0]["reinvest_amount"] == 40.0
        assert rows[0]["reserve_amount"] == 30.0
        assert rows[0]["creator_amount"] == 30.0

    def test_stores_allocations_json(self, commercialista, db):
        _seed_transactions(db, revenue=100.0, expense=0.0)
        allocs = [{"strategy": "freelance_writing", "amount": 20, "action": "boost"}]
        commercialista.record_reinvestment(40.0, 30.0, 30.0, allocations=allocs)

        rows = db.execute("SELECT allocations FROM reinvestment_log")
        parsed = json.loads(rows[0]["allocations"])
        assert parsed[0]["strategy"] == "freelance_writing"


class TestAllocateToStrategies:
    def test_empty_strategies(self, commercialista):
        result = commercialista.allocate_to_strategies(100.0, [])
        assert result == []

    def test_scale_winners(self, commercialista):
        strategies = [
            {"name": "s1", "revenue": 100, "expenses": 20, "roi": 5.0},
            {"name": "s2", "revenue": 50, "expenses": 40, "roi": 1.25},
        ]
        allocs = commercialista.allocate_to_strategies(50.0, strategies)
        # Both are winners (ROI > 1.0)
        boost_allocs = [a for a in allocs if a["action"] == "boost"]
        assert len(boost_allocs) == 2
        # Higher ROI gets more
        s1_alloc = next(a for a in boost_allocs if a["strategy"] == "s1")
        s2_alloc = next(a for a in boost_allocs if a["strategy"] == "s2")
        assert s1_alloc["amount"] > s2_alloc["amount"]

    def test_cut_losers(self, commercialista):
        strategies = [
            {"name": "loser", "revenue": 5, "expenses": 50, "roi": 0.1},
        ]
        allocs = commercialista.allocate_to_strategies(50.0, strategies)
        assert len(allocs) == 1
        assert allocs[0]["action"] == "reduce"
        assert allocs[0]["amount"] == 0

    def test_mixed_portfolio(self, commercialista):
        strategies = [
            {"name": "winner", "revenue": 200, "expenses": 50, "roi": 4.0},
            {"name": "neutral", "revenue": 20, "expenses": 25, "roi": 0.8},
            {"name": "loser", "revenue": 2, "expenses": 30, "roi": 0.07},
        ]
        allocs = commercialista.allocate_to_strategies(100.0, strategies)
        actions = {a["strategy"]: a["action"] for a in allocs}
        assert actions["winner"] == "boost"
        assert actions["neutral"] == "maintain"
        assert actions["loser"] == "reduce"

    def test_max_strategy_boost_cap(self, config, db):
        config.reinvestment = ReinvestmentConfig(max_strategy_boost=10.0)
        c = Commercialista(config, db)
        strategies = [
            {"name": "s1", "revenue": 1000, "expenses": 10, "roi": 100.0},
        ]
        allocs = c.allocate_to_strategies(500.0, strategies)
        boost = next(a for a in allocs if a["action"] == "boost")
        assert boost["amount"] <= 10.0  # Capped
