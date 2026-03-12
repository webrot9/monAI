"""Tests for ProductReviewer quality gate agent."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from monai.agents.product_reviewer import ProductReviewer, ReviewResult, REVIEW_SCHEMA
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
    mock.quick.return_value = "Mocked LLM response"
    mock.chat_json.return_value = {
        "completeness": 0.8,
        "actionability": 0.7,
        "uniqueness": 0.6,
        "value_for_money": 0.7,
        "professionalism": 0.8,
        "overall": 0.72,
        "issues": [],
        "suggestions": [],
    }
    mock.get_model.return_value = "gpt-4"
    return mock


@pytest.fixture
def reviewer(config, db, llm):
    return ProductReviewer(config, db, llm)


class TestReviewResult:
    def test_defaults(self):
        r = ReviewResult()
        assert r.verdict == "needs_revision"
        assert r.quality_score == 0.0
        assert r.issues == []
        assert r.suggestions == []

    def test_to_dict(self):
        r = ReviewResult(verdict="approved", quality_score=0.85)
        d = r.to_dict()
        assert d["verdict"] == "approved"
        assert d["quality_score"] == 0.85
        assert "improved_content" not in d  # Not in dict output


class TestProductReviewer:
    def test_init_creates_table(self, reviewer, db):
        """Review table should be created on init."""
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='product_reviews'")
        assert len(rows) == 1

    def test_extract_content_from_spec(self, reviewer):
        """Should extract text from product spec/design."""
        data = {
            "spec": {
                "title": "My Product",
                "description": "A great product",
                "features": ["feature1", "feature2"],
            },
        }
        text = reviewer._extract_content(data, "digital_product")
        assert "My Product" in text
        assert "A great product" in text
        assert "feature1" in text

    def test_extract_content_from_design(self, reviewer):
        """Should extract text from design field."""
        data = {
            "design": {
                "name": "Bot X",
                "tagline": "Best bot ever",
                "features": [
                    {"name": "Auto-reply", "description": "Responds instantly"},
                ],
            },
        }
        text = reviewer._extract_content(data, "bot")
        assert "Bot X" in text
        assert "Best bot ever" in text
        assert "Auto-reply" in text

    def test_extract_content_with_sections(self, reviewer):
        """Should extract content sections."""
        data = {
            "spec": {"title": "Guide"},
            "content": [
                {"section": "Chapter 1", "content": "Introduction text here"},
                {"section": "Chapter 2", "content": "More content here"},
            ],
        }
        text = reviewer._extract_content(data, "digital_product")
        assert "Chapter 1" in text
        assert "Introduction text here" in text

    def test_extract_content_empty(self, reviewer):
        """Empty product data should return empty string."""
        text = reviewer._extract_content({}, "digital_product")
        assert text == ""

    def test_review_product_approved(self, reviewer, db):
        """Product with good scores should be approved."""
        # Mock all sub-reviewers
        reviewer._humanizer = MagicMock()
        reviewer._humanizer.humanize.return_value = "Improved content"
        reviewer._humanizer.get_quality_stats.return_value = {"avg_quality_score": 0.85}

        reviewer._fact_checker = MagicMock()
        reviewer._fact_checker.check.return_value = {
            "verdict": "publish",
            "accuracy_score": 0.95,
            "claims_found": 3,
            "claims_verified": 3,
        }

        reviewer._legal_factory = MagicMock()
        reviewer._legal_factory.assess_activity.return_value = {
            "status": "approved",
            "risk_level": "low",
            "blockers": [],
        }

        product_data = {
            "spec": {
                "title": "Test Product",
                "description": "A valuable guide",
                "features": ["Feature A"],
            },
            "content": [
                {"section": "Intro", "content": "Good content here with real value"},
            ],
        }

        result = reviewer.review_product(
            strategy="digital_products",
            product_name="Test Product",
            product_data=product_data,
            product_type="digital_product",
        )

        assert result.verdict == "approved"
        assert result.quality_score > 0.7
        assert result.factcheck_verdict == "publish"
        assert result.legal_status == "approved"

        # Should be saved to DB
        rows = db.execute("SELECT * FROM product_reviews")
        assert len(rows) == 1
        assert rows[0]["verdict"] == "approved"

    def test_review_product_rejected_by_factcheck(self, reviewer, db):
        """Product with false claims should be rejected."""
        reviewer._humanizer = MagicMock()
        reviewer._humanizer.humanize.return_value = "content"
        reviewer._humanizer.get_quality_stats.return_value = {"avg_quality_score": 0.8}

        reviewer._fact_checker = MagicMock()
        reviewer._fact_checker.check.return_value = {
            "verdict": "block",
            "accuracy_score": 0.3,
            "blocking_reasons": ["False claim about revenue statistics"],
        }

        reviewer._legal_factory = MagicMock()
        reviewer._legal_factory.assess_activity.return_value = {
            "status": "approved",
        }

        product_data = {
            "spec": {"title": "Bad Product", "description": "Has false claims"},
        }

        result = reviewer.review_product(
            strategy="digital_products",
            product_name="Bad Product",
            product_data=product_data,
            product_type="digital_product",
        )

        assert result.verdict == "rejected"
        assert result.factcheck_verdict == "block"
        assert any("False claim" in i for i in result.issues)

    def test_review_product_rejected_by_legal(self, reviewer, db):
        """Product with legal issues should be rejected."""
        reviewer._humanizer = MagicMock()
        reviewer._humanizer.humanize.return_value = "content"
        reviewer._humanizer.get_quality_stats.return_value = {"avg_quality_score": 0.8}

        reviewer._fact_checker = MagicMock()
        reviewer._fact_checker.check.return_value = {
            "verdict": "publish",
            "accuracy_score": 0.9,
        }

        reviewer._legal_factory = MagicMock()
        reviewer._legal_factory.assess_activity.return_value = {
            "status": "blocked",
            "blockers": ["Violates GDPR data collection rules"],
        }

        product_data = {
            "spec": {"title": "Legal Risk", "description": "Collects PII"},
        }

        result = reviewer.review_product(
            strategy="micro_saas",
            product_name="Legal Risk",
            product_data=product_data,
            product_type="saas",
        )

        assert result.verdict == "rejected"
        assert result.legal_status == "blocked"
        assert any("GDPR" in i for i in result.issues)

    def test_review_product_needs_revision(self, reviewer, db):
        """Low quality score should trigger needs_revision."""
        reviewer._humanizer = MagicMock()
        reviewer._humanizer.humanize.return_value = "content"
        reviewer._humanizer.get_quality_stats.return_value = {"avg_quality_score": 0.4}

        reviewer._fact_checker = MagicMock()
        reviewer._fact_checker.check.return_value = {
            "verdict": "revise",
            "accuracy_score": 0.6,
            "suggested_corrections": [
                {"original": "bad claim", "correction": "good claim"},
            ],
        }

        reviewer._legal_factory = MagicMock()
        reviewer._legal_factory.assess_activity.return_value = {
            "status": "approved",
        }

        # Override usability assessment to return low score
        reviewer.llm.chat_json.return_value = {
            "overall": 0.3,
            "issues": ["Generic content"],
            "suggestions": ["Add real examples"],
        }

        product_data = {
            "spec": {"title": "Meh Product", "description": "Average"},
        }

        result = reviewer.review_product(
            strategy="digital_products",
            product_name="Meh Product",
            product_data=product_data,
            product_type="digital_product",
        )

        assert result.verdict == "needs_revision"
        assert result.usability_score < 0.5
        assert len(result.suggestions) > 0

    def test_review_empty_product(self, reviewer, db):
        """Product with no content should be rejected."""
        result = reviewer.review_product(
            strategy="digital_products",
            product_name="Empty",
            product_data={},
            product_type="digital_product",
        )

        assert result.verdict == "rejected"
        assert any("No content" in i for i in result.issues)

    def test_review_saves_to_db(self, reviewer, db):
        """Every review should be persisted to the database."""
        reviewer._humanizer = MagicMock()
        reviewer._humanizer.humanize.return_value = "content"
        reviewer._humanizer.get_quality_stats.return_value = {"avg_quality_score": 0.9}

        reviewer._fact_checker = MagicMock()
        reviewer._fact_checker.check.return_value = {
            "verdict": "publish",
            "accuracy_score": 0.95,
        }

        reviewer._legal_factory = MagicMock()
        reviewer._legal_factory.assess_activity.return_value = {
            "status": "approved",
        }

        product_data = {
            "spec": {"title": "DB Test", "description": "Testing persistence"},
        }

        reviewer.review_product(
            strategy="test_strategy",
            product_name="DB Test",
            product_data=product_data,
            product_type="digital_product",
        )

        rows = db.execute("SELECT * FROM product_reviews WHERE strategy = 'test_strategy'")
        assert len(rows) == 1
        row = rows[0]
        assert row["product_name"] == "DB Test"
        assert row["strategy"] == "test_strategy"
        assert row["humanizer_score"] > 0
        assert row["factcheck_verdict"] == "publish"

    def test_humanizer_failure_graceful(self, reviewer, db):
        """If Humanizer fails, review should still proceed."""
        reviewer._humanizer = MagicMock()
        reviewer._humanizer.humanize.side_effect = Exception("Humanizer crashed")

        reviewer._fact_checker = MagicMock()
        reviewer._fact_checker.check.return_value = {
            "verdict": "publish",
            "accuracy_score": 0.9,
        }

        reviewer._legal_factory = MagicMock()
        reviewer._legal_factory.assess_activity.return_value = {
            "status": "approved",
        }

        product_data = {
            "spec": {"title": "Resilience Test", "description": "Should not crash"},
        }

        result = reviewer.review_product(
            strategy="test",
            product_name="Resilience Test",
            product_data=product_data,
            product_type="digital_product",
        )

        # Should not crash — graceful degradation
        assert result.humanizer_score == 0.5  # Fallback score
        assert result.verdict in ("approved", "needs_revision")


class TestStrategyPipelineIntegration:
    """Test that strategies correctly integrate the review step."""

    def test_telegram_bots_pipeline_has_review(self):
        """telegram_bots plan() should include review step after build."""
        from monai.strategies.telegram_bots import TelegramBotAgent
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Config()
            cfg.data_dir = Path(tmpdir)
            fd, dbpath = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            db = Database(Path(dbpath))
            llm = MagicMock()

            agent = TelegramBotAgent(cfg, db, llm)
            # Write a "built" product
            bot_dir = Path(tmpdir) / "telegram_bots"
            bot_dir.mkdir(parents=True, exist_ok=True)
            (bot_dir / "test_bot.json").write_text(json.dumps({
                "design": {"name": "Test Bot"},
                "status": "built",
            }))

            plan = agent.plan()
            assert plan == ["review_product"]

            # After review, status becomes "reviewed" → deploy
            (bot_dir / "test_bot.json").write_text(json.dumps({
                "design": {"name": "Test Bot"},
                "status": "reviewed",
            }))
            plan = agent.plan()
            assert plan == ["deploy_bot"]

            os.unlink(dbpath)

    def test_digital_products_pipeline_has_review(self):
        """digital_products plan() should include review step after create."""
        from monai.strategies.digital_products import DigitalProductsAgent
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Config()
            cfg.data_dir = Path(tmpdir)
            fd, dbpath = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            db = Database(Path(dbpath))
            llm = MagicMock()

            agent = DigitalProductsAgent(cfg, db, llm)
            products_dir = Path(tmpdir) / "products"
            products_dir.mkdir(parents=True, exist_ok=True)
            (products_dir / "test.json").write_text(json.dumps({
                "spec": {"title": "Test"},
                "status": "created",
            }))

            plan = agent.plan()
            assert plan == ["review_product"]

            (products_dir / "test.json").write_text(json.dumps({
                "spec": {"title": "Test"},
                "status": "reviewed",
            }))
            plan = agent.plan()
            assert plan == ["list_product"]

            os.unlink(dbpath)

    def test_micro_saas_pipeline_has_review(self):
        """micro_saas plan() should include review step after build."""
        from monai.strategies.micro_saas import MicroSaaSAgent
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Config()
            cfg.data_dir = Path(tmpdir)
            fd, dbpath = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            db = Database(Path(dbpath))
            llm = MagicMock()

            agent = MicroSaaSAgent(cfg, db, llm)
            products_dir = Path(tmpdir) / "micro_saas"
            products_dir.mkdir(parents=True, exist_ok=True)
            (products_dir / "test.json").write_text(json.dumps({
                "design": {"name": "Test SaaS"},
                "status": "built",
            }))

            plan = agent.plan()
            assert plan == ["review_product"]

            (products_dir / "test.json").write_text(json.dumps({
                "design": {"name": "Test SaaS"},
                "status": "reviewed",
            }))
            plan = agent.plan()
            assert plan == ["deploy"]

            os.unlink(dbpath)
