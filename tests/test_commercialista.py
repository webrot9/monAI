"""Tests for monai.business.commercialista."""

import pytest

from monai.business.commercialista import Commercialista


class TestCommercialista:
    @pytest.fixture
    def acct(self, config, db):
        return Commercialista(config, db)

    def test_budget_initialized(self, acct):
        budget = acct.get_budget()
        assert budget["initial"] == 500.0
        assert budget["currency"] == "EUR"

    def test_budget_not_double_initialized(self, config, db):
        c1 = Commercialista(config, db)
        c2 = Commercialista(config, db)
        rows = db.execute("SELECT COUNT(*) as c FROM budget")
        assert rows[0]["c"] == 1

    def test_can_spend_within_budget(self, acct):
        assert acct.can_spend(100.0) is True

    def test_cannot_spend_over_budget(self, acct):
        assert acct.can_spend(99999.0) is False

    def test_get_remaining_budget(self, acct):
        remaining = acct.get_remaining_budget()
        assert remaining == 500.0

    def test_log_api_cost(self, acct, db):
        acct.log_api_cost("writer", "gpt-4o-mini", 1000, 500, 0.0005, "test call")
        rows = db.execute("SELECT * FROM cost_log WHERE agent_name = 'writer'")
        assert len(rows) == 1
        assert rows[0]["cost_eur"] == 0.0005
        assert rows[0]["model"] == "gpt-4o-mini"

    def test_log_expense(self, acct, db):
        acct.log_expense("provisioner", "platform_fee", 5.0, "Upwork connects")
        rows = db.execute("SELECT * FROM cost_log WHERE agent_name = 'provisioner'")
        assert len(rows) == 1
        assert rows[0]["cost_type"] == "platform_fee"

    def test_get_cost_by_agent(self, acct):
        acct.log_api_cost("writer", "gpt-4o-mini", 100, 50, 0.001)
        acct.log_api_cost("writer", "gpt-4o-mini", 100, 50, 0.001)
        acct.log_api_cost("researcher", "gpt-4o", 100, 50, 0.005)

        by_agent = acct.get_cost_by_agent()
        assert len(by_agent) == 2
        writer = [a for a in by_agent if a["agent_name"] == "writer"][0]
        assert writer["calls"] == 2

    def test_get_cost_by_model(self, acct):
        acct.log_api_cost("a", "gpt-4o", 100, 50, 0.005)
        acct.log_api_cost("b", "gpt-4o-mini", 100, 50, 0.001)

        by_model = acct.get_cost_by_model()
        models = {m["model"] for m in by_model}
        assert "gpt-4o" in models
        assert "gpt-4o-mini" in models

    def test_get_daily_costs(self, acct):
        acct.log_api_cost("a", "gpt-4o-mini", 100, 50, 0.001)
        daily = acct.get_daily_costs(days=1)
        assert len(daily) == 1
        assert daily[0]["cost"] == 0.001

    def test_get_full_report(self, acct):
        report = acct.get_full_report()
        assert "budget" in report
        assert "sustainability" in report
        assert "costs_by_agent" in report
        assert "costs_by_model" in report

    def test_recommendation_healthy(self, acct):
        budget = {"balance": 500, "initial": 500, "self_sustaining": True,
                  "days_until_broke": None}
        rec = acct._get_recommendation(budget)
        assert "HEALTHY" in rec

    def test_recommendation_critical(self, acct):
        budget = {"balance": 0, "initial": 500, "self_sustaining": False,
                  "days_until_broke": 0}
        rec = acct._get_recommendation(budget)
        assert "CRITICAL" in rec

    def test_recommendation_warning(self, acct):
        budget = {"balance": 10, "initial": 500, "self_sustaining": False,
                  "days_until_broke": 3}
        rec = acct._get_recommendation(budget)
        assert "WARNING" in rec

    def test_recommendation_caution(self, acct):
        budget = {"balance": 200, "initial": 500, "self_sustaining": False,
                  "days_until_broke": 100}
        rec = acct._get_recommendation(budget)
        assert "CAUTION" in rec
