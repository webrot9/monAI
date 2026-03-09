"""Tests for monai.utils.llm — CostTracker only (no real API calls)."""

from monai.utils.llm import CostTracker, MODEL_PRICING, get_cost_tracker


class TestCostTracker:
    def test_initial_state(self):
        tracker = CostTracker()
        assert tracker.calls == 0
        assert tracker.total_cost_eur == 0.0
        assert tracker.total_input_tokens == 0
        assert tracker.total_output_tokens == 0

    def test_record_returns_cost(self):
        tracker = CostTracker()
        cost = tracker.record("gpt-4o-mini", 1000, 500, "test_agent")
        assert cost > 0
        expected = (1000 * 0.15 + 500 * 0.60) / 1_000_000
        assert abs(cost - expected) < 1e-10

    def test_record_accumulates(self):
        tracker = CostTracker()
        tracker.record("gpt-4o-mini", 100, 50, "agent_a")
        tracker.record("gpt-4o-mini", 200, 100, "agent_b")
        assert tracker.calls == 2
        assert tracker.total_input_tokens == 300
        assert tracker.total_output_tokens == 150

    def test_cost_by_model(self):
        tracker = CostTracker()
        tracker.record("gpt-4o", 100, 50, "test")
        tracker.record("gpt-4o-mini", 100, 50, "test")
        assert "gpt-4o" in tracker.cost_by_model
        assert "gpt-4o-mini" in tracker.cost_by_model
        # gpt-4o is more expensive
        assert tracker.cost_by_model["gpt-4o"] > tracker.cost_by_model["gpt-4o-mini"]

    def test_cost_by_caller(self):
        tracker = CostTracker()
        tracker.record("gpt-4o-mini", 100, 50, "writer")
        tracker.record("gpt-4o-mini", 200, 100, "researcher")
        assert "writer" in tracker.cost_by_caller
        assert "researcher" in tracker.cost_by_caller
        assert tracker.cost_by_caller["researcher"] > tracker.cost_by_caller["writer"]

    def test_get_summary(self):
        tracker = CostTracker()
        tracker.record("gpt-4o-mini", 1000, 500, "test")
        summary = tracker.get_summary()
        assert summary["total_calls"] == 1
        assert summary["total_input_tokens"] == 1000
        assert summary["total_output_tokens"] == 500
        assert summary["total_cost_eur"] > 0
        assert "gpt-4o-mini" in summary["cost_by_model"]
        assert "test" in summary["cost_by_caller"]

    def test_unknown_model_uses_fallback(self):
        tracker = CostTracker()
        cost = tracker.record("unknown-model-xyz", 1000, 500, "test")
        # Should use gpt-4o-mini pricing as fallback
        expected = (1000 * 0.15 + 500 * 0.60) / 1_000_000
        assert abs(cost - expected) < 1e-10


class TestModelPricing:
    def test_all_models_have_input_and_output(self):
        for model, pricing in MODEL_PRICING.items():
            assert "input" in pricing, f"{model} missing input pricing"
            assert "output" in pricing, f"{model} missing output pricing"
            assert pricing["input"] > 0
            assert pricing["output"] > 0

    def test_mini_cheaper_than_full(self):
        assert MODEL_PRICING["gpt-4o-mini"]["input"] < MODEL_PRICING["gpt-4o"]["input"]
        assert MODEL_PRICING["gpt-4o-mini"]["output"] < MODEL_PRICING["gpt-4o"]["output"]


class TestGlobalTracker:
    def test_get_cost_tracker_returns_singleton(self):
        t1 = get_cost_tracker()
        t2 = get_cost_tracker()
        assert t1 is t2
