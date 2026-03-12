"""Tests for content review integration in strategies that were missing it.

Verifies that affiliate, content_sites, newsletter, freelance_writing,
social_media, and print_on_demand all include quality review steps.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from monai.config import Config
from monai.db.database import Database


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(Path(path))
    yield database
    os.unlink(path)


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    return cfg


@pytest.fixture
def llm():
    mock = MagicMock()
    mock.quick.return_value = "test"
    mock.chat_json.return_value = {
        "completeness": 0.8, "actionability": 0.7, "uniqueness": 0.6,
        "value_for_money": 0.7, "professionalism": 0.8, "overall": 0.72,
        "issues": [], "suggestions": [],
    }
    mock.chat.return_value = "Test content"
    mock.get_model.return_value = "gpt-4"
    return mock


# ── Affiliate ────────────────────────────────────────────────────


class TestAffiliateContentReview:
    def test_plan_includes_review_step(self, config, db, llm):
        from monai.agents.product_reviewer import REVIEW_SCHEMA
        with db.connect() as conn:
            conn.executescript(REVIEW_SCHEMA)

        from monai.strategies.affiliate import AffiliateAgent
        agent = AffiliateAgent(config, db, llm)

        # Write a draft content file
        draft_path = agent.content_dir / "review_test.json"
        draft_path.write_text(json.dumps({
            "type": "review",
            "target": {"product_name": "TestProduct"},
            "sections": [{"section": "intro", "content": "Great product"}],
            "status": "draft",
        }))

        steps = agent.plan()
        assert "review_content" in steps

    def test_review_content_approves(self, config, db, llm):
        from monai.agents.product_reviewer import REVIEW_SCHEMA, ReviewResult
        with db.connect() as conn:
            conn.executescript(REVIEW_SCHEMA)

        from monai.strategies.affiliate import AffiliateAgent
        agent = AffiliateAgent(config, db, llm)

        draft_path = agent.content_dir / "review_test.json"
        draft_path.write_text(json.dumps({
            "type": "review",
            "target": {"product_name": "GoodProduct"},
            "sections": [{"section": "intro", "content": "Excellent product review"}],
            "status": "draft",
        }))

        mock_result = ReviewResult(verdict="approved", quality_score=0.9)
        with patch.object(agent, "_reviewer", create=True) as mock_rev:
            mock_rev.review_product.return_value = mock_result
            type(agent).reviewer = PropertyMock(return_value=mock_rev)
            result = agent._review_content()

        # Check file was updated
        data = json.loads(draft_path.read_text())
        assert data["status"] == "reviewed"


# ── Content Sites ────────────────────────────────────────────────


class TestContentSitesContentReview:
    def test_plan_includes_review_step(self, config, db, llm):
        from monai.strategies.content_sites import ContentSiteAgent
        agent = ContentSiteAgent(config, db, llm)

        draft_path = agent.sites_dir / "article_test.json"
        draft_path.write_text(json.dumps({
            "target": {"keyword": "best tools"},
            "sections": [{"heading": "Intro", "content": "Here are the best tools"}],
            "status": "draft",
        }))

        steps = agent.plan()
        assert "review_content" in steps

    def test_reviewed_advances_pipeline(self, config, db, llm):
        from monai.strategies.content_sites import ContentSiteAgent
        agent = ContentSiteAgent(config, db, llm)

        # Write a reviewed file
        reviewed_path = agent.sites_dir / "article_reviewed.json"
        reviewed_path.write_text(json.dumps({
            "target": {"keyword": "reviewed tools"},
            "sections": [],
            "status": "reviewed",
        }))

        steps = agent.plan()
        assert "find_affiliate_programs" in steps


# ── Newsletter ───────────────────────────────────────────────────


class TestNewsletterReview:
    def test_plan_includes_review_for_draft_issues(self, config, db, llm):
        from monai.strategies.newsletter import NewsletterAgent, NEWSLETTER_SCHEMA
        with db.connect() as conn:
            conn.executescript(NEWSLETTER_SCHEMA)

        agent = NewsletterAgent(config, db, llm)

        # Insert a newsletter and a draft issue
        nl_id = db.execute_insert(
            "INSERT INTO newsletters (name, niche, status) VALUES (?, ?, ?)",
            ("Test NL", "tech", "growing"),
        )
        db.execute_insert(
            "INSERT INTO newsletter_issues (newsletter_id, subject, status) "
            "VALUES (?, ?, ?)",
            (nl_id, "Test Issue", "draft"),
        )

        steps = agent.plan()
        assert "review_issue" in steps


# ── Social Media ─────────────────────────────────────────────────


class TestSocialMediaReview:
    def test_plan_includes_review_for_drafts(self, config, db, llm):
        from monai.strategies.social_media import SocialMediaAgent, SMM_SCHEMA
        with db.connect() as conn:
            conn.executescript(SMM_SCHEMA)

        agent = SocialMediaAgent(config, db, llm)

        # Insert an active client and draft post
        client_id = db.execute_insert(
            "INSERT INTO smm_clients (client_name, platforms, status) VALUES (?, ?, ?)",
            ("TestCo", '["twitter"]', "active"),
        )
        db.execute_insert(
            "INSERT INTO social_posts (client_id, platform, content, status) "
            "VALUES (?, ?, ?, ?)",
            (client_id, "twitter", "Check out our new product!", "draft"),
        )

        steps = agent.plan()
        assert "review_posts" in steps


# ── Print on Demand ──────────────────────────────────────────────


class TestPrintOnDemandReview:
    def test_plan_includes_review_step(self, config, db, llm):
        from monai.strategies.print_on_demand import PrintOnDemandAgent, POD_SCHEMA
        with db.connect() as conn:
            conn.executescript(POD_SCHEMA)

        agent = PrintOnDemandAgent(config, db, llm)

        # Insert a designed product
        db.execute_insert(
            "INSERT INTO pod_designs (title, niche, status) VALUES (?, ?, ?)",
            ("Cool Design", "tech", "designed"),
        )

        steps = agent.plan()
        assert "review_design" in steps

    def test_reviewed_advances_to_listing(self, config, db, llm):
        from monai.strategies.print_on_demand import PrintOnDemandAgent, POD_SCHEMA
        with db.connect() as conn:
            conn.executescript(POD_SCHEMA)

        agent = PrintOnDemandAgent(config, db, llm)

        db.execute_insert(
            "INSERT INTO pod_designs (title, niche, status) VALUES (?, ?, ?)",
            ("Reviewed Design", "tech", "reviewed"),
        )

        steps = agent.plan()
        assert "create_listings" in steps


# ── Freelance Writing ────────────────────────────────────────────


class TestFreelanceWritingReview:
    def test_plan_includes_review_for_written_content(self, config, db, llm):
        from monai.strategies.freelance_writing import FreelanceWritingAgent
        agent = FreelanceWritingAgent(config, db, llm)

        # Create necessary tables
        with db.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS strategies (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'services',
                    description TEXT DEFAULT '',
                    allocated_budget REAL DEFAULT 0,
                    status TEXT DEFAULT 'active'
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY, contact_id INTEGER, strategy_id INTEGER,
                    title TEXT, description TEXT, status TEXT, paid_amount REAL DEFAULT 0,
                    quoted_amount REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY, name TEXT, platform TEXT, email TEXT,
                    stage TEXT DEFAULT 'prospect', notes TEXT, source_strategy TEXT
                );
            """)

        strategy_id = db.execute_insert(
            "INSERT INTO strategies (name, category) VALUES (?, ?)",
            ("freelance_writing", "services"),
        )
        contact_id = db.execute_insert(
            "INSERT INTO contacts (name, platform, stage) VALUES (?, ?, ?)",
            ("Client", "upwork", "client"),
        )
        db.execute_insert(
            "INSERT INTO projects (contact_id, strategy_id, title, status) VALUES (?, ?, ?, ?)",
            (contact_id, strategy_id, "Test Project", "written"),
        )

        steps = agent.plan()
        assert "review_content" in steps
