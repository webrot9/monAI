"""Tests for Marketing Team."""

from unittest.mock import MagicMock

import pytest

from monai.agents.marketing_team import MarketingTeam
from monai.agents.marketing_team.content_marketer import ContentMarketer
from monai.agents.marketing_team.growth_hacker import GrowthHacker
from monai.agents.marketing_team.outreach_specialist import OutreachSpecialist
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
    mock.quick.return_value = "Generated content here."
    mock.chat_json.return_value = {}
    return mock


# ── ContentMarketer tests ────────────────────────────────────


class TestContentMarketer:
    def test_run_no_campaign(self, config, db, llm):
        cm = ContentMarketer(config, db, llm)
        result = cm.run()
        assert result["pieces_created"] == 0

    def test_run_creates_content(self, config, db, llm):
        llm.chat_json.return_value = {
            "pieces": [
                {"type": "blog_post", "title": "How AI Works",
                 "outline": "Intro, Main, Conclusion",
                 "platform": "medium", "seo_keywords": ["AI", "automation"]},
            ]
        }
        cm = ContentMarketer(config, db, llm)
        result = cm.run(
            campaign={"name": "test", "target_audience": "devs",
                      "channel": "blog", "key_message": "AI is great"},
            strategy="content_sites",
        )
        assert result["pieces_created"] == 1

    def test_plan(self, config, db, llm):
        cm = ContentMarketer(config, db, llm)
        assert len(cm.plan()) >= 3


# ── GrowthHacker tests ──────────────────────────────────────


class TestGrowthHacker:
    def test_run_no_campaign(self, config, db, llm):
        gh = GrowthHacker(config, db, llm)
        result = gh.run()
        assert result["experiments_launched"] == 0

    def test_run_launches_experiments(self, config, db, llm):
        llm.chat_json.return_value = {
            "experiments": [
                {"name": "Referral loop", "hypothesis": "Users refer friends",
                 "type": "referral", "implementation": "Add referral code",
                 "success_metric": "signups", "expected_impact": "2x growth"},
            ]
        }
        gh = GrowthHacker(config, db, llm)
        result = gh.run(
            campaign={"name": "growth", "target_audience": "startups"},
            strategy="saas",
        )
        assert result["experiments_launched"] == 1

    def test_experiments_shared_as_knowledge(self, config, db, llm):
        llm.chat_json.return_value = {
            "experiments": [
                {"name": "Test exp", "hypothesis": "H1",
                 "type": "viral_loop", "implementation": "impl",
                 "success_metric": "metric", "expected_impact": "big"},
            ]
        }
        gh = GrowthHacker(config, db, llm)
        gh.run(campaign={"name": "test"}, strategy="test")
        rows = db.execute(
            "SELECT * FROM knowledge WHERE category = 'growth_experiment'"
        )
        assert len(rows) >= 1


# ── OutreachSpecialist tests ────────────────────────────────


class TestOutreachSpecialist:
    def test_run_no_campaign(self, config, db, llm):
        os_ = OutreachSpecialist(config, db, llm)
        result = os_.run()
        assert result["messages_sent"] == 0

    def test_run_plans_sequences(self, config, db, llm):
        llm.chat_json.return_value = {
            "outreach_sequences": [
                {"target_type": "startup founders", "channel": "email",
                 "message_template": "Hi {name}...",
                 "personalization_fields": ["name", "company"],
                 "follow_up_days": 3, "expected_response_rate": 0.15},
            ]
        }
        os_ = OutreachSpecialist(config, db, llm)
        result = os_.run(
            campaign={"name": "cold outreach", "target_audience": "founders"},
            strategy="lead_gen",
        )
        assert result["messages_sent"] == 1
        assert result["sequences"][0]["channel"] == "email"


# ── MarketingTeam coordinator tests ──────────────────────────


class TestMarketingTeam:
    def test_creates_tables(self, config, db, llm):
        team = MarketingTeam(config, db, llm)
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='marketing_campaigns'"
        )
        assert len(rows) == 1

    def test_plan(self, config, db, llm):
        team = MarketingTeam(config, db, llm)
        steps = team.plan()
        assert len(steps) >= 3

    def test_run_plans_and_executes(self, config, db, llm):
        # Plan response
        llm.chat_json.side_effect = [
            # _plan_campaigns
            {"campaigns": [
                {"name": "Content Push", "campaign_type": "content",
                 "strategy_name": "content_sites", "channel": "blog",
                 "target_audience": "developers", "key_message": "Build faster",
                 "budget_eur": 10},
            ]},
            # ContentMarketer.run → content_plan
            {"pieces": []},
        ]
        team = MarketingTeam(config, db, llm)
        result = team.run()
        assert result["campaigns_planned"] == 1

    def test_run_no_campaigns(self, config, db, llm):
        llm.chat_json.return_value = {"campaigns": []}
        team = MarketingTeam(config, db, llm)
        result = team.run()
        assert result["campaigns_planned"] == 0

    def test_launch_campaign(self, config, db, llm):
        llm.chat_json.return_value = {
            "campaign_name": "SaaS Launch",
            "phases": [
                {"phase": "awareness", "type": "content",
                 "actions": ["blog post"], "expected_result": "1000 views"},
            ],
            "total_expected_leads": 50,
            "timeline_days": 30,
        }
        team = MarketingTeam(config, db, llm)
        result = team.launch_campaign("saas", "AI writing tool", budget=50)
        assert "campaign_id" in result
        assert result["plan"]["campaign_name"] == "SaaS Launch"

    def test_campaign_stored_in_db(self, config, db, llm):
        llm.chat_json.return_value = {
            "campaign_name": "Test",
            "phases": [],
            "total_expected_leads": 0,
            "timeline_days": 7,
        }
        team = MarketingTeam(config, db, llm)
        team.launch_campaign("test", "Test product")
        rows = db.execute("SELECT * FROM marketing_campaigns")
        assert len(rows) == 1

    def test_get_campaign_performance(self, config, db, llm):
        team = MarketingTeam(config, db, llm)
        db.execute_insert(
            "INSERT INTO marketing_campaigns "
            "(name, campaign_type, strategy_name, status) "
            "VALUES ('Test', 'content', 'test', 'active')"
        )
        perf = team.get_campaign_performance()
        assert len(perf) == 1
        assert perf[0]["name"] == "Test"

    def test_roi_by_campaign_empty(self, config, db, llm):
        team = MarketingTeam(config, db, llm)
        roi = team.get_roi_by_campaign()
        assert roi == []

    def test_roi_by_campaign_with_data(self, config, db, llm):
        team = MarketingTeam(config, db, llm)
        db.execute_insert(
            "INSERT INTO marketing_campaigns "
            "(name, campaign_type, strategy_name, spent_eur, revenue_attributed) "
            "VALUES ('Profitable', 'content', 'test', 100, 500)"
        )
        roi = team.get_roi_by_campaign()
        assert len(roi) == 1
        assert roi[0]["roi"] == 5.0
