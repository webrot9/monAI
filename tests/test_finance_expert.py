"""Tests for FinanceExpert agent."""

from unittest.mock import MagicMock

import pytest

from monai.agents.finance_expert import FinanceExpert
from monai.business.commercialista import Commercialista
from monai.config import Config
from monai.db.database import Database
from tests.conftest_schema import TEST_SCHEMA


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    with d.connect() as conn:
        conn.executescript(TEST_SCHEMA)
    return d


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def llm():
    mock = MagicMock()
    mock.quick.return_value = "test response"
    mock.chat_json.return_value = {"risk_score": 0.3, "risk_factors": ["low competition"]}
    return mock


@pytest.fixture
def expert(config, db, llm):
    commercialista = Commercialista(config, db)
    return FinanceExpert(config, db, llm, commercialista=commercialista)


class TestFinanceExpertInit:
    def test_creates_tables(self, expert, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='investment_recommendations'"
        )
        assert len(rows) == 1

    def test_has_finance_module(self, expert):
        assert expert.finance is not None

    def test_has_projector(self, expert):
        assert expert.projector is not None


class TestDecideAction:
    def test_no_data_returns_maintain(self, expert):
        result = expert._decide_action(
            {"revenue": 0, "expenses": 0, "net": 0},
            total_revenue=0,
            budget={"balance": 500},
        )
        assert result["action"] == "maintain"

    def test_high_roi_returns_scale_up(self, expert):
        result = expert._decide_action(
            {"revenue": 300, "expenses": 50, "net": 250},
            total_revenue=300,
            budget={"balance": 500},
        )
        assert result["action"] == "scale_up"
        assert result["confidence"] >= 0.8

    def test_moderate_roi_returns_maintain(self, expert):
        result = expert._decide_action(
            {"revenue": 100, "expenses": 50, "net": 50},
            total_revenue=100,
            budget={"balance": 500},
        )
        assert result["action"] == "maintain"

    def test_low_roi_returns_reduce(self, expert):
        result = expert._decide_action(
            {"revenue": 30, "expenses": 50, "net": -20},
            total_revenue=30,
            budget={"balance": 500},
        )
        assert result["action"] == "reduce"

    def test_zero_revenue_high_spend_returns_pause(self, expert):
        result = expert._decide_action(
            {"revenue": 0, "expenses": 50, "net": -50},
            total_revenue=0,
            budget={"balance": 500},
        )
        assert result["action"] == "pause"


class TestPortfolioHealth:
    def test_healthy_portfolio(self, expert):
        pnl = [
            {"name": "a", "revenue": 100, "expenses": 20, "net": 80},
            {"name": "b", "revenue": 80, "expenses": 30, "net": 50},
            {"name": "c", "revenue": 50, "expenses": 10, "net": 40},
        ]
        result = expert._assess_portfolio_health(
            pnl, {"balance": 500, "burn_rate_daily": 1}
        )
        assert result["status"] == "healthy"
        assert result["profitable_strategies"] == 3

    def test_critical_when_broke(self, expert):
        result = expert._assess_portfolio_health(
            [], {"balance": -10, "burn_rate_daily": 5}
        )
        assert result["status"] == "critical"

    def test_concentration_risk_detected(self, expert):
        pnl = [
            {"name": "a", "revenue": 900, "expenses": 100, "net": 800},
            {"name": "b", "revenue": 50, "expenses": 20, "net": 30},
            {"name": "c", "revenue": 50, "expenses": 20, "net": 30},
        ]
        result = expert._assess_portfolio_health(
            pnl, {"balance": 500, "burn_rate_daily": 1}
        )
        assert result["concentration_risk"] is True


class TestOpportunityScoring:
    def test_high_roi_opportunity_scores_well(self, expert):
        result = expert.score_opportunity(
            "Build AI writing tool", revenue_potential=500,
            cost_estimate=50, time_to_revenue_days=30,
        )
        assert result["composite_score"] > 0.5
        assert result["recommendation"] in ("pursue", "high_priority")

    def test_poor_opportunity_scored_low(self, expert):
        expert.llm.chat_json.return_value = {"risk_score": 0.9, "risk_factors": ["very risky"]}
        result = expert.score_opportunity(
            "Risky bet", revenue_potential=10,
            cost_estimate=500, time_to_revenue_days=365,
        )
        assert result["composite_score"] < 0.5
        assert result["recommendation"] == "skip"

    def test_opportunity_stored_in_db(self, expert):
        expert.score_opportunity(
            "Test opportunity", revenue_potential=100,
            cost_estimate=20, time_to_revenue_days=60,
        )
        rows = expert.db.execute("SELECT * FROM opportunity_scores")
        assert len(rows) == 1
        assert rows[0]["opportunity"] == "Test opportunity"


class TestGenerateRecommendations:
    def test_recommendations_stored(self, expert, db):
        # Insert strategy data
        db.execute_insert(
            "INSERT INTO strategies (name, category) VALUES ('test_strat', 'services')"
        )
        db.execute_insert(
            "INSERT INTO transactions (strategy_id, type, category, amount) VALUES (1, 'revenue', 'service', 200)"
        )
        db.execute_insert(
            "INSERT INTO transactions (strategy_id, type, category, amount) VALUES (1, 'expense', 'api_cost', 50)"
        )

        pnl = expert.finance.get_strategy_pnl()
        recs = expert._generate_recommendations(pnl, {"balance": 500})
        assert len(recs) > 0

        stored = db.execute("SELECT * FROM investment_recommendations")
        assert len(stored) == len(recs)


class TestRun:
    def test_run_returns_expected_keys(self, expert, db):
        result = expert.run()
        assert "recommendations" in result
        assert "forecast_months" in result
        assert "portfolio_health" in result
