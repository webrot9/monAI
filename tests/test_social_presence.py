"""Tests for SocialPresence agent — monAI's own social media."""

from unittest.mock import MagicMock

import pytest

from monai.agents.social_presence import (
    PLATFORM_STRATEGIES,
    SocialPresence,
)
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
    mock.quick.return_value = "This is a great post about building in public."
    mock.chat_json.return_value = {}
    return mock


@pytest.fixture
def agent(config, db, llm):
    return SocialPresence(config, db, llm)


# ── Schema & Init ────────────────────────────────────────────


class TestInit:
    def test_creates_tables(self, agent, db):
        for table in ("own_social_accounts", "own_social_posts",
                      "content_calendar", "social_engagement_log"):
            rows = db.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            assert len(rows) == 1, f"Table {table} not created"

    def test_plan(self, agent):
        steps = agent.plan()
        assert len(steps) >= 4


# ── Account Management ───────────────────────────────────────


class TestAccountManagement:
    def test_get_account_status_seeds_platforms(self, agent, db):
        accounts = agent._get_account_status()
        platforms = {a["platform"] for a in accounts}
        assert "twitter" in platforms
        assert "linkedin" in platforms
        assert "reddit" in platforms

    def test_setup_account(self, agent, db):
        # Seed first
        agent._get_account_status()
        result = agent.setup_account("twitter", "monai_bot", "https://x.com/monai_bot")
        assert result["status"] == "active"
        assert result["username"] == "monai_bot"

        rows = db.execute(
            "SELECT * FROM own_social_accounts WHERE platform = 'twitter'"
        )
        assert rows[0]["username"] == "monai_bot"
        assert rows[0]["status"] == "active"

    def test_setup_shares_knowledge(self, agent, db):
        agent._get_account_status()
        agent.setup_account("linkedin", "monai-company")
        rows = db.execute(
            "SELECT * FROM knowledge WHERE topic = 'social_linkedin'"
        )
        assert len(rows) == 1


# ── Content Planning ─────────────────────────────────────────


class TestContentPlanning:
    def test_plan_content_empty_when_no_active(self, agent):
        # All accounts are 'planned', not 'active'
        agent._get_account_status()
        calendar = agent._plan_content(
            [{"platform": "twitter", "status": "planned"}]
        )
        assert calendar == []

    def test_plan_content_with_active_account(self, agent, llm):
        llm.chat_json.return_value = {
            "calendar": [
                {"platform": "twitter", "post_type": "thread",
                 "topic": "How we hit 100 users", "angle": "growth story",
                 "target_audience": "indie hackers"},
            ]
        }
        accounts = [{"platform": "twitter", "status": "active"}]
        calendar = agent._plan_content(accounts)
        assert len(calendar) == 1
        assert calendar[0]["platform"] == "twitter"

    def test_calendar_stored_in_db(self, agent, db, llm):
        llm.chat_json.return_value = {
            "calendar": [
                {"platform": "twitter", "post_type": "post",
                 "topic": "Test", "angle": "test angle"},
            ]
        }
        agent._plan_content([{"platform": "twitter", "status": "active"}])
        rows = db.execute("SELECT * FROM content_calendar")
        assert len(rows) == 1


# ── Content Creation ─────────────────────────────────────────


class TestContentCreation:
    def test_create_post(self, agent, db):
        entry = {
            "platform": "twitter",
            "post_type": "post",
            "topic": "Building in public",
            "angle": "monthly revenue update",
            "target_audience": "founders",
        }
        post = agent._create_post(entry)
        assert post is not None
        assert post["platform"] == "twitter"
        assert post["post_id"] > 0

    def test_post_stored_in_db(self, agent, db):
        agent._create_post({
            "platform": "linkedin",
            "post_type": "article",
            "topic": "AI automation",
        })
        rows = db.execute("SELECT * FROM own_social_posts")
        assert len(rows) == 1
        assert rows[0]["platform"] == "linkedin"
        assert rows[0]["status"] == "draft"

    def test_create_post_empty_content(self, agent, llm):
        llm.quick.return_value = ""
        result = agent._create_post({"platform": "twitter", "post_type": "post"})
        assert result is None


# ── Engagement ───────────────────────────────────────────────


class TestEngagement:
    def test_plan_engagement_empty_when_no_active(self, agent):
        result = agent._plan_engagement([{"platform": "twitter", "status": "planned"}])
        assert result == []

    def test_plan_engagement_with_active(self, agent, llm):
        llm.chat_json.return_value = {
            "actions": [
                {"platform": "reddit", "action_type": "comment",
                 "target_description": "r/SaaS question about pricing",
                 "our_approach": "Share our experience with value-based pricing"},
            ]
        }
        result = agent._plan_engagement([{"platform": "reddit", "status": "active"}])
        assert len(result) == 1
        assert result[0]["action_type"] == "comment"

    def test_engagement_logged(self, agent, db, llm):
        llm.chat_json.return_value = {
            "actions": [
                {"platform": "twitter", "action_type": "reply",
                 "target_description": "founder tweet",
                 "our_approach": "agree and add insight"},
            ]
        }
        agent._plan_engagement([{"platform": "twitter", "status": "active"}])
        rows = db.execute("SELECT * FROM social_engagement_log")
        assert len(rows) == 1


# ── Metrics ──────────────────────────────────────────────────


class TestMetrics:
    def test_check_metrics_empty(self, agent):
        metrics = agent._check_metrics()
        assert metrics == {}

    def test_check_metrics_with_data(self, agent, db):
        db.execute_insert(
            "INSERT INTO own_social_posts "
            "(platform, post_type, content, status, engagement_likes, engagement_comments) "
            "VALUES ('twitter', 'post', 'test', 'posted', 50, 10)"
        )
        metrics = agent._check_metrics()
        assert "twitter" in metrics
        assert metrics["twitter"]["total_likes"] == 50

    def test_update_metrics(self, agent, db):
        post_id = db.execute_insert(
            "INSERT INTO own_social_posts "
            "(platform, post_type, content, status) "
            "VALUES ('twitter', 'post', 'test', 'posted')"
        )
        agent.update_metrics(post_id, likes=100, comments=20, shares=5, clicks=30, leads=2)
        row = db.execute("SELECT * FROM own_social_posts WHERE id = ?", (post_id,))[0]
        assert row["engagement_likes"] == 100
        assert row["leads_generated"] == 2

    def test_update_followers(self, agent, db):
        agent._get_account_status()
        agent.update_followers("twitter", 1500)
        row = db.execute(
            "SELECT followers FROM own_social_accounts WHERE platform = 'twitter'"
        )[0]
        assert row["followers"] == 1500

    def test_get_follower_growth(self, agent, db):
        agent._get_account_status()
        agent.setup_account("twitter", "monai_bot")
        agent.update_followers("twitter", 500)
        growth = agent.get_follower_growth()
        assert len(growth) == 1
        assert growth[0]["followers"] == 500

    def test_best_performing_content_empty(self, agent):
        result = agent.get_best_performing_content()
        assert result == []

    def test_best_performing_content_ranked(self, agent, db):
        # Post A: lower engagement
        db.execute_insert(
            "INSERT INTO own_social_posts "
            "(platform, post_type, content, status, engagement_likes, engagement_shares) "
            "VALUES ('twitter', 'post', 'post A', 'posted', 10, 1)"
        )
        # Post B: higher engagement
        db.execute_insert(
            "INSERT INTO own_social_posts "
            "(platform, post_type, content, status, engagement_likes, engagement_shares) "
            "VALUES ('twitter', 'thread', 'post B', 'posted', 100, 50)"
        )
        result = agent.get_best_performing_content(limit=2)
        assert len(result) == 2
        assert result[0]["content"] == "post B"  # Higher score first


# ── Full Run ─────────────────────────────────────────────────


class TestRun:
    def test_run_with_no_active_accounts(self, agent, llm):
        llm.chat_json.return_value = {"calendar": [], "actions": []}
        result = agent.run()
        assert result["active_accounts"] == 0
        assert result["posts_created"] == 0

    def test_run_with_active_account(self, agent, db, llm):
        # Set up an active account
        agent._get_account_status()
        agent.setup_account("twitter", "monai_bot")

        llm.chat_json.side_effect = [
            # _plan_content
            {"calendar": [
                {"platform": "twitter", "post_type": "post",
                 "topic": "Revenue update", "angle": "transparency",
                 "target_audience": "founders"},
            ]},
            # _plan_engagement
            {"actions": [
                {"platform": "twitter", "action_type": "reply",
                 "target_description": "founder", "our_approach": "helpful reply"},
            ]},
        ]

        result = agent.run()
        assert result["active_accounts"] == 1
        assert result["posts_created"] == 1
        assert result["engagement_actions"] == 1


# ── Platform Strategies ──────────────────────────────────────


class TestPlatformStrategies:
    def test_all_platforms_have_strategies(self):
        for platform in ("twitter", "linkedin", "reddit", "indie_hackers"):
            assert platform in PLATFORM_STRATEGIES

    def test_content_mix_sums_to_one(self):
        for platform, strategy in PLATFORM_STRATEGIES.items():
            total = sum(strategy["content_mix"].values())
            assert abs(total - 1.0) < 0.01, f"{platform} content mix sums to {total}"

    def test_reddit_has_subreddits(self):
        assert len(PLATFORM_STRATEGIES["reddit"]["subreddits"]) > 5
