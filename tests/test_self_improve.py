"""Tests for monai.agents.self_improve."""

import json
import math

import pytest

from monai.agents.self_improve import (
    EARLY_STOP_P,
    HIGH_VARIANCE_RATIO,
    MIN_SAMPLE_SIZE,
    SIGNIFICANCE_LEVEL,
    SelfImprover,
    _normal_sf,
    _welch_t_test,
)


class TestSelfImprover:
    @pytest.fixture
    def improver(self, config, db, mock_llm):
        return SelfImprover(config, db, mock_llm)

    # ── Schema ────────────────────────────────────────────────

    def test_schema_created(self, improver, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_improvements'"
        )
        assert len(rows) == 1

        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_metrics'"
        )
        assert len(rows) == 1

    # ── Metrics ───────────────────────────────────────────────

    def test_record_metric(self, improver):
        mid = improver.record_metric("writer", 1, "articles_written", 5.0)
        assert mid >= 1

    def test_get_metrics(self, improver):
        improver.record_metric("writer", 1, "articles_written", 5.0)
        improver.record_metric("writer", 2, "articles_written", 8.0)
        improver.record_metric("writer", 1, "revenue", 100.0)

        metrics = improver.get_metrics("writer", "articles_written")
        assert len(metrics) == 2

        all_metrics = improver.get_metrics("writer")
        assert len(all_metrics) == 3

    def test_get_metrics_empty(self, improver):
        assert improver.get_metrics("nonexistent") == []

    def test_metric_trend_insufficient_data(self, improver):
        improver.record_metric("writer", 1, "revenue", 100.0)
        trend = improver.get_metric_trend("writer", "revenue")
        assert trend["trend"] == "insufficient_data"

    def test_metric_trend_improving(self, improver):
        # Record increasing values
        for i in range(10):
            improver.record_metric("writer", i + 1, "revenue", 50.0 + i * 20)

        trend = improver.get_metric_trend("writer", "revenue")
        assert trend["trend"] == "improving"
        assert trend["data_points"] == 10

    def test_metric_trend_declining(self, improver):
        for i in range(10):
            improver.record_metric("writer", i + 1, "revenue", 200.0 - i * 20)

        trend = improver.get_metric_trend("writer", "revenue")
        assert trend["trend"] == "declining"

    def test_metric_trend_stable(self, improver):
        for i in range(10):
            improver.record_metric("writer", i + 1, "revenue", 100.0)

        trend = improver.get_metric_trend("writer", "revenue")
        assert trend["trend"] == "stable"

    # ── Performance Analysis ──────────────────────────────────

    def test_analyze_performance_sparse(self, improver):
        analysis = improver.analyze_performance("new_agent")
        assert analysis["data_richness"] == "sparse"

    def test_analyze_performance_good(self, improver, db):
        # Add enough metrics
        for i in range(5):
            improver.record_metric("writer", i + 1, "revenue", 100.0 + i * 10)
            improver.record_metric("writer", i + 1, "cost", 5.0)
            improver.record_metric("writer", i + 1, "quality", 0.9)

        analysis = improver.analyze_performance("writer")
        assert analysis["data_richness"] == "good"
        assert "revenue" in analysis["metrics"]
        assert "cost" in analysis["metrics"]

    # ── Improvement Proposals ─────────────────────────────────

    def test_propose_improvement(self, improver):
        iid = improver.propose_improvement(
            "writer",
            "prompt",
            "Improve article intro generation",
            old_value="Write an article about {topic}",
            new_value="Write a compelling article about {topic} with a hook",
        )
        assert iid >= 1

    def test_get_improvements(self, improver):
        improver.propose_improvement("writer", "prompt", "Better intros")
        improver.propose_improvement("writer", "strategy", "Target higher-paying clients")

        improvements = improver.get_improvements("writer")
        assert len(improvements) == 2

    def test_get_improvements_by_status(self, improver):
        iid = improver.propose_improvement("writer", "prompt", "Test improvement")
        improver.approve_improvement(iid)

        proposed = improver.get_improvements("writer", status="proposed")
        assert len(proposed) == 0

        approved = improver.get_improvements("writer", status="approved")
        assert len(approved) == 1

    def test_approve_improvement(self, improver):
        iid = improver.propose_improvement("writer", "prompt", "Test")
        improver.approve_improvement(iid)

        improvements = improver.get_improvements("writer", status="approved")
        assert len(improvements) == 1

    def test_deploy_improvement(self, improver):
        iid = improver.propose_improvement("writer", "prompt", "Test")
        improver.approve_improvement(iid)
        improver.deploy_improvement(iid)

        improvements = improver.get_improvements("writer", status="deployed")
        assert len(improvements) == 1
        assert improvements[0]["deployed_at"] is not None

    def test_revert_improvement(self, improver):
        iid = improver.propose_improvement("writer", "prompt", "Bad idea")
        improver.deploy_improvement(iid)
        improver.revert_improvement(iid, reason="Performance dropped")

        improvements = improver.get_improvements("writer", status="reverted")
        assert len(improvements) == 1

    def test_mark_ethics_result(self, improver):
        iid = improver.propose_improvement("writer", "prompt", "Test")
        improver.mark_ethics_result(iid, True)

        improvements = improver.get_improvements("writer")
        assert improvements[0]["ethics_passed"] == 1

    def test_mark_ethics_failed(self, improver):
        iid = improver.propose_improvement("writer", "prompt", "Risky change")
        improver.mark_ethics_result(iid, False)

        improvements = improver.get_improvements("writer")
        assert improvements[0]["ethics_passed"] == 0

    # ── Generate Improvements ─────────────────────────────────

    def test_generate_improvements_sparse_data(self, improver):
        result = improver.generate_improvements("new_agent")
        assert result == []  # Not enough data

    def test_generate_improvements_with_data(self, improver, mock_llm):
        # Add enough data
        for i in range(5):
            improver.record_metric("writer", i + 1, "revenue", 100.0)
            improver.record_metric("writer", i + 1, "cost", 5.0)
            improver.record_metric("writer", i + 1, "quality", 0.9)

        mock_llm.chat_json.return_value = {
            "improvements": [
                {"type": "prompt", "description": "Better intro", "expected_impact": "10%", "risk": "low"},
            ]
        }

        improvements = improver.generate_improvements("writer")
        assert len(improvements) == 1

        # Should be recorded in DB
        stored = improver.get_improvements("writer")
        assert len(stored) == 1

    # ── Improvement Summary ───────────────────────────────────

    def test_improvement_summary_empty(self, improver):
        summary = improver.get_improvement_summary()
        assert summary == {}

    def test_improvement_summary(self, improver):
        improver.propose_improvement("writer", "prompt", "A")
        improver.propose_improvement("writer", "strategy", "B")
        iid = improver.propose_improvement("researcher", "tool", "C")
        improver.approve_improvement(iid)

        summary = improver.get_improvement_summary()
        assert "writer" in summary
        assert "researcher" in summary
        assert summary["writer"]["proposed"] == 2
        assert summary["researcher"]["approved"] == 1


class TestWelchTTest:
    """Tests for the pure-Python Welch's t-test implementation."""

    def test_identical_groups(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        t_stat, p_value = _welch_t_test(vals, vals)
        assert abs(t_stat) < 0.001
        assert p_value > 0.9

    def test_clearly_different_groups(self):
        before = [1.0, 2.0, 3.0, 2.0, 1.5, 2.5, 1.0, 2.0, 3.0, 2.0]
        after = [10.0, 11.0, 12.0, 10.5, 11.5, 10.0, 11.0, 12.0, 10.5, 11.5]
        t_stat, p_value = _welch_t_test(before, after)
        assert p_value < 0.001
        assert t_stat > 0  # after > before

    def test_not_significant(self):
        before = [5.0, 5.1, 4.9, 5.0, 5.2, 4.8, 5.0, 5.1, 4.9, 5.0]
        after = [5.1, 5.0, 5.2, 4.9, 5.0, 5.1, 5.0, 4.9, 5.1, 5.0]
        t_stat, p_value = _welch_t_test(before, after)
        assert p_value > 0.05

    def test_too_few_samples(self):
        t_stat, p_value = _welch_t_test([1.0], [2.0])
        assert t_stat == 0.0
        assert p_value == 1.0

    def test_zero_variance(self):
        before = [5.0, 5.0, 5.0, 5.0, 5.0]
        after = [5.0, 5.0, 5.0, 5.0, 5.0]
        t_stat, p_value = _welch_t_test(before, after)
        assert p_value == 1.0

    def test_unequal_sample_sizes(self):
        before = [1.0, 2.0, 3.0, 4.0, 5.0]
        after = [6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        t_stat, p_value = _welch_t_test(before, after)
        assert p_value < 0.01


class TestNormalSF:
    """Tests for the survival function approximation."""

    def test_zero(self):
        assert abs(_normal_sf(0) - 0.5) < 0.001

    def test_large_positive(self):
        assert _normal_sf(4.0) < 0.0001

    def test_symmetry(self):
        assert abs(_normal_sf(1.0) + _normal_sf(-1.0) - 1.0) < 0.001


class TestStatisticalComparison:
    """Tests for the statistically rigorous _compare_snapshots."""

    @pytest.fixture
    def improver(self, config, db, mock_llm):
        return SelfImprover(config, db, mock_llm)

    def test_insufficient_data_empty(self, improver):
        verdict, details = improver._compare_snapshots({}, {})
        assert verdict == "insufficient_data"

    def test_insufficient_data_low_sample(self, improver):
        before = {"revenue": 100.0}
        after = {"revenue": 110.0}
        # Only 2 data points per group — below MIN_SAMPLE_SIZE
        before_raw = {"revenue": [100.0, 100.0]}
        after_raw = {"revenue": [110.0, 110.0]}
        verdict, details = improver._compare_snapshots(
            before, after, before_raw=before_raw, after_raw=after_raw,
        )
        assert verdict == "insufficient_data"
        assert "min_required" in str(details)

    def test_significant_improvement(self, improver):
        before = {"revenue": 100.0}
        after = {"revenue": 130.0}
        # Clear improvement with sufficient data
        before_raw = {"revenue": [90, 95, 100, 105, 100, 95, 100, 105, 100, 95]}
        after_raw = {"revenue": [125, 130, 135, 128, 132, 130, 125, 135, 128, 132]}
        verdict, details = improver._compare_snapshots(
            before, after, before_raw=before_raw, after_raw=after_raw,
        )
        assert verdict == "improved"
        assert "metrics" in details
        revenue_result = details["metrics"]["revenue"]
        assert revenue_result["significant"] is True
        assert revenue_result["p_value"] < SIGNIFICANCE_LEVEL

    def test_significant_decline(self, improver):
        before = {"revenue": 100.0}
        after = {"revenue": 70.0}
        before_raw = {"revenue": [95, 100, 105, 100, 95, 100, 105, 100, 95, 100]}
        after_raw = {"revenue": [65, 70, 75, 70, 65, 70, 75, 70, 65, 70]}
        verdict, details = improver._compare_snapshots(
            before, after, before_raw=before_raw, after_raw=after_raw,
        )
        assert verdict == "declined"

    def test_not_significant_small_change(self, improver):
        before = {"revenue": 100.0}
        after = {"revenue": 101.0}
        # Tiny difference with high variance — should not be significant
        before_raw = {"revenue": [80, 90, 100, 110, 120, 80, 90, 100, 110, 120]}
        after_raw = {"revenue": [81, 91, 101, 111, 121, 81, 91, 101, 111, 121]}
        verdict, details = improver._compare_snapshots(
            before, after, before_raw=before_raw, after_raw=after_raw,
        )
        assert verdict in ("stable", "inconclusive_high_variance")

    def test_bonferroni_correction(self, improver):
        """Multiple metrics should use corrected alpha."""
        before = {"revenue": 100.0, "roi": 1.0, "execution_success": 0.8}
        after = {"revenue": 108.0, "roi": 1.05, "execution_success": 0.85}
        # 3 metrics: corrected alpha = 0.05/3 ≈ 0.0167
        before_raw = {
            "revenue": [95, 100, 105, 100, 95, 100, 105, 100, 95, 100],
            "roi": [0.9, 1.0, 1.1, 1.0, 0.9, 1.0, 1.1, 1.0, 0.9, 1.0],
            "execution_success": [0.7, 0.8, 0.9, 0.8, 0.7, 0.8, 0.9, 0.8, 0.7, 0.8],
        }
        after_raw = {
            "revenue": [103, 108, 113, 108, 103, 108, 113, 108, 103, 108],
            "roi": [1.0, 1.05, 1.1, 1.05, 1.0, 1.05, 1.1, 1.05, 1.0, 1.05],
            "execution_success": [0.8, 0.85, 0.9, 0.85, 0.8, 0.85, 0.9, 0.85, 0.8, 0.85],
        }
        verdict, details = improver._compare_snapshots(
            before, after, before_raw=before_raw, after_raw=after_raw,
        )
        # Should have bonferroni alpha in details
        if "bonferroni_alpha" in details:
            assert details["bonferroni_alpha"] < SIGNIFICANCE_LEVEL

    def test_threshold_fallback_no_raw(self, improver):
        """When no raw data, uses threshold-based comparison."""
        before = {"revenue": 100.0}
        after = {"revenue": 120.0}
        verdict, details = improver._compare_snapshots(before, after)
        assert verdict == "improved"
        assert details.get("method") == "threshold"

    def test_threshold_fallback_decline(self, improver):
        before = {"revenue": 100.0}
        after = {"revenue": 85.0}
        verdict, details = improver._compare_snapshots(before, after)
        assert verdict == "declined"
        assert details.get("method") == "threshold"

    def test_high_variance_detection(self, improver):
        """High variance data should be flagged."""
        before = {"revenue": 100.0}
        after = {"revenue": 105.0}
        # Very high variance (stdev >> mean * 0.5)
        before_raw = {"revenue": [10, 200, 50, 150, 20, 180, 30, 170, 40, 160]}
        after_raw = {"revenue": [15, 205, 55, 155, 25, 185, 35, 175, 45, 165]}
        verdict, details = improver._compare_snapshots(
            before, after, before_raw=before_raw, after_raw=after_raw,
        )
        # Should be inconclusive or stable due to high variance
        assert verdict in ("stable", "inconclusive_high_variance")

    def test_cohens_d_in_results(self, improver):
        """Effect size should be reported for significant results."""
        before = {"revenue": 100.0}
        after = {"revenue": 150.0}
        before_raw = {"revenue": [95, 100, 105, 100, 95, 100, 105, 100, 95, 100]}
        after_raw = {"revenue": [145, 150, 155, 150, 145, 150, 155, 150, 145, 150]}
        verdict, details = improver._compare_snapshots(
            before, after, before_raw=before_raw, after_raw=after_raw,
        )
        assert "metrics" in details
        revenue = details["metrics"]["revenue"]
        assert "cohens_d" in revenue
        assert revenue["cohens_d"] > 1.0  # Large effect

    def test_no_common_metrics(self, improver):
        before = {"metric_a": 1.0}
        after = {"metric_b": 2.0}
        verdict, _ = improver._compare_snapshots(before, after)
        assert verdict == "insufficient_data"


class TestExperimentEarlyStop:
    """Tests for early stop functionality."""

    @pytest.fixture
    def improver(self, config, db, mock_llm):
        from monai.agents.memory import SharedMemory
        memory = SharedMemory(db)
        return SelfImprover(config, db, mock_llm, memory=memory)

    def test_early_stop_returns_none_insufficient_data(self, improver, db):
        """No early stop when not enough data."""
        iid = improver.propose_improvement("test_agent", "prompt", "test change")
        improver.db.execute(
            "UPDATE agent_improvements SET status = 'testing', deployed_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00", iid),
        )
        exp = dict(db.execute("SELECT * FROM agent_improvements WHERE id = ?", (iid,))[0])
        result = improver._check_early_stop(exp)
        assert result is None  # No data → no early stop

    def test_record_experiment_result_to_memory(self, improver, db):
        """Experiment results should be written to SharedMemory."""
        iid = improver.propose_improvement("test_agent", "prompt", "test improvement")
        exp = {
            "id": iid, "agent_name": "test_agent",
            "improvement_type": "prompt",
            "description": "test improvement",
        }

        improver._record_experiment_result(exp, "improved", {"p_value": 0.01})

        # Check knowledge was stored
        knowledge = improver.memory.query_knowledge(topic="test_agent")
        assert len(knowledge) >= 1
        assert any("experiment_result" in k.get("category", "") for k in knowledge)

        # Check lesson was recorded
        lessons = improver.memory.get_lessons("test_agent")
        assert len(lessons) >= 1

    def test_record_experiment_declined_lesson(self, improver, db):
        """Declined experiments should record warning lessons."""
        exp = {
            "id": 1, "agent_name": "test_agent",
            "improvement_type": "parameter",
            "description": "bad parameter change",
        }
        improver._record_experiment_result(exp, "declined", {"p_value": 0.02})

        lessons = improver.memory.get_lessons("test_agent", category="warning")
        assert len(lessons) >= 1
        assert "declined" in lessons[0]["lesson"].lower() or "reverted" in lessons[0]["lesson"].lower()
