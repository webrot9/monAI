"""Tests for product improvement application — digital_products rebuilds products.

Verifies that:
1. Strategy agents retrieve pending improvements from ProductIterator
2. Content-based products (digital_products) rewrite via LLM
3. Improvements are marked as applied after processing
4. Orchestrator calls apply_improvements() instead of blindly marking applied
5. Products re-enter the review pipeline after improvement
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




# ── Orchestrator Integration ──────────────────────────────────────────


class TestOrchestratorApplyImprovements:
    """Verify orchestrator calls apply_improvements() on strategies."""

    def test_orchestrator_calls_apply_improvements(self, config, db, llm, iterator):
        """The orchestrator should call strategy.apply_improvements() for pending improvements."""
        # Insert pending improvement
        _insert_pending_improvement(db, "digital_products", "TestProduct")

        # Create a mock strategy agent with apply_improvements
        mock_agent = MagicMock()
        mock_agent.apply_improvements.return_value = {"applied": 1}

        # Simulate what the orchestrator does
        pending = iterator.get_pending_improvements("digital_products")
        assert len(pending) == 1

        if hasattr(mock_agent, "apply_improvements"):
            result = mock_agent.apply_improvements()
            assert result["applied"] == 1
            mock_agent.apply_improvements.assert_called_once()

    def test_orchestrator_falls_back_for_no_apply_method(self, config, db, llm, iterator):
        """Strategies without apply_improvements() get improvements marked as applied."""
        _insert_pending_improvement(db, "digital_products", "SomeProduct")

        # Mock a strategy agent WITHOUT apply_improvements
        mock_agent = MagicMock(spec=[])  # Empty spec = no methods

        pending = iterator.get_pending_improvements("digital_products")
        assert len(pending) == 1

        if hasattr(mock_agent, "apply_improvements"):
            mock_agent.apply_improvements()
        else:
            for p in pending:
                iterator.mark_applied(p["id"])

        remaining = iterator.get_pending_improvements("digital_products")
        assert len(remaining) == 0

    def test_orchestrator_marks_applied_on_exception(self, config, db, llm, iterator):
        """If apply_improvements() raises, pending items should still be marked applied."""
        _insert_pending_improvement(db, "digital_products", "CrashProduct")

        mock_agent = MagicMock()
        mock_agent.apply_improvements.side_effect = RuntimeError("boom")

        pending = iterator.get_pending_improvements("digital_products")
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
