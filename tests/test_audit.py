"""Tests for monai.business.audit — audit trail / activity log."""

from __future__ import annotations

import pytest

from monai.business.audit import AuditTrail


@pytest.fixture
def audit(db):
    return AuditTrail(db)


class TestAuditLogging:
    def test_log_action(self, audit):
        """Basic action logging returns entry ID."""
        entry_id = audit.log("orchestrator", "system", "cycle_start")
        assert entry_id >= 1

    def test_log_with_details(self, audit):
        """Action logged with dict details stored as JSON."""
        entry_id = audit.log(
            "sweep_engine", "payment", "fund_transfer",
            details={"amount": 100.0, "currency": "EUR"},
            result="completed",
            brand="test_brand",
        )
        entries = audit.get_recent(limit=1)
        assert len(entries) == 1
        assert entries[0]["id"] == entry_id
        assert '"amount": 100.0' in entries[0]["details"]
        assert entries[0]["brand"] == "test_brand"

    def test_log_failure(self, audit):
        """Failed actions recorded with success=0."""
        audit.log("provisioner", "api_call", "stripe_setup",
                  success=False, result="API key invalid")
        entries = audit.get_recent(success=False)
        assert len(entries) == 1
        assert entries[0]["success"] == 0
        assert entries[0]["result"] == "API key invalid"

    def test_auto_risk_assessment_payment(self, audit):
        """Payment actions auto-assessed as high risk."""
        audit.log("sweep_engine", "payment", "fund_transfer")
        entries = audit.get_recent(limit=1)
        assert entries[0]["risk_level"] == "high"

    def test_auto_risk_assessment_deploy(self, audit):
        """Deploy actions auto-assessed as medium risk."""
        audit.log("web_presence", "deploy", "push_to_production")
        entries = audit.get_recent(limit=1)
        assert entries[0]["risk_level"] == "medium"

    def test_auto_risk_assessment_system(self, audit):
        """Regular system actions are low risk."""
        audit.log("orchestrator", "system", "cycle_complete")
        entries = audit.get_recent(limit=1)
        assert entries[0]["risk_level"] == "low"

    def test_manual_risk_override(self, audit):
        """Explicit risk_level overrides auto-detection."""
        audit.log("ethics", "system", "ethics_violation",
                  risk_level="critical")
        entries = audit.get_recent(limit=1)
        assert entries[0]["risk_level"] == "critical"

    def test_log_with_strategy_id(self, audit):
        """Strategy ID stored on audit entry."""
        audit.log("executor", "content", "publish_post",
                  strategy_id=42)
        entries = audit.get_recent(limit=1)
        assert entries[0]["strategy_id"] == 42

    def test_log_with_metadata(self, audit):
        """Metadata JSON stored separately from details."""
        audit.log("coder", "api_call", "llm_call",
                  details="Generated code",
                  metadata={"model": "gpt-4", "tokens": 1500})
        entries = audit.get_recent(limit=1)
        assert '"model": "gpt-4"' in entries[0]["metadata"]


class TestAuditQueries:
    def _populate(self, audit):
        """Create a mix of audit entries for testing."""
        audit.log("orchestrator", "system", "cycle_start", brand="alpha")
        audit.log("sweep_engine", "payment", "fund_transfer",
                  brand="alpha", success=True)
        audit.log("provisioner", "api_call", "stripe_setup",
                  brand="beta", success=False)
        audit.log("executor", "content", "publish_post",
                  brand="alpha")
        audit.log("ethics", "system", "ethics_check",
                  risk_level="critical")

    def test_filter_by_agent(self, audit):
        self._populate(audit)
        entries = audit.get_recent(agent_name="orchestrator")
        assert all(e["agent_name"] == "orchestrator" for e in entries)

    def test_filter_by_action_type(self, audit):
        self._populate(audit)
        entries = audit.get_recent(action_type="payment")
        assert len(entries) == 1
        assert entries[0]["action"] == "fund_transfer"

    def test_filter_by_brand(self, audit):
        self._populate(audit)
        entries = audit.get_recent(brand="alpha")
        assert len(entries) == 3

    def test_filter_by_risk_level(self, audit):
        self._populate(audit)
        entries = audit.get_recent(risk_level="critical")
        assert len(entries) == 1

    def test_filter_failures_only(self, audit):
        self._populate(audit)
        entries = audit.get_recent(success=False)
        assert len(entries) == 1

    def test_get_high_risk_entries(self, audit):
        self._populate(audit)
        entries = audit.get_high_risk_entries()
        # payment = high, ethics = critical
        assert len(entries) == 2

    def test_get_failures(self, audit):
        self._populate(audit)
        failures = audit.get_failures()
        assert len(failures) == 1
        assert failures[0]["agent_name"] == "provisioner"

    def test_count_actions(self, audit):
        self._populate(audit)
        assert audit.count_actions() == 5
        assert audit.count_actions(agent_name="orchestrator") == 1

    def test_agent_summary(self, audit):
        self._populate(audit)
        summary = audit.get_agent_summary()
        assert len(summary) >= 4  # 4 different agent/action_type combos
        total = sum(s["total"] for s in summary)
        assert total == 5

    def test_date_range_query(self, audit):
        self._populate(audit)
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        entries = audit.get_by_date_range(today, today)
        assert len(entries) == 5


class TestAuditReporting:
    def test_telegram_report(self, audit):
        """Telegram report formats correctly."""
        audit.log("orchestrator", "system", "cycle_start")
        audit.log("sweep_engine", "payment", "fund_transfer")
        audit.log("provisioner", "api_call", "setup", success=False)

        report = audit.format_telegram_report()
        assert "Audit Report" in report
        assert "Total actions:" in report
        assert "Failures:" in report
        assert "```" in report

    def test_telegram_report_empty(self, audit):
        """Report with no data still formats."""
        report = audit.format_telegram_report()
        assert "Total actions:" in report
        assert "0" in report
