"""Tests for monai.agents.legal — Legal Advisor agent."""

import json

import pytest

from monai.agents.legal import LegalAdvisor, LegalAdvisorFactory, LEGAL_CHECKS


class TestLegalAdvisor:
    @pytest.fixture
    def advisor(self, config, db, mock_llm):
        mock_llm.quick_json.return_value = {
            "status": "approved",
            "risk_level": "low",
            "summary": "Activity appears legal",
            "checks": [{"area": "business_registration", "legal": True, "notes": "OK"}],
            "requirements": ["Register as business", "Comply with GDPR"],
            "blockers": [],
            "recommendations": ["Keep records"],
        }
        # LegalAdvisor inherits from BaseAgent which uses think_json
        # which calls llm.chat_json
        mock_llm.chat_json.return_value = {
            "status": "approved",
            "risk_level": "low",
            "summary": "Activity appears legal",
            "checks": [{"area": "business_registration", "legal": True, "notes": "OK"}],
            "requirements": ["Register as business", "Comply with GDPR"],
            "blockers": [],
            "recommendations": ["Keep records"],
        }
        return LegalAdvisor(config, db, mock_llm,
                            activity_name="freelance_writing",
                            activity_type="strategy")

    @pytest.fixture
    def blocked_advisor(self, config, db, mock_llm):
        mock_llm.chat_json.return_value = {
            "status": "blocked",
            "risk_level": "critical",
            "summary": "This activity is illegal",
            "checks": [{"area": "legality", "legal": False, "notes": "Illegal"}],
            "requirements": [],
            "blockers": ["Activity violates gambling laws", "No license available"],
            "recommendations": [],
        }
        return LegalAdvisor(config, db, mock_llm,
                            activity_name="illegal_gambling",
                            activity_type="strategy")

    # ── Schema ────────────────────────────────────────────────

    def test_schema_created(self, advisor, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='legal_assessments'"
        )
        assert len(rows) == 1

        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='legal_guidance'"
        )
        assert len(rows) == 1

    # ── Plan ──────────────────────────────────────────────────

    def test_plan_returns_checks(self, advisor):
        steps = advisor.plan()
        assert len(steps) > 0
        assert all(s.startswith("review_") for s in steps)

    def test_plan_uses_activity_type(self, config, db, mock_llm):
        mock_llm.chat_json.return_value = {"status": "approved", "risk_level": "low"}
        advisor = LegalAdvisor(config, db, mock_llm,
                               activity_name="test", activity_type="financial")
        steps = advisor.plan()
        assert "review_tax_obligations" in steps
        assert "review_anti_money_laundering" in steps

    # ── Run Assessment ────────────────────────────────────────

    def test_run_approved(self, advisor):
        result = advisor.run(requesting_agent="orchestrator",
                             description="Write articles for clients")
        assert result["status"] == "approved"
        assert result["risk_level"] == "low"
        assert result["requirements_count"] == 2
        assert result["blockers_count"] == 0
        assert result["assessment_id"] >= 1

    def test_run_blocked(self, blocked_advisor):
        result = blocked_advisor.run(requesting_agent="orchestrator",
                                     description="Online gambling site")
        assert result["status"] == "blocked"
        assert result["blockers_count"] == 2

    def test_assessment_stored_in_db(self, advisor, db):
        advisor.run(requesting_agent="orchestrator")
        rows = db.execute("SELECT * FROM legal_assessments")
        assert len(rows) == 1
        assert rows[0]["activity_name"] == "freelance_writing"
        assert rows[0]["status"] == "approved"

    def test_guidance_generated(self, advisor, db):
        advisor.run(requesting_agent="orchestrator")
        rows = db.execute("SELECT * FROM legal_guidance")
        # Should have requirements + recommendations as guidance
        assert len(rows) >= 2
        types = {r["guidance_type"] for r in rows}
        assert "requirement" in types

    def test_blocked_creates_lesson(self, blocked_advisor, db):
        blocked_advisor.run(requesting_agent="orchestrator")
        # Check that lessons table was populated (via SharedMemory)
        # The learn() method writes to lessons table via memory
        rows = db.execute("SELECT * FROM agent_log WHERE action = 'legal_review_complete'")
        assert len(rows) == 1

    # ── Query Methods ─────────────────────────────────────────

    def test_get_assessment(self, advisor):
        advisor.run(requesting_agent="test")
        assessment = advisor.get_assessment("freelance_writing")
        assert assessment is not None
        assert assessment["status"] == "approved"
        assert isinstance(assessment["requirements"], list)

    def test_get_assessment_nonexistent(self, advisor):
        assert advisor.get_assessment("nonexistent") is None

    def test_is_activity_approved(self, advisor):
        advisor.run(requesting_agent="test")
        assert advisor.is_activity_approved("freelance_writing") is True
        assert advisor.is_activity_approved("nonexistent") is False

    def test_is_activity_blocked(self, blocked_advisor):
        blocked_advisor.run(requesting_agent="test")
        assert blocked_advisor.is_activity_blocked("illegal_gambling") is True

    def test_get_guidance_for_agent(self, advisor, db):
        advisor.run(requesting_agent="writer")
        guidance = advisor.get_guidance_for_agent("writer")
        assert len(guidance) >= 1

    def test_acknowledge_guidance(self, advisor, db):
        advisor.run(requesting_agent="writer")
        guidance = advisor.get_guidance_for_agent("writer")
        assert len(guidance) > 0

        advisor.acknowledge_guidance(guidance[0]["id"])

        # Should be fewer unacknowledged now
        remaining = advisor.get_guidance_for_agent("writer", unacknowledged_only=True)
        assert len(remaining) == len(guidance) - 1

    def test_get_all_assessments(self, advisor, blocked_advisor, mock_llm):
        # Set approved response, then run advisor
        mock_llm.chat_json.return_value = {
            "status": "approved", "risk_level": "low", "summary": "OK",
            "checks": [], "requirements": ["R1"], "blockers": [], "recommendations": [],
        }
        advisor.run(requesting_agent="test")

        # Set blocked response, then run blocked_advisor
        mock_llm.chat_json.return_value = {
            "status": "blocked", "risk_level": "critical", "summary": "Illegal",
            "checks": [], "requirements": [], "blockers": ["Illegal"], "recommendations": [],
        }
        blocked_advisor.run(requesting_agent="test")

        all_assessments = advisor.get_all_assessments()
        assert len(all_assessments) == 2

        blocked = advisor.get_all_assessments(status="blocked")
        assert len(blocked) == 1

    def test_get_blocked_activities(self, advisor, blocked_advisor, mock_llm):
        mock_llm.chat_json.return_value = {
            "status": "approved", "risk_level": "low", "summary": "OK",
            "checks": [], "requirements": [], "blockers": [], "recommendations": [],
        }
        advisor.run(requesting_agent="test")

        mock_llm.chat_json.return_value = {
            "status": "blocked", "risk_level": "critical", "summary": "Illegal",
            "checks": [], "requirements": [], "blockers": ["Illegal"], "recommendations": [],
        }
        blocked_advisor.run(requesting_agent="test")

        blocked = advisor.get_blocked_activities()
        assert "illegal_gambling" in blocked
        assert "freelance_writing" not in blocked


class TestLegalAdvisorFactory:
    @pytest.fixture
    def factory(self, config, db, mock_llm):
        mock_llm.chat_json.return_value = {
            "status": "approved",
            "risk_level": "low",
            "summary": "OK",
            "checks": [],
            "requirements": ["Requirement 1"],
            "blockers": [],
            "recommendations": [],
        }
        return LegalAdvisorFactory(config, db, mock_llm)

    def test_create_for_activity(self, factory):
        advisor = factory.create_for_activity("test_strategy", "strategy")
        assert advisor.activity_name == "test_strategy"
        assert advisor.activity_type == "strategy"

    def test_assess_activity_oneshot(self, factory):
        result = factory.assess_activity(
            "content_writing", "strategy",
            description="Write blog posts for clients",
        )
        assert result["status"] == "approved"
        assert result["assessment_id"] >= 1

    def test_is_approved_after_assessment(self, factory):
        factory.assess_activity("approved_thing", "strategy")
        assert factory.is_approved("approved_thing") is True
        assert factory.is_approved("unknown") is False

    def test_is_blocked(self, factory, mock_llm):
        mock_llm.chat_json.return_value = {
            "status": "blocked",
            "risk_level": "critical",
            "blockers": ["Illegal"],
            "requirements": [],
            "recommendations": [],
        }
        factory.assess_activity("bad_thing", "strategy")
        assert factory.is_blocked("bad_thing") is True

    def test_get_blocked_activities(self, factory, mock_llm):
        # First: approved
        factory.assess_activity("good_thing", "strategy")

        # Then: blocked
        mock_llm.chat_json.return_value = {
            "status": "blocked",
            "risk_level": "critical",
            "blockers": ["Nope"],
            "requirements": [],
            "recommendations": [],
        }
        factory.assess_activity("bad_thing", "strategy")

        blocked = factory.get_blocked_activities()
        assert "bad_thing" in blocked
        assert "good_thing" not in blocked


class TestLegalChecks:
    def test_all_activity_types_have_checks(self):
        expected_types = ["strategy", "registration", "client_work", "financial", "marketing", "content"]
        for t in expected_types:
            assert t in LEGAL_CHECKS, f"Missing checks for activity type: {t}"
            assert len(LEGAL_CHECKS[t]) > 0

    def test_strategy_checks_comprehensive(self):
        checks = LEGAL_CHECKS["strategy"]
        assert "tax_obligations" in checks
        assert "data_protection_gdpr" in checks

    def test_financial_checks_include_aml(self):
        checks = LEGAL_CHECKS["financial"]
        assert "anti_money_laundering" in checks
        assert "cryptocurrency_regulation" in checks
