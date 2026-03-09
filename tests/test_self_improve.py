"""Tests for monai.agents.self_improve."""

import json

import pytest

from monai.agents.self_improve import SelfImprover


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
