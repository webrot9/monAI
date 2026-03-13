"""Tests for ProductIterator — continuous product improvement engine."""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        "rebuild_recommended": False,
        "rebuild_reason": "",
    }
    mock.get_model.return_value = "gpt-4"
    return mock


@pytest.fixture
def iterator(config, db, llm):
    return ProductIterator(config, db, llm)


class TestProductIteratorInit:
    def test_schema_created(self, iterator, db):
        """Tables are created on init."""
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='product_iterations'"
        )
        assert len(rows) == 1

        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='product_performance'"
        )
        assert len(rows) == 1

    def test_name_and_description(self, iterator):
        assert iterator.name == "product_iterator"
        assert "improvement" in iterator.description.lower()


class TestRecordMetric:
    def test_records_metric(self, iterator, db):
        iterator._record_metric("digital_products", "ebook_1", "sales_count", 10.0)
        rows = db.execute("SELECT * FROM product_performance")
        assert len(rows) == 1
        assert rows[0]["strategy"] == "digital_products"
        assert rows[0]["product_name"] == "ebook_1"
        assert rows[0]["metric_name"] == "sales_count"
        assert rows[0]["metric_value"] == 10.0

    def test_multiple_metrics(self, iterator, db):
        iterator._record_metric("saas", "tool_1", "revenue", 100.0)
        iterator._record_metric("saas", "tool_1", "refund_count", 2.0)
        iterator._record_metric("saas", "tool_2", "revenue", 50.0)
        rows = db.execute("SELECT * FROM product_performance")
        assert len(rows) == 3


class TestIdentifyUnderperformers:
    def test_empty_returns_empty(self, iterator):
        result = iterator._identify_underperformers([])
        assert isinstance(result, list)

    def test_low_score_products_identified(self, iterator, db):
        # Create product_reviews table and add low-score reviews
        db.execute(
            "CREATE TABLE IF NOT EXISTS product_reviews ("
            "id INTEGER PRIMARY KEY, strategy TEXT, product_name TEXT, "
            "quality_score REAL, verdict TEXT, humanizer_score REAL, "
            "factcheck_accuracy REAL, usability_score REAL, "
            "issues TEXT, suggestions TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute_insert(
            "INSERT INTO product_reviews (strategy, product_name, quality_score, verdict) "
            "VALUES (?, ?, ?, ?)",
            ("digital_products", "bad_ebook", 0.3, "needs_revision"),
        )
        result = iterator._identify_underperformers([])
        # Should find the low-score product
        assert any(p["product_name"] == "bad_ebook" for p in result)


class TestIterateProduct:
    def test_creates_iteration_record(self, iterator, db):
        product = {
            "strategy": "digital_products",
            "product_name": "test_product",
            "avg_score": 0.4,
            "trigger": "low_quality",
        }

        # Mock the web search
        with patch.object(iterator, "search_web", return_value={"competitors": []}):
            result = iterator._iterate_product(product)

        assert result["strategy"] == "digital_products"
        assert result["product_name"] == "test_product"
        assert result["iteration"] == 1

        # Check DB
        rows = db.execute("SELECT * FROM product_iterations")
        assert len(rows) == 1
        assert rows[0]["strategy"] == "digital_products"
        assert rows[0]["status"] == "pending"

    def test_increments_iteration_number(self, iterator, db):
        # Insert a previous iteration
        db.execute_insert(
            "INSERT INTO product_iterations (strategy, product_name, iteration_number, "
            "trigger, status) VALUES (?, ?, ?, ?, ?)",
            ("saas", "tool_1", 1, "low_sales", "applied"),
        )

        product = {
            "strategy": "saas",
            "product_name": "tool_1",
            "avg_score": 0.5,
            "trigger": "low_quality",
        }

        with patch.object(iterator, "search_web", return_value={"competitors": []}):
            result = iterator._iterate_product(product)

        assert result["iteration"] == 2


class TestGetPendingImprovements:
    def test_returns_pending_for_strategy(self, iterator, db):
        db.execute_insert(
            "INSERT INTO product_iterations (strategy, product_name, iteration_number, "
            "trigger, improvements, status) VALUES (?, ?, ?, ?, ?, ?)",
            ("saas", "tool_1", 1, "low_sales", '{"improvements": []}', "pending"),
        )
        db.execute_insert(
            "INSERT INTO product_iterations (strategy, product_name, iteration_number, "
            "trigger, improvements, status) VALUES (?, ?, ?, ?, ?, ?)",
            ("saas", "tool_2", 1, "competitor_gap", '{"improvements": []}', "applied"),
        )

        pending = iterator.get_pending_improvements("saas")
        assert len(pending) == 1
        assert pending[0]["product_name"] == "tool_1"

    def test_returns_empty_for_other_strategy(self, iterator, db):
        db.execute_insert(
            "INSERT INTO product_iterations (strategy, product_name, iteration_number, "
            "trigger, status) VALUES (?, ?, ?, ?, ?)",
            ("saas", "tool_1", 1, "low_sales", "pending"),
        )
        pending = iterator.get_pending_improvements("affiliate")
        assert len(pending) == 0


class TestMarkApplied:
    def test_marks_applied_with_timestamp(self, iterator, db):
        row_id = db.execute_insert(
            "INSERT INTO product_iterations (strategy, product_name, iteration_number, "
            "trigger, status) VALUES (?, ?, ?, ?, ?)",
            ("saas", "tool_1", 1, "low_sales", "pending"),
        )
        iterator.mark_applied(row_id)

        rows = db.execute("SELECT * FROM product_iterations WHERE id = ?", (row_id,))
        assert rows[0]["status"] == "applied"
        assert rows[0]["applied_at"] is not None


class TestIterationSummary:
    def test_empty_summary(self, iterator):
        summary = iterator.get_iteration_summary()
        assert summary == {}

    def test_grouped_summary(self, iterator, db):
        db.execute_insert(
            "INSERT INTO product_iterations (strategy, product_name, iteration_number, "
            "trigger, status) VALUES (?, ?, ?, ?, ?)",
            ("saas", "t1", 1, "low_sales", "pending"),
        )
        db.execute_insert(
            "INSERT INTO product_iterations (strategy, product_name, iteration_number, "
            "trigger, status) VALUES (?, ?, ?, ?, ?)",
            ("saas", "t2", 1, "low_sales", "applied"),
        )
        db.execute_insert(
            "INSERT INTO product_iterations (strategy, product_name, iteration_number, "
            "trigger, status) VALUES (?, ?, ?, ?, ?)",
            ("affiliate", "a1", 1, "competitor_gap", "pending"),
        )

        summary = iterator.get_iteration_summary()
        assert summary["saas"]["pending"] == 1
        assert summary["saas"]["applied"] == 1
        assert summary["affiliate"]["pending"] == 1


class TestAnalyzeCompetitors:
    def test_uses_strategy_specific_search(self, iterator):
        with patch.object(iterator, "search_web", return_value={"competitors": []}) as mock:
            iterator._analyze_competitors("saas", "MyTool")
            call_args = mock.call_args[0][0]
            assert "MyTool" in call_args
            assert "competitors" in call_args.lower() or "alternatives" in call_args.lower()

    def test_fallback_for_unknown_strategy(self, iterator):
        with patch.object(iterator, "search_web", return_value={"competitors": []}) as mock:
            iterator._analyze_competitors("custom_strategy", "CustomProduct")
            call_args = mock.call_args[0][0]
            assert "CustomProduct" in call_args


class TestRunCycle:
    def test_run_returns_expected_keys(self, iterator):
        with patch.object(iterator, "_collect_performance_metrics", return_value=[]):
            with patch.object(iterator, "_identify_underperformers", return_value=[]):
                with patch.object(iterator, "_evaluate_past_iterations", return_value=[]):
                    result = iterator.run()

        assert "metrics_collected" in result
        assert "underperformers" in result
        assert "iterations" in result
        assert "evaluations" in result

    def test_limits_iterations_per_cycle(self, iterator):
        """Should process max 3 underperformers per cycle."""
        underperformers = [
            {"strategy": f"s{i}", "product_name": f"p{i}", "avg_score": 0.3, "trigger": "low"}
            for i in range(5)
        ]

        with patch.object(iterator, "_collect_performance_metrics", return_value=[]):
            with patch.object(iterator, "_identify_underperformers", return_value=underperformers):
                with patch.object(iterator, "_iterate_product", return_value={"iteration": 1}) as mock_iterate:
                    with patch.object(iterator, "_evaluate_past_iterations", return_value=[]):
                        result = iterator.run()

        # Should only call _iterate_product 3 times (max per cycle)
        assert mock_iterate.call_count == 3
        assert len(result["iterations"]) == 3


class TestCompetitorTracking:
    """Tests for persistent competitor tracking with change history."""

    def test_competitors_table_created(self, iterator, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='competitors'"
        )
        assert len(rows) == 1

    def test_competitor_history_table_created(self, iterator, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='competitor_history'"
        )
        assert len(rows) == 1

    def test_persist_new_competitors(self, iterator, db):
        competitor_data = {
            "competitors": [
                {"name": "Rival1", "features": ["a", "b"], "pricing": "$10/mo",
                 "rating": "4.5", "differentiator": "fast"},
                {"name": "Rival2", "features": ["c"], "pricing": "$20/mo",
                 "rating": "4.0", "differentiator": "cheap"},
            ]
        }
        iterator._persist_competitors("saas", "MyTool", competitor_data)

        rows = db.execute("SELECT * FROM competitors ORDER BY competitor_name")
        assert len(rows) == 2
        assert rows[0]["competitor_name"] == "Rival1"
        assert rows[0]["pricing"] == "$10/mo"
        assert rows[1]["competitor_name"] == "Rival2"

    def test_update_existing_competitor_tracks_changes(self, iterator, db):
        # Insert initial
        data1 = {"competitors": [
            {"name": "Rival1", "features": ["a"], "pricing": "$10/mo",
             "rating": "4.5", "differentiator": "fast"},
        ]}
        iterator._persist_competitors("saas", "MyTool", data1)

        # Update with new pricing
        data2 = {"competitors": [
            {"name": "Rival1", "features": ["a", "b"], "pricing": "$15/mo",
             "rating": "4.5", "differentiator": "fast"},
        ]}
        iterator._persist_competitors("saas", "MyTool", data2)

        # Should still have 1 competitor
        comps = db.execute("SELECT * FROM competitors")
        assert len(comps) == 1
        assert comps[0]["pricing"] == "$15/mo"

        # Should have change history
        history = db.execute("SELECT * FROM competitor_history")
        assert len(history) >= 1
        pricing_changes = [h for h in history if h["field_name"] == "pricing"]
        assert len(pricing_changes) == 1
        assert pricing_changes[0]["old_value"] == "$10/mo"
        assert pricing_changes[0]["new_value"] == "$15/mo"

    def test_get_competitor_trends(self, iterator, db):
        data = {"competitors": [
            {"name": "Rival1", "features": ["a"], "pricing": "$10",
             "rating": "4.0", "differentiator": "speed"},
        ]}
        iterator._persist_competitors("saas", "MyTool", data)

        # Update
        data2 = {"competitors": [
            {"name": "Rival1", "features": ["a", "b"], "pricing": "$12",
             "rating": "4.2", "differentiator": "speed"},
        ]}
        iterator._persist_competitors("saas", "MyTool", data2)

        trends = iterator.get_competitor_trends("saas", "MyTool")
        assert len(trends) == 1
        assert trends[0]["competitor_name"] == "Rival1"
        assert len(trends[0]["recent_changes"]) >= 1

    def test_handles_invalid_competitor_data(self, iterator, db):
        # Should not crash on bad data
        iterator._persist_competitors("saas", "MyTool", {})
        iterator._persist_competitors("saas", "MyTool", {"competitors": "not_a_list"})
        iterator._persist_competitors("saas", "MyTool", {"competitors": [{"no_name": True}]})
        rows = db.execute("SELECT * FROM competitors")
        assert len(rows) == 0

    def test_analyze_competitors_persists(self, iterator, db):
        """_analyze_competitors should persist results to DB."""
        comp_data = {"competitors": [
            {"name": "CompA", "features": ["x"], "pricing": "$5",
             "rating": "3.8", "differentiator": "simple"},
        ]}
        with patch.object(iterator, "search_web", return_value=comp_data):
            result = iterator._analyze_competitors("saas", "MyTool")

        assert result == comp_data
        rows = db.execute("SELECT * FROM competitors")
        assert len(rows) == 1
        assert rows[0]["competitor_name"] == "CompA"


class TestCustomerFeedbackInIteration:
    """Tests for customer feedback integration in product iteration."""

    def _setup_reviews_table(self, db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS product_reviews ("
            "id INTEGER PRIMARY KEY, strategy TEXT, product_name TEXT, "
            "quality_score REAL, verdict TEXT, humanizer_score REAL, "
            "factcheck_accuracy REAL, usability_score REAL, "
            "issues TEXT, suggestions TEXT, "
            "customer_rating REAL, customer_feedback TEXT, "
            "nps_score INTEGER, support_tickets INTEGER DEFAULT 0, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )

    def test_low_customer_rating_triggers_underperformer(self, iterator, db):
        self._setup_reviews_table(db)
        # Insert reviews with low customer rating but OK quality
        for _ in range(5):
            db.execute_insert(
                "INSERT INTO product_reviews "
                "(strategy, product_name, quality_score, verdict, customer_rating) "
                "VALUES (?, ?, ?, ?, ?)",
                ("saas", "tool_1", 0.8, "approved", 2.5),
            )

        result = iterator._identify_underperformers([])
        low_rated = [p for p in result if p.get("trigger") == "low_customer_rating"]
        assert len(low_rated) >= 1
        assert low_rated[0]["product_name"] == "tool_1"

    def test_customer_feedback_in_iterate_product(self, iterator, db):
        """Customer feedback should be included in improvement plan prompt."""
        self._setup_reviews_table(db)
        db.execute_insert(
            "INSERT INTO product_reviews "
            "(strategy, product_name, quality_score, verdict, "
            "customer_rating, customer_feedback, issues, suggestions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("saas", "tool_1", 0.5, "needs_revision", 2.0,
             "UI is confusing and slow", "[]", "[]"),
        )

        product = {
            "strategy": "saas",
            "product_name": "tool_1",
            "avg_score": 0.5,
            "trigger": "low_customer_rating",
        }
        with patch.object(iterator, "search_web", return_value={"competitors": []}):
            result = iterator._iterate_product(product)

        assert result["strategy"] == "saas"
        # Verify iteration was created
        rows = db.execute("SELECT * FROM product_iterations")
        assert len(rows) == 1


class TestImprovedEvaluation:
    """Tests for the multi-metric iteration evaluation."""

    def _setup_tables(self, db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS product_reviews ("
            "id INTEGER PRIMARY KEY, strategy TEXT, product_name TEXT, "
            "quality_score REAL, verdict TEXT, humanizer_score REAL, "
            "factcheck_accuracy REAL, usability_score REAL, "
            "issues TEXT, suggestions TEXT, "
            "customer_rating REAL, customer_feedback TEXT, "
            "nps_score INTEGER, support_tickets INTEGER DEFAULT 0, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS payments ("
            "id INTEGER PRIMARY KEY, strategy TEXT, amount REAL, "
            "status TEXT, refund_reason TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )

    def test_skips_eval_with_few_reviews(self, iterator, db):
        """Should not evaluate iteration without minimum reviews."""
        self._setup_tables(db)
        db.execute_insert(
            "INSERT INTO product_iterations "
            "(strategy, product_name, iteration_number, trigger, status, "
            "performance_before, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("saas", "tool_1", 1, "low_quality", "applied",
             '{"quality_score": 0.4}', "2020-01-01T00:00:00"),
        )
        # Only 1 review (below MIN_REVIEW_SAMPLES=3)
        db.execute_insert(
            "INSERT INTO product_reviews (strategy, product_name, quality_score, verdict) "
            "VALUES (?, ?, ?, ?)",
            ("saas", "tool_1", 0.8, "approved"),
        )

        results = iterator._evaluate_past_iterations()
        assert len(results) == 0  # Not enough data

    def test_evaluates_with_sufficient_reviews(self, iterator, db):
        """Should evaluate when enough reviews are available."""
        self._setup_tables(db)
        db.execute_insert(
            "INSERT INTO product_iterations "
            "(strategy, product_name, iteration_number, trigger, status, "
            "performance_before, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("saas", "tool_1", 1, "low_quality", "applied",
             '{"quality_score": 0.4}', "2020-01-01T00:00:00"),
        )
        # Add enough reviews
        for _ in range(5):
            db.execute_insert(
                "INSERT INTO product_reviews (strategy, product_name, quality_score, verdict) "
                "VALUES (?, ?, ?, ?)",
                ("saas", "tool_1", 0.85, "approved"),
            )

        results = iterator._evaluate_past_iterations()
        assert len(results) == 1
        assert results[0]["improved"] is True
        assert results[0]["sample_size"] >= 3

    def test_multi_metric_evaluation(self, iterator, db):
        """Should check quality + revenue + refund rate."""
        self._setup_tables(db)
        db.execute_insert(
            "INSERT INTO product_iterations "
            "(strategy, product_name, iteration_number, trigger, status, "
            "performance_before, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("saas", "tool_1", 1, "low_quality", "applied",
             '{"quality_score": 0.4}', "2020-01-01T00:00:00"),
        )
        # Good reviews
        for _ in range(4):
            db.execute_insert(
                "INSERT INTO product_reviews (strategy, product_name, quality_score, verdict) "
                "VALUES (?, ?, ?, ?)",
                ("saas", "tool_1", 0.9, "approved"),
            )
        # Revenue
        for _ in range(3):
            db.execute_insert(
                "INSERT INTO payments (strategy, amount, status) VALUES (?, ?, ?)",
                ("saas", 50.0, "completed"),
            )

        results = iterator._evaluate_past_iterations()
        assert len(results) == 1
        r = results[0]
        assert r["improved"] is True
        assert "signals" in r
        assert r["signals"]["improvement"] > 0
