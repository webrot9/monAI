"""Tests for Market Research Team."""

from unittest.mock import MagicMock

import pytest

from monai.agents.research_team import ResearchTeam
from monai.agents.research_team.trend_scout import TrendScout
from monai.agents.research_team.market_researcher import MarketResearcher
from monai.agents.research_team.competitor_analyst import CompetitorAnalyst
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
    mock.chat_json.return_value = {}
    return mock


# ── TrendScout tests ─────────────────────────────────────────


class TestTrendScout:
    def test_plan(self, config, db, llm):
        scout = TrendScout(config, db, llm)
        steps = scout.plan()
        assert len(steps) >= 2

    def test_run_returns_opportunities(self, config, db, llm):
        llm.chat_json.return_value = {
            "opportunities": [
                {"niche": "AI writing tools", "trend_direction": "rising",
                 "timing_score": 0.8, "reasoning": "High demand"},
            ]
        }
        scout = TrendScout(config, db, llm)
        result = scout.run()
        assert "opportunities" in result
        assert len(result["opportunities"]) == 1

    def test_run_with_focus_areas(self, config, db, llm):
        llm.chat_json.return_value = {"opportunities": []}
        scout = TrendScout(config, db, llm)
        result = scout.run(focus_areas=["AI tools", "SaaS"])
        assert "opportunities" in result

    def test_run_handles_empty_response(self, config, db, llm):
        llm.chat_json.return_value = {}
        scout = TrendScout(config, db, llm)
        result = scout.run()
        assert result["opportunities"] == []


# ── MarketResearcher tests ───────────────────────────────────


class TestMarketResearcher:
    def test_run_no_niche_returns_not_viable(self, config, db, llm):
        researcher = MarketResearcher(config, db, llm)
        result = researcher.run(niche="")
        assert result["viable"] is False

    def test_run_viable_niche(self, config, db, llm):
        llm.chat_json.return_value = {
            "niche": "AI writing tools",
            "market_size": "$2B TAM",
            "viable": True,
            "estimated_monthly_revenue": 500,
            "confidence": 0.7,
            "target_customer": "content creators",
            "willingness_to_pay": "$20-50/mo",
            "demand_signals": ["growing search volume"],
            "barriers_to_entry": ["competition"],
        }
        researcher = MarketResearcher(config, db, llm)
        result = researcher.run(niche="AI writing tools")
        assert result["viable"] is True
        assert result["estimated_monthly_revenue"] == 500

    def test_viable_niche_shares_knowledge(self, config, db, llm):
        llm.chat_json.return_value = {
            "niche": "test", "viable": True, "confidence": 0.8,
        }
        researcher = MarketResearcher(config, db, llm)
        researcher.run(niche="test niche")
        # Knowledge should be stored
        rows = db.execute(
            "SELECT * FROM knowledge WHERE category = 'market_research'"
        )
        assert len(rows) >= 1


# ── CompetitorAnalyst tests ──────────────────────────────────


class TestCompetitorAnalyst:
    def test_run_no_niche(self, config, db, llm):
        analyst = CompetitorAnalyst(config, db, llm)
        result = analyst.run(niche="")
        assert result["competition_level"] == "unknown"

    def test_run_returns_gaps(self, config, db, llm):
        llm.chat_json.return_value = {
            "niche": "AI tools",
            "competitors": [{"name": "Jasper", "strengths": ["brand"], "weaknesses": ["price"]}],
            "competition_level": "medium",
            "gaps": ["affordable tier", "API access"],
            "moat_difficulty": "medium",
            "monai_advantages": ["speed", "cost"],
            "entry_strategy": "Undercut on price",
            "confidence": 0.7,
        }
        analyst = CompetitorAnalyst(config, db, llm)
        result = analyst.run(niche="AI tools")
        assert result["competition_level"] == "medium"
        assert len(result["gaps"]) == 2

    def test_gaps_shared_as_knowledge(self, config, db, llm):
        llm.chat_json.return_value = {
            "niche": "test",
            "gaps": ["gap1"],
            "competition_level": "low",
            "confidence": 0.7,
        }
        analyst = CompetitorAnalyst(config, db, llm)
        analyst.run(niche="test")
        rows = db.execute(
            "SELECT * FROM knowledge WHERE category = 'competitive_intel'"
        )
        assert len(rows) >= 1


# ── ResearchTeam coordinator tests ───────────────────────────


class TestResearchTeam:
    def test_creates_tables(self, config, db, llm):
        team = ResearchTeam(config, db, llm)
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='research_briefs'"
        )
        assert len(rows) == 1

    def test_plan(self, config, db, llm):
        team = ResearchTeam(config, db, llm)
        steps = team.plan()
        assert len(steps) >= 3

    def test_run_coordinates_all_researchers(self, config, db, llm):
        # Trend scout finds opportunities
        llm.chat_json.side_effect = [
            # TrendScout
            {"opportunities": [{"niche": "AI tools", "trend_direction": "rising",
                                "timing_score": 0.8, "reasoning": "hot"}]},
            # MarketResearcher
            {"niche": "AI tools", "viable": True, "market_size": "$1B",
             "estimated_monthly_revenue": 300, "confidence": 0.7,
             "target_customer": "devs", "willingness_to_pay": "$30/mo",
             "demand_signals": ["growing"], "barriers_to_entry": []},
            # CompetitorAnalyst
            {"niche": "AI tools", "competitors": [], "competition_level": "low",
             "gaps": ["pricing"], "moat_difficulty": "easy",
             "monai_advantages": ["speed"], "entry_strategy": "MVP",
             "confidence": 0.6},
            # Synthesis
            {"briefs": [{"title": "AI Tools Opportunity", "niche": "AI tools",
                         "findings": "Low competition, growing demand",
                         "recommended_action": "pursue",
                         "confidence": 0.7, "revenue_estimate": 300}]},
        ]

        team = ResearchTeam(config, db, llm)
        result = team.run()
        assert result["trends"] >= 1
        assert result["markets_analyzed"] >= 1
        assert len(result["briefs"]) >= 1

    def test_research_specific_topic(self, config, db, llm):
        llm.chat_json.side_effect = [
            # MarketResearcher
            {"niche": "test", "viable": True, "confidence": 0.5},
            # CompetitorAnalyst
            {"niche": "test", "gaps": [], "competition_level": "high", "confidence": 0.5},
            # Synthesis
            {"briefs": []},
        ]
        team = ResearchTeam(config, db, llm)
        result = team.research_specific("test topic")
        assert "market" in result
        assert "competition" in result

    def test_get_pursue_briefs(self, config, db, llm):
        team = ResearchTeam(config, db, llm)
        # Insert a brief
        db.execute_insert(
            "INSERT INTO research_briefs "
            "(title, brief_type, niche, findings, recommended_action, confidence, revenue_estimate) "
            "VALUES ('Test', 'opportunity', 'AI', 'Good market', 'pursue', 0.8, 500)"
        )
        briefs = team.get_pursue_briefs()
        assert len(briefs) == 1
        assert briefs[0]["recommended_action"] == "pursue"

    def test_run_empty_trends(self, config, db, llm):
        llm.chat_json.return_value = {"opportunities": []}
        team = ResearchTeam(config, db, llm)
        result = team.run()
        assert result["trends"] == 0
        assert result["briefs"] == []
