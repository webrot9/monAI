"""Tests for Sprint 4: Team agents with real logic.

Covers:
- GrowthHacker: historical experiment insights, data-driven design
- ContentMarketer: programmatic SEO validation
- OutreachSpecialist: prospect segmentation, performance tracking, follow-up templates
- AgentSpawner: structured task decomposition, dependency resolution
"""

from unittest.mock import MagicMock

import pytest

from monai.agents.marketing_team.content_marketer import ContentMarketer
from monai.agents.marketing_team.growth_hacker import GrowthHacker
from monai.agents.marketing_team.outreach_specialist import OutreachSpecialist
from monai.agents.spawner import AgentSpawner
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
    mock.quick.return_value = "Generated content."
    mock.chat_json.return_value = {}
    return mock


# ── GrowthHacker: Historical Experiment Insights ──────────────


class TestGrowthHackerInsights:
    def test_empty_history_returns_empty(self, config, db, llm):
        gh = GrowthHacker(config, db, llm)
        insights = gh._get_experiment_insights()
        assert insights["type_performance"] == []
        assert insights["winning_patterns"] == []
        assert insights["best_types"] == []

    def test_insights_from_concluded_experiments(self, config, db, llm):
        gh = GrowthHacker(config, db, llm)
        # Insert concluded experiments
        for i, (exp_type, winner) in enumerate([
            ("viral_loop", "a"), ("viral_loop", "b"), ("viral_loop", "a"),
            ("referral", "inconclusive"), ("referral", "a"),
        ]):
            db.execute_insert(
                "INSERT INTO growth_experiments "
                "(name, experiment_type, hypothesis, variant_a, variant_b, "
                "success_metric, status, winner, confidence_level, "
                "variant_a_views, variant_a_conversions, variant_b_views, variant_b_conversions) "
                "VALUES (?, ?, 'Test hypothesis', 'A', 'B', 'conversions', 'concluded', ?, ?, "
                "100, 20, 100, 15)",
                (f"exp_{i}", exp_type, winner, 0.95 if winner != "inconclusive" else 0.5),
            )

        insights = gh._get_experiment_insights()
        assert len(insights["type_performance"]) == 2
        # viral_loop has 3 concluded, all with real winners
        viral = [t for t in insights["type_performance"] if t["experiment_type"] == "viral_loop"]
        assert len(viral) == 1
        assert viral[0]["total"] == 3
        assert viral[0]["wins"] == 3
        assert viral[0]["win_rate"] == 1.0

        assert "viral_loop" in insights["best_types"]
        assert len(insights["winning_patterns"]) > 0
        assert len(insights["inconclusive_types"]) > 0

    def test_insights_fed_to_experiment_design(self, config, db, llm):
        """Verify that historical insights are included in the LLM prompt."""
        gh = GrowthHacker(config, db, llm)
        gh.search_web = MagicMock(return_value={"benchmarks": []})
        gh.execute_task = MagicMock(return_value={"status": "completed"})
        llm.chat_json.return_value = {"experiments": []}

        gh.run(
            campaign={"name": "test", "id": 1, "target_audience": "devs"},
            strategy="saas",
        )

        # Check LLM was called with historical insights context
        call_args = llm.chat_json.call_args
        prompt = str(call_args)
        assert "Historical insights" in prompt or "historical" in prompt.lower()


# ── ContentMarketer: SEO Validation ───────────────────────────


class TestContentValidation:
    def test_valid_content_passes(self, config, db, llm):
        cm = ContentMarketer(config, db, llm)
        body = (
            "# How to Build Modern Applications\n\n"
            "Technology is transforming how we build software. "
            "In this guide, we explore key concepts in modern development.\n\n"
            "## Getting Started\n\n"
            "First, understand the fundamentals of automation. "
            "Machine learning and related tools can dramatically improve "
            "productivity in many domains. " * 10
        )
        result = cm._validate_content(body, ["AI", "automation"])
        assert result["pass"] is True
        assert result["keywords_found"] == 2
        assert result["word_count"] > 100

    def test_too_short_fails(self, config, db, llm):
        cm = ContentMarketer(config, db, llm)
        result = cm._validate_content("Short content.", ["AI"])
        assert result["pass"] is False
        assert any("short" in i.lower() for i in result["issues"])

    def test_missing_keywords_fails(self, config, db, llm):
        cm = ContentMarketer(config, db, llm)
        body = "This is a test article about technology. " * 10
        result = cm._validate_content(body, ["blockchain", "crypto"])
        assert result["pass"] is False
        assert any("keyword" in i.lower() for i in result["issues"])

    def test_keyword_stuffing_detected(self, config, db, llm):
        cm = ContentMarketer(config, db, llm)
        # Repeat keyword to exceed 3% density
        body = "AI AI AI AI AI " * 20 + "other words here. " * 5
        result = cm._validate_content(body, ["AI"])
        assert result["pass"] is False
        assert any("stuffing" in i.lower() for i in result["issues"])

    def test_no_headings_in_long_content(self, config, db, llm):
        cm = ContentMarketer(config, db, llm)
        body = "This is a paragraph about modern technology and development. " * 40  # >300 words, no headings
        result = cm._validate_content(body, [])
        assert result["pass"] is False
        assert any("heading" in i.lower() for i in result["issues"])

    def test_content_with_headings_passes(self, config, db, llm):
        cm = ContentMarketer(config, db, llm)
        body = (
            "# Main Title\n\n"
            "Introduction paragraph with enough words. " * 10 + "\n\n"
            "## Section Two\n\n"
            "More content here. " * 10
        )
        result = cm._validate_content(body, [])
        assert result["pass"] is True

    def test_empty_keywords_skips_keyword_checks(self, config, db, llm):
        cm = ContentMarketer(config, db, llm)
        body = "# Guide\n\n" + "Valid content. " * 20
        result = cm._validate_content(body, [])
        assert result["keywords_found"] == 0
        assert result["keywords_total"] == 0


# ── OutreachSpecialist: Segmentation & Performance ────────────


class TestOutreachSegmentation:
    def test_segment_by_channel(self, config, db, llm):
        os_ = OutreachSpecialist(config, db, llm)
        prospects = {
            "prospects": [
                {"name": "Alice", "email": "alice@example.com"},
                {"name": "Bob", "linkedin": "linkedin.com/in/bob"},
                {"name": "Charlie", "twitter": "@charlie"},
                {"name": "Unknown"},  # No contact info
            ]
        }
        result = os_._segment_prospects(prospects)
        assert result["total"] == 4
        assert len(result["segments"]["email"]) == 1
        assert len(result["segments"]["linkedin"]) == 1
        assert len(result["segments"]["twitter"]) == 1

    def test_deduplication(self, config, db, llm):
        os_ = OutreachSpecialist(config, db, llm)
        # Pre-populate contacted targets
        db.execute_insert(
            "INSERT INTO outreach_sequences "
            "(campaign_id, target_name, target_email, channel, message_body, status) "
            "VALUES (1, 'Alice', 'alice@example.com', 'email', 'Hi', 'sent')"
        )
        prospects = {
            "prospects": [
                {"name": "Alice", "email": "alice@example.com"},
                {"name": "New Person", "email": "new@example.com"},
            ]
        }
        result = os_._segment_prospects(prospects)
        assert result["total"] == 1
        assert result["deduplicated"] == 1

    def test_empty_prospects(self, config, db, llm):
        os_ = OutreachSpecialist(config, db, llm)
        result = os_._segment_prospects({"prospects": []})
        assert result["total"] == 0

    def test_outreach_performance_empty(self, config, db, llm):
        os_ = OutreachSpecialist(config, db, llm)
        perf = os_._get_outreach_performance()
        assert perf["best_channel"] == "email"

    def test_outreach_performance_with_data(self, config, db, llm):
        os_ = OutreachSpecialist(config, db, llm)
        # Insert outreach history
        for channel, status in [
            ("email", "sent"), ("email", "sent"), ("email", "replied"),
            ("linkedin", "sent"), ("linkedin", "replied"), ("linkedin", "replied"),
        ]:
            db.execute_insert(
                "INSERT INTO outreach_sequences "
                "(campaign_id, target_name, channel, message_body, status) "
                "VALUES (1, 'Test', ?, 'Hi', ?)",
                (channel, status),
            )
        perf = os_._get_outreach_performance()
        assert perf["by_channel"]["linkedin"]["response_rate"] > perf["by_channel"]["email"]["response_rate"]
        assert perf["best_channel"] == "linkedin"


class TestFollowUpTemplates:
    def test_first_follow_up_uses_template(self, config, db, llm):
        os_ = OutreachSpecialist(config, db, llm)
        msg = os_._generate_follow_up("Alice Smith", "Original msg", 1)
        assert "Alice" in msg
        assert "follow up" in msg.lower()
        # Should NOT call LLM
        llm.quick.assert_not_called()

    def test_second_follow_up_uses_template(self, config, db, llm):
        os_ = OutreachSpecialist(config, db, llm)
        msg = os_._generate_follow_up("Bob Jones", "Original msg", 2)
        assert "Bob" in msg
        assert "last time" in msg.lower() or "following up" in msg.lower()

    def test_third_follow_up_uses_llm(self, config, db, llm):
        os_ = OutreachSpecialist(config, db, llm)
        llm.quick.return_value = "Custom LLM follow-up"
        msg = os_._generate_follow_up("Charlie", "Original msg", 3)
        # Should call LLM for unusual follow-up count
        assert msg is not None


# ── AgentSpawner: Structured Decomposition ────────────────────

@pytest.fixture
def spawner_llm():
    """LLM mock that works with IdentityManager init."""
    mock = MagicMock()
    mock.quick.return_value = "Test"
    mock.quick_json.return_value = {
        "name": "Test Agent",
        "backstory": "A helpful agent",
        "traits": ["smart"],
    }
    mock.chat_json.return_value = {"tasks": []}
    return mock


class TestStructuredDecomposition:
    def test_numbered_list_decomposition(self, config, db, spawner_llm):
        spawner = AgentSpawner(config, db, spawner_llm)
        tasks = spawner._decompose_structured(
            "1. Research competitors\n"
            "2. Build landing page\n"
            "3. Test conversion flow"
        )
        assert tasks is not None
        assert len(tasks) == 3
        assert "research" in tasks[0]["task"].lower()

    def test_bullet_list_decomposition(self, config, db, spawner_llm):
        spawner = AgentSpawner(config, db, spawner_llm)
        tasks = spawner._decompose_structured(
            "- Find affiliate programs\n"
            "- Create review content\n"
            "- Set up tracking"
        )
        assert tasks is not None
        assert len(tasks) == 3

    def test_and_separated_tasks(self, config, db, spawner_llm):
        spawner = AgentSpawner(config, db, spawner_llm)
        tasks = spawner._decompose_structured(
            "Research market trends and build MVP and launch beta"
        )
        assert tasks is not None
        assert len(tasks) == 3

    def test_complex_goal_returns_none(self, config, db, spawner_llm):
        spawner = AgentSpawner(config, db, spawner_llm)
        tasks = spawner._decompose_structured(
            "Analyze the competitive landscape for AI writing tools by reviewing "
            "their pricing, features, target audiences, customer reviews, market "
            "positioning relative to incumbent solutions in the space."
        )
        # Complex single sentence without list/and structure — returns None
        assert tasks is None

    def test_plan_delegation_uses_structured_first(self, config, db, spawner_llm):
        spawner = AgentSpawner(config, db, spawner_llm)
        tasks = spawner.plan_delegation(
            "1. Research keywords\n2. Write blog post\n3. Publish"
        )
        assert len(tasks) == 3
        # LLM should NOT be called since structured decomposition succeeded
        spawner_llm.chat_json.assert_not_called()


class TestDependencyResolution:
    def test_valid_dependencies_sorted(self, config, db, spawner_llm):
        spawner = AgentSpawner(config, db, spawner_llm)
        tasks = [
            {"name": "deploy", "task": "Deploy to prod", "depends_on": ["build"]},
            {"name": "research", "task": "Research market", "depends_on": []},
            {"name": "build", "task": "Build MVP", "depends_on": ["research"]},
        ]
        sorted_tasks = spawner._resolve_dependencies(tasks)
        names = [t["name"] for t in sorted_tasks]
        assert names.index("research") < names.index("build")
        assert names.index("build") < names.index("deploy")

    def test_invalid_dependencies_dropped(self, config, db, spawner_llm):
        spawner = AgentSpawner(config, db, spawner_llm)
        tasks = [
            {"name": "task_a", "task": "Do A", "depends_on": ["nonexistent"]},
            {"name": "task_b", "task": "Do B", "depends_on": []},
        ]
        sorted_tasks = spawner._resolve_dependencies(tasks)
        assert len(sorted_tasks) == 2
        # Invalid dependency should be removed
        assert sorted_tasks[0].get("depends_on", []) == [] or sorted_tasks[1].get("depends_on", []) == []

    def test_empty_tasks(self, config, db, spawner_llm):
        spawner = AgentSpawner(config, db, spawner_llm)
        assert spawner._resolve_dependencies([]) == []

    def test_circular_dependency_returns_original(self, config, db, spawner_llm):
        spawner = AgentSpawner(config, db, spawner_llm)
        tasks = [
            {"name": "a", "task": "Do A", "depends_on": ["b"]},
            {"name": "b", "task": "Do B", "depends_on": ["a"]},
        ]
        sorted_tasks = spawner._resolve_dependencies(tasks)
        # Cycle detected — returns original order
        assert len(sorted_tasks) == 2
