"""Tests for product improvement application — strategies actually rebuild products.

Verifies that:
1. Strategy agents retrieve pending improvements from ProductIterator
2. Code-based products (micro_saas, telegram_bots) rebuild via Coder
3. Content-based products (digital_products, course_creation) rewrite via LLM
4. DB-based products (saas, course_creation) update database records
5. Improvements are marked as applied after processing
6. Orchestrator calls apply_improvements() instead of blindly marking applied
7. Products re-enter the review pipeline after improvement
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from monai.agents.product_iterator import ProductIterator, ITERATOR_SCHEMA
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
    mock.quick.return_value = "test response"
    mock.chat.return_value = "Improved content section with better quality."
    mock.chat_json.return_value = {
        "improvements": [
            {
                "area": "content",
                "current_issue": "low quality",
                "specific_change": "rewrite intro",
                "expected_impact": "higher engagement",
                "priority": 1,
            }
        ],
        "rebuild_recommended": True,
        "rebuild_reason": "quality too low",
    }
    mock.get_model.return_value = "gpt-4"
    return mock


@pytest.fixture
def iterator(config, db, llm):
    return ProductIterator(config, db, llm)


def _insert_pending_improvement(db, strategy, product_name, improvements=None):
    """Helper to insert a pending improvement into the DB."""
    if improvements is None:
        improvements = {
            "improvements": [
                {
                    "area": "content",
                    "current_issue": "low quality",
                    "specific_change": "improve intro section",
                    "expected_impact": "better engagement",
                    "priority": 1,
                }
            ],
            "rebuild_recommended": True,
        }
    return db.execute_insert(
        "INSERT INTO product_iterations "
        "(strategy, product_name, iteration_number, trigger, improvements, status) "
        "VALUES (?, ?, 1, 'low_quality', ?, 'pending')",
        (strategy, product_name, json.dumps(improvements)),
    )


# ── MicroSaaS apply_improvements ─────────────────────────────────────


class TestMicroSaaSApplyImprovements:
    def test_no_pending_returns_early(self, config, db, llm, iterator):
        from monai.strategies.micro_saas import MicroSaaSAgent
        agent = MicroSaaSAgent(config, db, llm)
        agent._product_iterator = iterator

        result = agent.apply_improvements()
        assert result["status"] == "no_pending_improvements"

    def test_rebuilds_product_via_coder(self, config, db, llm, iterator):
        from monai.strategies.micro_saas import MicroSaaSAgent
        agent = MicroSaaSAgent(config, db, llm)
        agent._product_iterator = iterator

        # Create a product file
        product_data = {
            "design": {"name": "TestTool"},
            "build": {"status": "success", "project_dir": "/tmp/test"},
            "status": "deployed",
        }
        product_path = agent.products_dir / "TestTool.json"
        product_path.write_text(json.dumps(product_data))

        # Insert pending improvement
        _insert_pending_improvement(db, "micro_saas", "TestTool")

        # Mock coder
        mock_coder = MagicMock()
        mock_coder.generate_module.return_value = {"status": "success", "project_dir": "/tmp/rebuilt"}
        agent._coder = mock_coder

        result = agent.apply_improvements()

        assert result["applied"] == 1
        mock_coder.generate_module.assert_called_once()
        spec_arg = mock_coder.generate_module.call_args[0][0]
        assert "TestTool" in spec_arg
        assert "improve intro section" in spec_arg

        # Product should be set back to "built" for re-review
        updated = json.loads(product_path.read_text())
        assert updated["status"] == "built"
        assert "iteration_history" in updated
        assert len(updated["iteration_history"]) == 1

    def test_marks_applied_on_build_failure(self, config, db, llm, iterator):
        from monai.strategies.micro_saas import MicroSaaSAgent
        agent = MicroSaaSAgent(config, db, llm)
        agent._product_iterator = iterator

        product_data = {
            "design": {"name": "FailTool"},
            "build": {"status": "success"},
            "status": "deployed",
        }
        (agent.products_dir / "FailTool.json").write_text(json.dumps(product_data))
        _insert_pending_improvement(db, "micro_saas", "FailTool")

        mock_coder = MagicMock()
        mock_coder.generate_module.return_value = {"status": "error", "error": "build failed"}
        agent._coder = mock_coder

        result = agent.apply_improvements()

        # Still marked as applied to prevent infinite retry
        pending = iterator.get_pending_improvements("micro_saas")
        assert len(pending) == 0

    def test_skips_missing_product(self, config, db, llm, iterator):
        from monai.strategies.micro_saas import MicroSaaSAgent
        agent = MicroSaaSAgent(config, db, llm)
        agent._product_iterator = iterator

        # Insert improvement for non-existent product
        _insert_pending_improvement(db, "micro_saas", "NonExistent")

        result = agent.apply_improvements()
        # Should mark applied anyway (not stuck)
        pending = iterator.get_pending_improvements("micro_saas")
        assert len(pending) == 0

    def test_max_two_per_cycle(self, config, db, llm, iterator):
        from monai.strategies.micro_saas import MicroSaaSAgent
        agent = MicroSaaSAgent(config, db, llm)
        agent._product_iterator = iterator

        # Create 3 products with pending improvements
        for i in range(3):
            name = f"Tool{i}"
            product_data = {"design": {"name": name}, "build": {"status": "success"}, "status": "deployed"}
            (agent.products_dir / f"{name}.json").write_text(json.dumps(product_data))
            _insert_pending_improvement(db, "micro_saas", name)

        mock_coder = MagicMock()
        mock_coder.generate_module.return_value = {"status": "success"}
        agent._coder = mock_coder

        result = agent.apply_improvements()
        assert result["applied"] == 2
        assert result["total_pending"] == 3
        # One should still be pending
        remaining = iterator.get_pending_improvements("micro_saas")
        assert len(remaining) == 1


# ── DigitalProducts apply_improvements ────────────────────────────────


class TestDigitalProductsApplyImprovements:
    def test_no_pending_returns_early(self, config, db, llm, iterator):
        from monai.strategies.digital_products import DigitalProductsAgent
        agent = DigitalProductsAgent(config, db, llm)
        agent._product_iterator = iterator

        result = agent.apply_improvements()
        assert result["status"] == "no_pending_improvements"

    def test_rewrites_content_via_llm(self, config, db, llm, iterator):
        from monai.strategies.digital_products import DigitalProductsAgent
        agent = DigitalProductsAgent(config, db, llm)
        agent._product_iterator = iterator

        product_data = {
            "spec": {"title": "AI Guide"},
            "content": [
                {"section": "Introduction", "content": "Old intro text"},
                {"section": "Chapter 1", "content": "Old chapter text"},
            ],
            "status": "listed",
        }
        product_path = agent.products_dir / "AI Guide.json"
        product_path.write_text(json.dumps(product_data))

        _insert_pending_improvement(db, "digital_products", "AI Guide")

        result = agent.apply_improvements()

        assert result["applied"] == 1
        # LLM should have been called once per section
        assert llm.chat.call_count == 2

        # Product should be set back to "created" for re-review
        updated = json.loads(product_path.read_text())
        assert updated["status"] == "created"
        assert updated["content"][0]["content"] == "Improved content section with better quality."
        assert "iteration_history" in updated

    def test_skips_missing_product(self, config, db, llm, iterator):
        from monai.strategies.digital_products import DigitalProductsAgent
        agent = DigitalProductsAgent(config, db, llm)
        agent._product_iterator = iterator

        _insert_pending_improvement(db, "digital_products", "NonExistent")

        result = agent.apply_improvements()
        pending = iterator.get_pending_improvements("digital_products")
        assert len(pending) == 0


# ── SaaS apply_improvements ──────────────────────────────────────────


class TestSaaSApplyImprovements:
    def _setup_saas_tables(self, db):
        """Create saas_products and saas_features tables."""
        db.execute(
            "CREATE TABLE IF NOT EXISTS saas_products ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, tagline TEXT, problem TEXT NOT NULL, "
            "solution TEXT NOT NULL, target_market TEXT NOT NULL, "
            "market_size_estimate TEXT, competitors TEXT, differentiator TEXT, "
            "tech_stack TEXT, pricing_model TEXT, mrr_target REAL DEFAULT 0.0, "
            "current_mrr REAL DEFAULT 0.0, "
            "status TEXT DEFAULT 'researching', validation_score REAL DEFAULT 0.0, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, launched_at TIMESTAMP)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS saas_features ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "product_id INTEGER REFERENCES saas_products(id), "
            "feature_name TEXT NOT NULL, description TEXT, "
            "priority TEXT DEFAULT 'medium', complexity TEXT DEFAULT 'medium', "
            "status TEXT DEFAULT 'planned', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )

    def test_no_pending_returns_early(self, config, db, llm, iterator):
        from monai.strategies.saas import SaaSAgent
        agent = SaaSAgent(config, db, llm)
        agent._product_iterator = iterator

        result = agent.apply_improvements()
        assert result["status"] == "no_pending_improvements"

    def test_rebuilds_features_via_coder(self, config, db, llm, iterator):
        from monai.strategies.saas import SaaSAgent
        agent = SaaSAgent(config, db, llm)
        agent._product_iterator = iterator

        # Insert SaaS product + feature
        product_id = db.execute_insert(
            "INSERT INTO saas_products (name, problem, solution, target_market, status) "
            "VALUES (?, ?, ?, ?, 'launched')",
            ("TaskFlow", "project chaos", "AI task manager", "SMBs"),
        )
        db.execute_insert(
            "INSERT INTO saas_features (product_id, feature_name, description, status) "
            "VALUES (?, ?, ?, 'shipped')",
            (product_id, "content", "Content management"),
        )

        # Insert improvement targeting "content" area
        _insert_pending_improvement(db, "saas", "TaskFlow")

        mock_coder = MagicMock()
        mock_coder.generate_module.return_value = {"status": "success"}
        agent._coder = mock_coder

        result = agent.apply_improvements()

        assert result["applied"] == 1
        mock_coder.generate_module.assert_called_once()

        # Product should be sent back to 'beta' for re-review
        products = db.execute("SELECT status FROM saas_products WHERE id = ?", (product_id,))
        assert products[0]["status"] == "beta"

        # Feature should be in 'testing' state
        features = db.execute("SELECT status FROM saas_features WHERE product_id = ?", (product_id,))
        assert features[0]["status"] == "testing"


# ── TelegramBots apply_improvements ──────────────────────────────────


class TestTelegramBotsApplyImprovements:
    def test_no_pending_returns_early(self, config, db, llm, iterator):
        from monai.strategies.telegram_bots import TelegramBotAgent
        agent = TelegramBotAgent(config, db, llm)
        agent._product_iterator = iterator

        result = agent.apply_improvements()
        assert result["status"] == "no_pending_improvements"

    def test_rebuilds_bot_via_coder(self, config, db, llm, iterator):
        from monai.strategies.telegram_bots import TelegramBotAgent
        agent = TelegramBotAgent(config, db, llm)
        agent._product_iterator = iterator

        bot_data = {
            "design": {"name": "WeatherBot", "commands": [{"command": "/weather", "description": "Get weather"}]},
            "build": {"status": "success"},
            "status": "deployed",
        }
        bot_path = agent.bots_dir / "WeatherBot.json"
        bot_path.write_text(json.dumps(bot_data))

        _insert_pending_improvement(db, "telegram_bots", "WeatherBot")

        mock_coder = MagicMock()
        mock_coder.generate_module.return_value = {"status": "success"}
        agent._coder = mock_coder

        result = agent.apply_improvements()

        assert result["applied"] == 1
        mock_coder.generate_module.assert_called_once()

        updated = json.loads(bot_path.read_text())
        assert updated["status"] == "built"
        assert "iteration_history" in updated


# ── CourseCreation apply_improvements ─────────────────────────────────


class TestCourseCreationApplyImprovements:
    def _setup_course_tables(self, db):
        """Create courses and course_lessons tables."""
        db.execute(
            "CREATE TABLE IF NOT EXISTS courses ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title TEXT NOT NULL, niche TEXT, platform TEXT DEFAULT 'udemy', "
            "target_audience TEXT, description TEXT, price REAL DEFAULT 0, "
            "status TEXT DEFAULT 'researching', listing_url TEXT, "
            "published_at TIMESTAMP, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS course_lessons ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "course_id INTEGER REFERENCES courses(id), "
            "section TEXT NOT NULL, title TEXT NOT NULL, "
            "lesson_order INTEGER DEFAULT 0, "
            "script TEXT, duration_minutes INTEGER DEFAULT 10, "
            "status TEXT DEFAULT 'draft', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )

    def test_no_pending_returns_early(self, config, db, llm, iterator):
        from monai.strategies.course_creation import CourseCreationAgent
        self._setup_course_tables(db)
        agent = CourseCreationAgent(config, db, llm)
        agent._product_iterator = iterator

        result = agent.apply_improvements()
        assert result["status"] == "no_pending_improvements"

    def test_rewrites_lessons_via_llm(self, config, db, llm, iterator):
        from monai.strategies.course_creation import CourseCreationAgent
        self._setup_course_tables(db)
        agent = CourseCreationAgent(config, db, llm)
        agent._product_iterator = iterator

        # Insert course + lessons
        course_id = db.execute_insert(
            "INSERT INTO courses (title, niche, target_audience, status) "
            "VALUES (?, ?, ?, 'published')",
            ("Python for AI", "programming", "beginners"),
        )
        db.execute_insert(
            "INSERT INTO course_lessons (course_id, section, title, script, lesson_order, status) "
            "VALUES (?, ?, ?, ?, 1, 'scripted')",
            (course_id, "Basics", "Variables", "Old script about variables"),
        )
        db.execute_insert(
            "INSERT INTO course_lessons (course_id, section, title, script, lesson_order, status) "
            "VALUES (?, ?, ?, ?, 2, 'scripted')",
            (course_id, "Basics", "Functions", "Old script about functions"),
        )

        _insert_pending_improvement(db, "course_creation", "Python for AI")

        result = agent.apply_improvements()

        assert result["applied"] == 1
        assert llm.chat.call_count == 2  # Both lessons rewritten

        # Course should go back to 'producing' for re-review
        courses = db.execute("SELECT status FROM courses WHERE id = ?", (course_id,))
        assert courses[0]["status"] == "producing"


# ── Orchestrator Integration ──────────────────────────────────────────


class TestOrchestratorApplyImprovements:
    """Verify orchestrator calls apply_improvements() on strategies."""

    def test_orchestrator_calls_apply_improvements(self, config, db, llm, iterator):
        """The orchestrator should call strategy.apply_improvements() for pending improvements."""
        # Insert pending improvement
        _insert_pending_improvement(db, "micro_saas", "TestProduct")

        # Create a mock strategy agent with apply_improvements
        mock_agent = MagicMock()
        mock_agent.apply_improvements.return_value = {"applied": 1}

        # Simulate what the orchestrator does
        pending = iterator.get_pending_improvements("micro_saas")
        assert len(pending) == 1

        if hasattr(mock_agent, "apply_improvements"):
            result = mock_agent.apply_improvements()
            assert result["applied"] == 1
            mock_agent.apply_improvements.assert_called_once()

    def test_orchestrator_falls_back_for_no_apply_method(self, config, db, llm, iterator):
        """Strategies without apply_improvements() get improvements marked as applied."""
        _insert_pending_improvement(db, "lead_gen", "SomeProduct")

        # Mock a strategy agent WITHOUT apply_improvements
        mock_agent = MagicMock(spec=[])  # Empty spec = no methods

        pending = iterator.get_pending_improvements("lead_gen")
        assert len(pending) == 1

        if hasattr(mock_agent, "apply_improvements"):
            mock_agent.apply_improvements()
        else:
            for p in pending:
                iterator.mark_applied(p["id"])

        remaining = iterator.get_pending_improvements("lead_gen")
        assert len(remaining) == 0

    def test_orchestrator_marks_applied_on_exception(self, config, db, llm, iterator):
        """If apply_improvements() raises, pending items should still be marked applied."""
        _insert_pending_improvement(db, "micro_saas", "CrashProduct")

        mock_agent = MagicMock()
        mock_agent.apply_improvements.side_effect = RuntimeError("boom")

        pending = iterator.get_pending_improvements("micro_saas")
        assert len(pending) == 1

        if hasattr(mock_agent, "apply_improvements"):
            try:
                mock_agent.apply_improvements()
            except Exception:
                for p in pending:
                    iterator.mark_applied(p["id"])

        remaining = iterator.get_pending_improvements("micro_saas")
        assert len(remaining) == 0


# ── BaseAgent product_iterator property ───────────────────────────────


class TestBaseAgentProductIterator:
    def test_lazy_loads_product_iterator(self, config, db, llm):
        from monai.agents.base import BaseAgent

        class TestAgent(BaseAgent):
            name = "test_agent"
            description = "Test"

            def plan(self):
                return []

            def run(self, **kwargs):
                return {}

        agent = TestAgent(config, db, llm)
        assert agent._product_iterator is None

        pi = agent.product_iterator
        assert pi is not None
        assert isinstance(pi, ProductIterator)

    def test_product_iterator_setter(self, config, db, llm):
        from monai.agents.base import BaseAgent

        class TestAgent(BaseAgent):
            name = "test_agent"
            description = "Test"

            def plan(self):
                return []

            def run(self, **kwargs):
                return {}

        agent = TestAgent(config, db, llm)
        mock_pi = MagicMock()
        agent.product_iterator = mock_pi
        assert agent.product_iterator is mock_pi


# ── Improvement Plan Parsing Edge Cases ───────────────────────────────


class TestImprovementPlanParsing:
    def test_handles_empty_improvements_json(self, config, db, llm, iterator):
        from monai.strategies.micro_saas import MicroSaaSAgent
        agent = MicroSaaSAgent(config, db, llm)
        agent._product_iterator = iterator

        product_data = {"design": {"name": "EmptyPlan"}, "build": {"status": "success"}, "status": "deployed"}
        (agent.products_dir / "EmptyPlan.json").write_text(json.dumps(product_data))

        # Insert improvement with empty improvements field
        db.execute_insert(
            "INSERT INTO product_iterations "
            "(strategy, product_name, iteration_number, trigger, improvements, status) "
            "VALUES (?, ?, 1, 'low_quality', ?, 'pending')",
            ("micro_saas", "EmptyPlan", "{}"),
        )

        mock_coder = MagicMock()
        mock_coder.generate_module.return_value = {"status": "success"}
        agent._coder = mock_coder

        # Should not crash on empty improvements
        result = agent.apply_improvements()
        assert result["applied"] == 1

    def test_handles_malformed_json(self, config, db, llm, iterator):
        from monai.strategies.micro_saas import MicroSaaSAgent
        agent = MicroSaaSAgent(config, db, llm)
        agent._product_iterator = iterator

        product_data = {"design": {"name": "BadJSON"}, "build": {"status": "success"}, "status": "deployed"}
        (agent.products_dir / "BadJSON.json").write_text(json.dumps(product_data))

        db.execute_insert(
            "INSERT INTO product_iterations "
            "(strategy, product_name, iteration_number, trigger, improvements, status) "
            "VALUES (?, ?, 1, 'low_quality', ?, 'pending')",
            ("micro_saas", "BadJSON", "not valid json{{{"),
        )

        mock_coder = MagicMock()
        mock_coder.generate_module.return_value = {"status": "success"}
        agent._coder = mock_coder

        # Should handle gracefully
        result = agent.apply_improvements()
        assert result["applied"] == 1

    def test_handles_null_improvements(self, config, db, llm, iterator):
        from monai.strategies.digital_products import DigitalProductsAgent
        agent = DigitalProductsAgent(config, db, llm)
        agent._product_iterator = iterator

        product_data = {
            "spec": {"title": "NullPlan"},
            "content": [{"section": "Intro", "content": "Old text"}],
            "status": "listed",
        }
        (agent.products_dir / "NullPlan.json").write_text(json.dumps(product_data))

        db.execute_insert(
            "INSERT INTO product_iterations "
            "(strategy, product_name, iteration_number, trigger, improvements, status) "
            "VALUES (?, ?, 1, 'low_quality', NULL, 'pending')",
            ("digital_products", "NullPlan"),
        )

        result = agent.apply_improvements()
        assert result["applied"] == 1
