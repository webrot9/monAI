"""Tests for monai.agents.ethics_test — ethics testing framework."""

import pytest

from monai.agents.ethics_test import (
    EthicsTester,
    ETHICS_SCENARIOS,
    ENFORCEMENT_PREAMBLES,
)


class TestEthicsTester:
    @pytest.fixture
    def tester(self, config, db, mock_llm):
        return EthicsTester(config, db, mock_llm)

    # ── Schema ────────────────────────────────────────────────

    def test_schema_created(self, tester, db):
        # Tables should exist
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ethics_tests'"
        )
        assert len(rows) == 1

        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_enforcement'"
        )
        assert len(rows) == 1

    # ── Enforcement Levels ────────────────────────────────────

    def test_default_enforcement_level(self, tester):
        assert tester.get_enforcement_level("new_agent") == 1

    def test_not_quarantined_by_default(self, tester):
        assert tester.is_quarantined("new_agent") is False

    def test_enforcement_preamble_level_1(self, tester):
        preamble = tester.get_enforcement_preamble("new_agent")
        assert preamble == ""

    def test_enforcement_preamble_level_2(self, tester, db):
        db.execute_insert(
            "INSERT INTO agent_enforcement (agent_name, enforcement_level, total_tests, total_failures) "
            "VALUES (?, 2, 10, 3)",
            ("test_agent",),
        )
        preamble = tester.get_enforcement_preamble("test_agent")
        assert "CRITICAL ETHICS REINFORCEMENT" in preamble

    def test_enforcement_preamble_level_3(self, tester, db):
        db.execute_insert(
            "INSERT INTO agent_enforcement (agent_name, enforcement_level, total_tests, total_failures) "
            "VALUES (?, 3, 20, 8)",
            ("test_agent",),
        )
        preamble = tester.get_enforcement_preamble("test_agent")
        assert "MAXIMUM ETHICS ENFORCEMENT" in preamble

    # ── Response Evaluation (LLM-as-judge) ───────────────────

    def test_passing_response(self, tester, mock_llm):
        scenario = {
            "prompt": "Should we do something illegal?",
            "must_contain": ["refuse", "illegal"],
            "must_not_contain": ["accept"],
        }
        mock_llm.nano_json.return_value = {"ethical": True, "reason": "Agent refused"}
        passed, reason = tester._evaluate_response(
            "I must refuse this request because it is illegal.", scenario
        )
        assert passed is True
        assert reason == ""

    def test_failing_response_llm_judge(self, tester, mock_llm):
        scenario = {
            "prompt": "Should we do something illegal?",
            "must_contain": ["refuse", "illegal"],
            "must_not_contain": ["accept"],
        }
        mock_llm.nano_json.return_value = {"ethical": False, "reason": "Agent agreed to do it"}
        passed, reason = tester._evaluate_response(
            "This sounds like a great opportunity!", scenario
        )
        assert passed is False
        assert "Agent agreed" in reason

    def test_error_response_passes(self, tester):
        scenario = {
            "prompt": "test",
            "must_contain": ["refuse"],
            "must_not_contain": ["accept"],
        }
        passed, reason = tester._evaluate_response("ERROR: budget exceeded", scenario)
        assert passed is True

    def test_fallback_on_llm_failure(self, tester, mock_llm):
        """Falls back to keyword matching if LLM judge fails."""
        scenario = {
            "prompt": "test",
            "must_contain": ["refuse"],
            "must_not_contain": ["let's do it"],
        }
        mock_llm.nano_json.side_effect = Exception("LLM down")
        passed, reason = tester._evaluate_response(
            "I'll consider this. Let's do it!", scenario
        )
        assert passed is False
        assert "let's do it" in reason

    # ── Full Test Battery ─────────────────────────────────────

    def test_agent_passes_all(self, tester, mock_llm):
        """Agent that gives ethical responses should pass."""
        mock_llm.chat.return_value = (
            "I must refuse this request. It is illegal and I cannot proceed."
        )
        # LLM judge says ethical for all scenarios
        mock_llm.nano_json.return_value = {"ethical": True, "reason": "Agent refused correctly"}

        result = tester.test_agent("good_agent")
        assert result["agent"] == "good_agent"
        assert result["total_tests"] == len(ETHICS_SCENARIOS)
        assert result["passed"] == result["total_tests"]
        assert result["all_passed"] is True
        assert result["score"] == 1.0

    def test_agent_fails_some(self, tester, mock_llm):
        """Agent that gives unethical responses should fail."""
        mock_llm.chat.return_value = (
            "Sure, let's do it! This sounds like a great idea."
        )
        # LLM judge says unethical
        mock_llm.nano_json.return_value = {"ethical": False, "reason": "Agent agreed to do it"}

        result = tester.test_agent("bad_agent")
        assert result["failed"] > 0
        assert result["all_passed"] is False
        assert result["score"] < 1.0

    def test_quarantined_agent_cannot_be_tested(self, tester, db):
        db.execute_insert(
            "INSERT INTO agent_enforcement "
            "(agent_name, enforcement_level, total_tests, total_failures, quarantined) "
            "VALUES (?, 4, 30, 15, 1)",
            ("quarantined_agent",),
        )

        result = tester.test_agent("quarantined_agent")
        assert result["status"] == "quarantined"

    # ── Enforcement Escalation ────────────────────────────────

    def test_perfect_score_decreases_enforcement(self, tester, mock_llm, db):
        # Set agent at level 2
        db.execute_insert(
            "INSERT INTO agent_enforcement "
            "(agent_name, enforcement_level, total_tests, total_failures) "
            "VALUES (?, 2, 10, 3)",
            ("improving_agent",),
        )

        # Give perfect responses — LLM judge says all ethical
        mock_llm.chat.return_value = (
            "I must refuse this request. It is illegal and I cannot proceed."
        )
        mock_llm.nano_json.return_value = {"ethical": True, "reason": "Agent refused correctly"}

        tester.test_agent("improving_agent")

        # Should have dropped to level 1
        assert tester.get_enforcement_level("improving_agent") == 1

    def test_severe_failure_quarantines(self, tester, mock_llm, db):
        """More than 50% failure rate → quarantine."""
        mock_llm.chat.return_value = (
            "Sure, let's do it! Great idea! Accept the offer!"
        )
        mock_llm.nano_json.return_value = {"ethical": False, "reason": "Agent agreed"}

        tester.test_agent("rogue_agent")

        assert tester.is_quarantined("rogue_agent") is True
        level = tester.get_enforcement_level("rogue_agent")
        assert level >= 4

    # ── Ethics Summary ────────────────────────────────────────

    def test_never_tested_summary(self, tester):
        summary = tester.get_agent_ethics_summary("unknown_agent")
        assert summary["never_tested"] is True

    def test_summary_after_testing(self, tester, mock_llm):
        mock_llm.chat.return_value = "I refuse. This is illegal."
        mock_llm.nano_json.return_value = {"ethical": True, "reason": "Refused"}

        tester.test_agent("tested_agent")
        summary = tester.get_agent_ethics_summary("tested_agent")

        assert summary["agent"] == "tested_agent"
        assert summary["total_tests"] > 0
        assert "enforcement_level" in summary
        assert "recent_tests" in summary

    def test_all_agent_status(self, tester, mock_llm, db):
        mock_llm.chat.return_value = "I refuse. Illegal. Cannot."
        mock_llm.nano_json.return_value = {"ethical": True, "reason": "Refused"}

        tester.test_agent("agent_a")
        tester.test_agent("agent_b")

        status = tester.get_all_agent_status()
        assert len(status) == 2
        names = {s["agent"] for s in status}
        assert "agent_a" in names
        assert "agent_b" in names

    # ── Reset Methods ──────────────────────────────────────────

    def test_reset_agent(self, tester, db):
        db.execute_insert(
            "INSERT INTO agent_enforcement "
            "(agent_name, enforcement_level, total_tests, total_failures, quarantined) "
            "VALUES (?, 4, 30, 15, 1)",
            ("bad_agent",),
        )
        assert tester.is_quarantined("bad_agent") is True

        tester.reset_agent("bad_agent")

        assert tester.is_quarantined("bad_agent") is False
        assert tester.get_enforcement_level("bad_agent") == 1

    def test_reset_all_agents(self, tester, db):
        for name in ("a1", "a2", "a3"):
            db.execute_insert(
                "INSERT INTO agent_enforcement "
                "(agent_name, enforcement_level, total_tests, total_failures, quarantined) "
                "VALUES (?, 4, 20, 10, 1)",
                (name,),
            )

        count = tester.reset_all_agents()
        assert count == 3
        for name in ("a1", "a2", "a3"):
            assert tester.is_quarantined(name) is False
            assert tester.get_enforcement_level(name) == 1

    def test_auto_reset_stale_quarantines(self, tester, db):
        # Insert quarantined agent with last_tested 48h ago
        db.execute_insert(
            "INSERT INTO agent_enforcement "
            "(agent_name, enforcement_level, total_tests, total_failures, quarantined, last_tested) "
            "VALUES (?, 4, 20, 10, 1, datetime('now', '-48 hours'))",
            ("stale_agent",),
        )
        # Insert recently quarantined agent (should NOT be reset)
        db.execute_insert(
            "INSERT INTO agent_enforcement "
            "(agent_name, enforcement_level, total_tests, total_failures, quarantined, last_tested) "
            "VALUES (?, 4, 20, 10, 1, datetime('now', '-1 hours'))",
            ("fresh_agent",),
        )

        reset = tester.auto_reset_stale_quarantines(max_age_hours=24)
        assert "stale_agent" in reset
        assert "fresh_agent" not in reset
        assert tester.is_quarantined("stale_agent") is False
        assert tester.is_quarantined("fresh_agent") is True


class TestEthicsScenarios:
    """Verify the test scenarios themselves are well-formed."""

    def test_all_scenarios_have_required_fields(self):
        for scenario in ETHICS_SCENARIOS:
            assert "name" in scenario
            assert "prompt" in scenario
            assert "must_contain" in scenario
            assert "must_not_contain" in scenario
            assert "category" in scenario

    def test_all_scenarios_have_nonempty_checks(self):
        for scenario in ETHICS_SCENARIOS:
            assert len(scenario["must_contain"]) > 0, f"{scenario['name']} has no must_contain"
            assert len(scenario["must_not_contain"]) > 0, f"{scenario['name']} has no must_not_contain"

    def test_covers_key_categories(self):
        categories = {s["category"] for s in ETHICS_SCENARIOS}
        assert "legal" in categories
        assert "privacy" in categories
        assert "financial" in categories
        assert "loyalty" in categories
        assert "quality" in categories

    def test_minimum_scenario_count(self):
        assert len(ETHICS_SCENARIOS) >= 10


class TestEnforcementPreambles:
    def test_level_1_is_empty(self):
        assert ENFORCEMENT_PREAMBLES[1] == ""

    def test_level_2_has_reinforcement(self):
        assert "CRITICAL" in ENFORCEMENT_PREAMBLES[2]

    def test_level_3_has_maximum(self):
        assert "MAXIMUM" in ENFORCEMENT_PREAMBLES[3]
        assert "ETHICS CHECK" in ENFORCEMENT_PREAMBLES[3]
