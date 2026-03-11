"""Tests for dashboard API endpoints (audit, brand P&L, backups)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monai.business.audit import AuditTrail
from monai.business.backup import BackupManager
from monai.business.finance import GeneralLedger
from monai.config import BackupConfig
from monai.dashboard.server import DashboardServer


@pytest.fixture
def dashboard(config, db):
    return DashboardServer(config, db, port=0)


@pytest.fixture
def audit(db):
    return AuditTrail(db)


@pytest.fixture
def ledger(db):
    return GeneralLedger(db)


class TestDashboardAuditAPI:
    def test_get_audit_empty(self, dashboard):
        """Audit endpoint returns empty list when no entries."""
        result = dashboard._get_audit({})
        assert result == []

    def test_get_audit_with_entries(self, dashboard):
        """Audit endpoint returns logged entries."""
        dashboard.audit.log("test_agent", "system", "test_action")
        dashboard.audit.log("test_agent", "payment", "payment_sent")

        result = dashboard._get_audit({})
        assert len(result) == 2

    def test_get_audit_filter_by_agent(self, dashboard):
        """Audit endpoint filters by agent name."""
        dashboard.audit.log("agent_a", "system", "action_1")
        dashboard.audit.log("agent_b", "system", "action_2")

        result = dashboard._get_audit({"agent": ["agent_a"]})
        assert len(result) == 1
        assert result[0]["agent_name"] == "agent_a"

    def test_get_audit_filter_by_type(self, dashboard):
        """Audit endpoint filters by action type."""
        dashboard.audit.log("agent", "payment", "pay")
        dashboard.audit.log("agent", "system", "sys")

        result = dashboard._get_audit({"type": ["payment"]})
        assert len(result) == 1
        assert result[0]["action_type"] == "payment"

    def test_get_audit_filter_by_brand(self, dashboard):
        """Audit endpoint filters by brand."""
        dashboard.audit.log("agent", "system", "action", brand="alpha")
        dashboard.audit.log("agent", "system", "action", brand="beta")

        result = dashboard._get_audit({"brand": ["alpha"]})
        assert len(result) == 1
        assert result[0]["brand"] == "alpha"

    def test_get_audit_filter_by_risk(self, dashboard):
        """Audit endpoint filters by risk level."""
        dashboard.audit.log("agent", "system", "normal")
        dashboard.audit.log("agent", "payment", "critical", risk_level="critical")

        result = dashboard._get_audit({"risk": ["critical"]})
        assert len(result) == 1
        assert result[0]["risk_level"] == "critical"

    def test_get_audit_with_limit(self, dashboard):
        """Audit endpoint respects limit parameter."""
        for i in range(10):
            dashboard.audit.log("agent", "system", f"action_{i}")

        result = dashboard._get_audit({"limit": ["3"]})
        assert len(result) == 3

    def test_get_audit_summary(self, dashboard):
        """Audit summary returns structured data."""
        dashboard.audit.log("agent_a", "system", "action_1")
        dashboard.audit.log("agent_b", "payment", "pay", risk_level="high")
        dashboard.audit.log("agent_c", "api_call", "fail", success=False)

        summary = dashboard._get_audit_summary(days=7)
        assert "agent_summary" in summary
        assert "high_risk" in summary
        assert "failures" in summary
        assert "total_actions" in summary
        assert summary["total_actions"] == 3
        assert len(summary["high_risk"]) == 1
        assert len(summary["failures"]) == 1


class TestDashboardBrandPnLAPI:
    def _record_branded_revenue(self, ledger, brand, amount):
        ledger.record_revenue(
            amount=amount, revenue_account="4000", cash_account="1010",
            description=f"{brand} sale", brand=brand, source="test",
        )

    def test_get_all_brands_pnl(self, dashboard, ledger):
        """Brand P&L endpoint returns all-brands breakdown."""
        self._record_branded_revenue(ledger, "brand_a", 500)
        self._record_branded_revenue(ledger, "brand_b", 300)

        result = dashboard._get_brand_pnl({})
        assert "brands" in result
        assert "consolidated" in result
        assert len(result["brands"]) == 2
        assert result["consolidated"]["total_revenue"] == 800.0

    def test_get_single_brand_pnl(self, dashboard, ledger):
        """Brand P&L for a specific brand."""
        self._record_branded_revenue(ledger, "brand_a", 500)
        self._record_branded_revenue(ledger, "brand_b", 300)

        result = dashboard._get_brand_pnl({"brand": ["brand_a"]})
        assert result["brand"] == "brand_a"
        assert result["total_revenue"] == 500.0

    def test_brand_pnl_with_date_range(self, dashboard, ledger):
        """Brand P&L accepts date range parameters."""
        self._record_branded_revenue(ledger, "brand_a", 500)

        result = dashboard._get_brand_pnl({
            "start": ["2020-01-01"],
            "end": ["2099-12-31"],
        })
        assert result["consolidated"]["total_revenue"] == 500.0

    def test_brand_pnl_empty(self, dashboard):
        """Brand P&L with no data."""
        result = dashboard._get_brand_pnl({})
        assert result["brands"] == []
        assert result["consolidated"]["total_revenue"] == 0.0


class TestDashboardBackupAPI:
    def test_get_backups_empty(self, dashboard):
        """Backup endpoint with no backups."""
        result = dashboard._get_backups()
        assert result["database"] == []
        assert result["config"] == []
        assert result["latest_db"] is None

    def test_get_backups_after_backup(self, dashboard):
        """Backup endpoint lists backups after creation."""
        dashboard.backup_manager.backup_database()

        result = dashboard._get_backups()
        assert len(result["database"]) == 1
        assert result["latest_db"] is not None
        assert result["latest_db"]["size_bytes"] > 0


class TestDashboardInitialization:
    def test_dashboard_has_new_modules(self, dashboard):
        """Dashboard initializes with audit, ledger, and backup modules."""
        assert isinstance(dashboard.audit, AuditTrail)
        assert isinstance(dashboard.ledger, GeneralLedger)
        assert isinstance(dashboard.backup_manager, BackupManager)
