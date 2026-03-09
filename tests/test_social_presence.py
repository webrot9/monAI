"""Tests for SocialPresence agent — per-brand social media management."""

import json
from unittest.mock import MagicMock, patch

import pytest

from monai.agents.social_presence import (
    BRAND_PLATFORMS,
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
        for table in ("brand_social_accounts", "brand_social_posts",
                      "brand_content_calendar", "brand_engagement_log"):
            rows = db.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            assert len(rows) == 1, f"Table {table} not created"

    def test_plan(self, agent):
        steps = agent.plan()
        assert len(steps) >= 4


# ── Brand Registration ───────────────────────────────────────


class TestBrandRegistration:
    def test_register_brand_default_platforms(self, agent, db):
        accounts = agent.register_brand("micro_saas")
        platforms = {a["platform"] for a in accounts}
        assert platforms == set(BRAND_PLATFORMS["micro_saas"])

    def test_register_brand_custom_platforms(self, agent, db):
        accounts = agent.register_brand("newsletter", platforms=["twitter"])
        assert len(accounts) == 1
        assert accounts[0]["platform"] == "twitter"

    def test_register_brand_with_voice(self, agent, db):
        agent.register_brand("freelance_writing", brand_voice="professional, witty")
        rows = db.execute(
            "SELECT brand_voice FROM brand_social_accounts "
            "WHERE brand = 'freelance_writing'"
        )
        assert all(r["brand_voice"] == "professional, witty" for r in rows)

    def test_register_unknown_brand_defaults_to_twitter(self, agent):
        accounts = agent.register_brand("unknown_biz")
        assert len(accounts) == 1
        assert accounts[0]["platform"] == "twitter"

    def test_register_brand_idempotent(self, agent, db):
        agent.register_brand("micro_saas")
        agent.register_brand("micro_saas")  # duplicate
        rows = db.execute(
            "SELECT * FROM brand_social_accounts WHERE brand = 'micro_saas'"
        )
        assert len(rows) == len(BRAND_PLATFORMS["micro_saas"])

    def test_get_brands(self, agent):
        agent.register_brand("micro_saas")
        agent.register_brand("newsletter")
        brands = agent._get_brands()
        assert set(brands) == {"micro_saas", "newsletter"}

    def test_get_brands_filtered(self, agent):
        agent.register_brand("micro_saas")
        agent.register_brand("newsletter")
        brands = agent._get_brands("newsletter")
        assert brands == ["newsletter"]


# ── Account Management ───────────────────────────────────────


class TestAccountManagement:
    def test_get_brand_accounts(self, agent, db):
        agent.register_brand("saas")
        accounts = agent._get_brand_accounts("saas")
        assert len(accounts) == len(BRAND_PLATFORMS["saas"])
        assert all(a["brand"] == "saas" for a in accounts)

    def test_setup_account(self, agent, db):
        agent.register_brand("micro_saas")
        result = agent.setup_account("micro_saas", "twitter",
                                     "saas_builder", "https://x.com/saas_builder")
        assert result["status"] == "active"
        assert result["brand"] == "micro_saas"
        assert result["username"] == "saas_builder"

        rows = db.execute(
            "SELECT * FROM brand_social_accounts "
            "WHERE brand = 'micro_saas' AND platform = 'twitter'"
        )
        assert rows[0]["username"] == "saas_builder"
        assert rows[0]["status"] == "active"

    def test_setup_shares_knowledge(self, agent, db):
        agent.register_brand("newsletter")
        agent.setup_account("newsletter", "linkedin", "newsletter_co")
        rows = db.execute(
            "SELECT * FROM knowledge WHERE topic = 'social_newsletter_linkedin'"
        )
        assert len(rows) == 1

    def test_multiple_brands_isolated(self, agent, db):
        agent.register_brand("micro_saas")
        agent.register_brand("newsletter")
        agent.setup_account("micro_saas", "twitter", "saas_bot")
        agent.setup_account("newsletter", "twitter", "news_bot")

        saas_accs = agent._get_brand_accounts("micro_saas")
        news_accs = agent._get_brand_accounts("newsletter")

        saas_twitter = [a for a in saas_accs if a["platform"] == "twitter"][0]
        news_twitter = [a for a in news_accs if a["platform"] == "twitter"][0]

        assert saas_twitter["username"] == "saas_bot"
        assert news_twitter["username"] == "news_bot"


# ── Content Planning ─────────────────────────────────────────


class TestContentPlanning:
    def test_plan_content_empty_when_no_active(self, agent):
        agent.register_brand("micro_saas")
        calendar = agent._plan_content(
            "micro_saas",
            [{"platform": "twitter", "status": "planned"}],
        )
        assert calendar == []

    def test_plan_content_with_active_account(self, agent, llm):
        llm.chat_json.return_value = {
            "calendar": [
                {"platform": "twitter", "post_type": "thread",
                 "topic": "How our SaaS hit 100 users", "angle": "growth story",
                 "target_audience": "indie hackers"},
            ]
        }
        accounts = [{"platform": "twitter", "status": "active",
                      "brand_voice": "technical and direct"}]
        calendar = agent._plan_content("micro_saas", accounts)
        assert len(calendar) == 1
        assert calendar[0]["platform"] == "twitter"

    def test_calendar_stored_with_brand(self, agent, db, llm):
        llm.chat_json.return_value = {
            "calendar": [
                {"platform": "twitter", "post_type": "post",
                 "topic": "Test", "angle": "test angle"},
            ]
        }
        agent._plan_content(
            "newsletter",
            [{"platform": "twitter", "status": "active", "brand_voice": ""}],
        )
        rows = db.execute(
            "SELECT * FROM brand_content_calendar WHERE brand = 'newsletter'"
        )
        assert len(rows) == 1


# ── Content Creation ─────────────────────────────────────────


class TestContentCreation:
    def test_create_post_for_brand(self, agent, db):
        agent.register_brand("micro_saas")
        entry = {
            "platform": "twitter",
            "post_type": "post",
            "topic": "Building in public",
            "angle": "monthly MRR update",
            "target_audience": "founders",
        }
        post = agent._create_post("micro_saas", entry)
        assert post is not None
        assert post["brand"] == "micro_saas"
        assert post["platform"] == "twitter"
        assert post["post_id"] > 0

    def test_post_stored_with_brand(self, agent, db):
        agent.register_brand("newsletter")
        agent._create_post("newsletter", {
            "platform": "linkedin",
            "post_type": "article",
            "topic": "Growing a newsletter to 10k subs",
        })
        rows = db.execute(
            "SELECT * FROM brand_social_posts WHERE brand = 'newsletter'"
        )
        assert len(rows) == 1
        assert rows[0]["platform"] == "linkedin"

    def test_create_post_empty_content(self, agent, llm):
        agent.register_brand("micro_saas")
        llm.quick.return_value = ""
        result = agent._create_post("micro_saas",
                                     {"platform": "twitter", "post_type": "post"})
        assert result is None

    def test_posts_isolated_between_brands(self, agent, db):
        agent.register_brand("micro_saas")
        agent.register_brand("newsletter")
        agent._create_post("micro_saas", {"platform": "twitter",
                                           "post_type": "post", "topic": "SaaS thing"})
        agent._create_post("newsletter", {"platform": "twitter",
                                           "post_type": "post", "topic": "Newsletter thing"})
        saas_posts = db.execute(
            "SELECT * FROM brand_social_posts WHERE brand = 'micro_saas'"
        )
        news_posts = db.execute(
            "SELECT * FROM brand_social_posts WHERE brand = 'newsletter'"
        )
        assert len(saas_posts) == 1
        assert len(news_posts) == 1


# ── Engagement ───────────────────────────────────────────────


class TestEngagement:
    def test_plan_engagement_empty_when_no_active(self, agent):
        result = agent._plan_engagement(
            "micro_saas",
            [{"platform": "twitter", "status": "planned"}],
        )
        assert result == []

    def test_plan_engagement_with_active(self, agent, llm):
        llm.chat_json.return_value = {
            "actions": [
                {"platform": "reddit", "action_type": "comment",
                 "target_description": "r/SaaS question about pricing",
                 "our_approach": "Share our experience with value-based pricing"},
            ]
        }
        result = agent._plan_engagement(
            "micro_saas",
            [{"platform": "reddit", "status": "active"}],
        )
        assert len(result) == 1
        assert result[0]["action_type"] == "comment"

    def test_engagement_logged_with_brand(self, agent, db, llm):
        llm.chat_json.return_value = {
            "actions": [
                {"platform": "twitter", "action_type": "reply",
                 "target_description": "founder tweet",
                 "our_approach": "agree and add insight"},
            ]
        }
        agent._plan_engagement(
            "newsletter",
            [{"platform": "twitter", "status": "active"}],
        )
        rows = db.execute(
            "SELECT * FROM brand_engagement_log WHERE brand = 'newsletter'"
        )
        assert len(rows) == 1


# ── Metrics ──────────────────────────────────────────────────


class TestMetrics:
    def test_check_metrics_empty(self, agent):
        metrics = agent._check_metrics("micro_saas")
        assert metrics == {}

    def test_check_metrics_with_data(self, agent, db):
        db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, status, "
            "engagement_likes, engagement_comments) "
            "VALUES ('micro_saas', 'twitter', 'post', 'test', 'posted', 50, 10)"
        )
        metrics = agent._check_metrics("micro_saas")
        assert "twitter" in metrics
        assert metrics["twitter"]["total_likes"] == 50

    def test_metrics_isolated_per_brand(self, agent, db):
        db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, status, engagement_likes) "
            "VALUES ('micro_saas', 'twitter', 'post', 'saas post', 'posted', 100)"
        )
        db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, status, engagement_likes) "
            "VALUES ('newsletter', 'twitter', 'post', 'news post', 'posted', 50)"
        )
        saas_metrics = agent._check_metrics("micro_saas")
        news_metrics = agent._check_metrics("newsletter")
        assert saas_metrics["twitter"]["total_likes"] == 100
        assert news_metrics["twitter"]["total_likes"] == 50

    def test_update_metrics(self, agent, db):
        post_id = db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, status) "
            "VALUES ('micro_saas', 'twitter', 'post', 'test', 'posted')"
        )
        agent.update_metrics(post_id, likes=100, comments=20, shares=5, clicks=30, leads=2)
        row = db.execute("SELECT * FROM brand_social_posts WHERE id = ?", (post_id,))[0]
        assert row["engagement_likes"] == 100
        assert row["leads_generated"] == 2

    def test_update_followers(self, agent, db):
        agent.register_brand("micro_saas")
        agent.update_followers("micro_saas", "twitter", 1500)
        row = db.execute(
            "SELECT followers FROM brand_social_accounts "
            "WHERE brand = 'micro_saas' AND platform = 'twitter'"
        )[0]
        assert row["followers"] == 1500

    def test_get_follower_growth_all(self, agent, db):
        agent.register_brand("micro_saas")
        agent.register_brand("newsletter")
        agent.setup_account("micro_saas", "twitter", "saas_bot")
        agent.setup_account("newsletter", "twitter", "news_bot")
        agent.update_followers("micro_saas", "twitter", 500)
        agent.update_followers("newsletter", "twitter", 300)
        growth = agent.get_follower_growth()
        assert len(growth) == 2
        brands = {g["brand"] for g in growth}
        assert brands == {"micro_saas", "newsletter"}

    def test_get_follower_growth_filtered(self, agent, db):
        agent.register_brand("micro_saas")
        agent.register_brand("newsletter")
        agent.setup_account("micro_saas", "twitter", "saas_bot")
        agent.setup_account("newsletter", "twitter", "news_bot")
        growth = agent.get_follower_growth(brand="micro_saas")
        assert len(growth) == 1
        assert growth[0]["brand"] == "micro_saas"

    def test_best_performing_content_empty(self, agent):
        result = agent.get_best_performing_content()
        assert result == []

    def test_best_performing_content_ranked(self, agent, db):
        db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, status, "
            "engagement_likes, engagement_shares) "
            "VALUES ('micro_saas', 'twitter', 'post', 'post A', 'posted', 10, 1)"
        )
        db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, status, "
            "engagement_likes, engagement_shares) "
            "VALUES ('micro_saas', 'twitter', 'thread', 'post B', 'posted', 100, 50)"
        )
        result = agent.get_best_performing_content(brand="micro_saas", limit=2)
        assert len(result) == 2
        assert result[0]["content"] == "post B"

    def test_best_performing_content_cross_brand(self, agent, db):
        db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, status, engagement_likes) "
            "VALUES ('micro_saas', 'twitter', 'post', 'saas', 'posted', 100)"
        )
        db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, status, engagement_likes) "
            "VALUES ('newsletter', 'twitter', 'post', 'news', 'posted', 50)"
        )
        # No brand filter — get both
        result = agent.get_best_performing_content(limit=10)
        assert len(result) == 2

    def test_get_all_brands_summary(self, agent, db):
        agent.register_brand("micro_saas")
        agent.register_brand("newsletter")
        agent.setup_account("micro_saas", "twitter", "saas_bot")
        summary = agent.get_all_brands_summary()
        assert len(summary) == 2
        brands = {s["brand"] for s in summary}
        assert brands == {"micro_saas", "newsletter"}


# ── Full Run ─────────────────────────────────────────────────


class TestRun:
    def test_run_no_brands(self, agent):
        result = agent.run()
        assert result["brands_processed"] == 0
        assert result["total_posts"] == 0

    def test_run_brand_no_active_accounts(self, agent, llm):
        agent.register_brand("micro_saas")
        result = agent.run()
        assert result["per_brand"]["micro_saas"]["status"] == "no_active_accounts"

    def test_run_single_brand_full_pipeline(self, agent, db, llm):
        agent.register_brand("micro_saas")
        agent.setup_account("micro_saas", "twitter", "saas_bot")

        llm.chat_json.side_effect = [
            # _plan_content
            {"calendar": [
                {"platform": "twitter", "post_type": "post",
                 "topic": "MRR update", "angle": "transparency",
                 "target_audience": "founders"},
            ]},
            # _plan_engagement
            {"actions": [
                {"platform": "twitter", "action_type": "reply",
                 "target_description": "founder", "our_approach": "helpful reply"},
            ]},
            # review_post (quality gate)
            {"approved": True, "score": 0.9, "reason": ""},
        ]

        result = agent.run()
        assert result["brands_processed"] == 1
        assert result["total_posts"] == 1
        assert result["total_engagement"] == 1
        assert result["review"]["approved"] == 1
        # No credentials = scheduled but publish fails gracefully
        assert result["scheduled"]["scheduled"] == 1

    def test_run_filtered_to_one_brand(self, agent, db, llm):
        agent.register_brand("micro_saas")
        agent.register_brand("newsletter")
        agent.setup_account("micro_saas", "twitter", "saas_bot")
        agent.setup_account("newsletter", "twitter", "news_bot")

        llm.chat_json.side_effect = [
            {"calendar": [{"platform": "twitter", "post_type": "post",
                           "topic": "SaaS only"}]},
            {"actions": []},
            # review
            {"approved": True, "score": 0.85, "reason": ""},
        ]

        result = agent.run(brand="micro_saas")
        assert result["brands_processed"] == 1
        assert "micro_saas" in result["per_brand"]
        assert "newsletter" not in result["per_brand"]


# ── Content Approval Flow ────────────────────────────────────


class TestApprovalFlow:
    def _insert_draft(self, agent, db, brand="micro_saas"):
        agent.register_brand(brand)
        return db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, topic, status) "
            "VALUES (?, 'twitter', 'post', 'Great content here', 'test topic', 'draft')",
            (brand,),
        )

    def test_review_approves_good_post(self, agent, db, llm):
        post_id = self._insert_draft(agent, db)
        llm.chat_json.return_value = {"approved": True, "score": 0.92, "reason": ""}
        result = agent.review_post(post_id)
        assert result["status"] == "approved"
        assert result["score"] == 0.92

        row = db.execute("SELECT * FROM brand_social_posts WHERE id = ?", (post_id,))[0]
        assert row["status"] == "approved"
        assert row["quality_score"] == 0.92
        assert row["approved_at"] is not None

    def test_review_rejects_bad_post(self, agent, db, llm):
        post_id = self._insert_draft(agent, db)
        llm.chat_json.return_value = {
            "approved": False, "score": 0.3, "reason": "Sounds too robotic",
        }
        result = agent.review_post(post_id)
        assert result["status"] == "rejected"
        assert result["reason"] == "Sounds too robotic"

        row = db.execute("SELECT * FROM brand_social_posts WHERE id = ?", (post_id,))[0]
        assert row["status"] == "rejected"
        assert row["rejection_reason"] == "Sounds too robotic"

    def test_review_nonexistent_post(self, agent):
        result = agent.review_post(9999)
        assert result["status"] == "not_found"

    def test_review_already_reviewed(self, agent, db, llm):
        post_id = self._insert_draft(agent, db)
        llm.chat_json.return_value = {"approved": True, "score": 0.9, "reason": ""}
        agent.review_post(post_id)
        result = agent.review_post(post_id)
        assert result["status"] == "already_reviewed"

    def test_review_all_drafts(self, agent, db, llm):
        self._insert_draft(agent, db)
        self._insert_draft(agent, db)
        llm.chat_json.side_effect = [
            {"approved": True, "score": 0.9, "reason": ""},
            {"approved": False, "score": 0.2, "reason": "bad"},
        ]
        result = agent.review_all_drafts()
        assert result["approved"] == 1
        assert result["rejected"] == 1
        assert result["total"] == 2

    def test_schedule_approved_post(self, agent, db, llm):
        post_id = self._insert_draft(agent, db)
        llm.chat_json.return_value = {"approved": True, "score": 0.9, "reason": ""}
        agent.review_post(post_id)

        result = agent.schedule_post(post_id)
        assert result["status"] == "scheduled"

        row = db.execute("SELECT * FROM brand_social_posts WHERE id = ?", (post_id,))[0]
        assert row["status"] == "scheduled"

    def test_schedule_unapproved_post_fails(self, agent, db):
        post_id = self._insert_draft(agent, db)
        result = agent.schedule_post(post_id)
        assert result["status"] == "not_approved"

    def test_schedule_all_approved(self, agent, db, llm):
        p1 = self._insert_draft(agent, db)
        p2 = self._insert_draft(agent, db)
        llm.chat_json.side_effect = [
            {"approved": True, "score": 0.9, "reason": ""},
            {"approved": True, "score": 0.85, "reason": ""},
        ]
        agent.review_post(p1)
        agent.review_post(p2)
        result = agent.schedule_all_approved()
        assert result["scheduled"] == 2


# ── Publishing ───────────────────────────────────────────────


class TestPublishing:
    def _setup_scheduled_post(self, agent, db, llm, with_creds=True):
        """Helper: create a brand, account, draft, approve, schedule."""
        agent.register_brand("micro_saas")
        creds = {"bearer_token": "tok", "user_id": "123"} if with_creds else None
        agent.setup_account("micro_saas", "twitter", "saas_bot",
                            credentials=creds)

        post_id = db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, topic, status) "
            "VALUES ('micro_saas', 'twitter', 'post', 'Ship fast!', 'shipping', 'scheduled')"
        )
        return post_id

    @patch("monai.agents.social_presence.create_platform_client")
    def test_publish_success(self, mock_create, agent, db, llm):
        post_id = self._setup_scheduled_post(agent, db, llm)

        mock_client = MagicMock()
        mock_client.post.return_value = {
            "post_id": "tweet_999", "url": "https://x.com/i/status/tweet_999",
        }
        mock_create.return_value = mock_client

        result = agent.publish_post(post_id)
        assert result["status"] == "posted"
        assert result["platform_post_id"] == "tweet_999"

        row = db.execute("SELECT * FROM brand_social_posts WHERE id = ?", (post_id,))[0]
        assert row["status"] == "posted"
        assert row["platform_post_id"] == "tweet_999"
        assert row["posted_at"] is not None

    @patch("monai.agents.social_presence.create_platform_client")
    def test_publish_api_error(self, mock_create, agent, db, llm):
        from monai.social.api import SocialAPIError
        post_id = self._setup_scheduled_post(agent, db, llm)

        mock_client = MagicMock()
        mock_client.post.side_effect = SocialAPIError("Rate limited")
        mock_create.return_value = mock_client

        result = agent.publish_post(post_id)
        assert result["status"] == "failed"
        assert "Rate limited" in result["error"]

        row = db.execute("SELECT * FROM brand_social_posts WHERE id = ?", (post_id,))[0]
        assert row["status"] == "failed"

    def test_publish_no_credentials(self, agent, db, llm):
        post_id = self._setup_scheduled_post(agent, db, llm, with_creds=False)
        result = agent.publish_post(post_id)
        assert result["status"] == "no_credentials"

    def test_publish_not_scheduled(self, agent, db):
        agent.register_brand("micro_saas")
        post_id = db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, status) "
            "VALUES ('micro_saas', 'twitter', 'post', 'draft content', 'draft')"
        )
        result = agent.publish_post(post_id)
        assert result["status"] == "not_scheduled"

    @patch("monai.agents.social_presence.create_platform_client")
    def test_publish_all_scheduled(self, mock_create, agent, db, llm):
        agent.register_brand("micro_saas")
        agent.setup_account("micro_saas", "twitter", "saas_bot",
                            credentials={"bearer_token": "tok", "user_id": "123"})

        for i in range(3):
            db.execute_insert(
                "INSERT INTO brand_social_posts "
                "(brand, platform, post_type, content, status) "
                f"VALUES ('micro_saas', 'twitter', 'post', 'Post {i}', 'scheduled')"
            )

        mock_client = MagicMock()
        mock_client.post.return_value = {"post_id": "tw_x", "url": "https://x.com/tw_x"}
        mock_create.return_value = mock_client

        result = agent.publish_all_scheduled()
        assert result["posted"] == 3
        assert result["failed"] == 0


# ── Analytics Pull ───────────────────────────────────────────


class TestAnalyticsPull:
    def _setup_posted_post(self, agent, db):
        agent.register_brand("micro_saas")
        agent.setup_account("micro_saas", "twitter", "saas_bot",
                            credentials={"bearer_token": "tok", "user_id": "123"})
        return db.execute_insert(
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, status, platform_post_id) "
            "VALUES ('micro_saas', 'twitter', 'post', 'test', 'posted', 'tweet_100')"
        )

    @patch("monai.agents.social_presence.create_platform_client")
    def test_pull_post_metrics(self, mock_create, agent, db):
        post_id = self._setup_posted_post(agent, db)

        mock_client = MagicMock()
        mock_client.get_post_metrics.return_value = {
            "likes": 42, "comments": 5, "shares": 10, "clicks": 100,
        }
        mock_create.return_value = mock_client

        result = agent.pull_post_metrics(post_id)
        assert result["status"] == "updated"
        assert result["metrics"]["likes"] == 42

        row = db.execute("SELECT * FROM brand_social_posts WHERE id = ?", (post_id,))[0]
        assert row["engagement_likes"] == 42
        assert row["engagement_shares"] == 10

    @patch("monai.agents.social_presence.create_platform_client")
    def test_pull_all_metrics(self, mock_create, agent, db):
        self._setup_posted_post(agent, db)
        self._setup_posted_post(agent, db)

        mock_client = MagicMock()
        mock_client.get_post_metrics.return_value = {
            "likes": 10, "comments": 2, "shares": 1, "clicks": 50,
        }
        mock_create.return_value = mock_client

        result = agent.pull_all_metrics()
        assert result["updated"] == 2

    def test_pull_metrics_not_posted(self, agent):
        result = agent.pull_post_metrics(9999)
        assert result["status"] == "not_found_or_not_posted"

    @patch("monai.agents.social_presence.create_platform_client")
    def test_pull_follower_counts(self, mock_create, agent, db):
        agent.register_brand("micro_saas")
        agent.setup_account("micro_saas", "twitter", "saas_bot",
                            credentials={"bearer_token": "tok", "user_id": "123"})

        mock_client = MagicMock()
        mock_client.get_profile_metrics.return_value = {"followers": 2500}
        mock_create.return_value = mock_client

        result = agent.pull_follower_counts()
        assert result["updated"] == 1

        row = db.execute(
            "SELECT followers FROM brand_social_accounts "
            "WHERE brand = 'micro_saas' AND platform = 'twitter'"
        )[0]
        assert row["followers"] == 2500

    @patch("monai.agents.social_presence.create_platform_client")
    def test_pull_follower_counts_api_error(self, mock_create, agent, db):
        agent.register_brand("micro_saas")
        agent.setup_account("micro_saas", "twitter", "saas_bot",
                            credentials={"bearer_token": "tok", "user_id": "123"})

        mock_create.side_effect = Exception("API down")

        result = agent.pull_follower_counts()
        assert result["failed"] == 1


# ── Platform & Brand Config ──────────────────────────────────


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

    def test_all_strategies_have_platform_recommendations(self):
        expected = [
            "freelance_writing", "digital_products", "content_sites",
            "micro_saas", "telegram_bots", "affiliate", "newsletter",
            "lead_gen", "social_media", "course_creation", "domain_flipping",
            "print_on_demand", "saas", "cold_outreach",
        ]
        for strategy in expected:
            assert strategy in BRAND_PLATFORMS, f"Missing platform recs for {strategy}"

    def test_all_brand_platforms_are_valid(self):
        for brand, platforms in BRAND_PLATFORMS.items():
            for p in platforms:
                assert p in PLATFORM_STRATEGIES, (
                    f"Brand {brand} references unknown platform {p}"
                )
