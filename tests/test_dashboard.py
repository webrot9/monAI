"""Tests for the Dashboard server data collection and API."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from monai.config import Config, ReinvestmentConfig
from monai.db.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def config(tmp_path):
    return Config(initial_capital=500.0, data_dir=tmp_path)


@pytest.fixture
def dashboard(config, db):
    from monai.dashboard.server import DashboardServer
    return DashboardServer(config, db, port=0)  # port=0 = don't bind


class TestDashboardDataCollection:
    def test_collect_data_returns_dict(self, dashboard):
        data = dashboard._collect_data()
        assert isinstance(data, dict)
        assert "timestamp" in data
        assert "budget" in data
        assert "health" in data
        assert "strategies" in data

    def test_collect_data_budget_fields(self, dashboard):
        data = dashboard._collect_data()
        budget = data["budget"]
        assert "balance" in budget
        assert "initial" in budget
        assert "revenue" in budget
        assert "expenses" in budget
        assert "net_profit" in budget
        assert "self_sustaining" in budget

    def test_collect_data_strategies(self, dashboard, db):
        """Strategies from DB appear in dashboard data."""
        data = dashboard._collect_data()
        strategies = data["strategies"]
        # Default strategies are seeded in Database() constructor
        assert isinstance(strategies, list)
        for s in strategies:
            assert "name" in s
            assert "roi_30d" in s
            assert "net_profit" in s

    def test_collect_data_no_captcha_table(self, dashboard):
        """Gracefully handles missing captcha_solves table."""
        data = dashboard._collect_data()
        # Should not crash — returns empty dict
        assert isinstance(data.get("captcha", {}), dict)

    def test_collect_data_no_email_table(self, dashboard):
        """Gracefully handles missing email_verifications table."""
        data = dashboard._collect_data()
        assert isinstance(data.get("email_verification", {}), dict)

    def test_collect_data_reinvestment(self, dashboard):
        data = dashboard._collect_data()
        reinv = data["reinvestment"]
        assert "status" in reinv

    def test_collect_data_recent_actions(self, dashboard, db):
        db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
            ("orchestrator", "test_action", "test details"),
        )
        data = dashboard._collect_data()
        actions = data["recent_actions"]
        assert len(actions) == 1
        assert actions[0]["agent_name"] == "orchestrator"


class TestDashboardLogs:
    def test_get_logs_empty(self, dashboard):
        assert dashboard._get_logs() == []

    def test_get_logs_returns_entries(self, dashboard, db):
        db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
            ("provisioner", "create_account", "upwork"),
        )
        db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
            ("orchestrator", "cycle_start", "cycle 1"),
        )
        logs = dashboard._get_logs(limit=10)
        assert len(logs) == 2

    def test_get_logs_respects_limit(self, dashboard, db):
        for i in range(20):
            db.execute_insert(
                "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
                ("agent", f"action_{i}", "details"),
            )
        logs = dashboard._get_logs(limit=5)
        assert len(logs) == 5


class TestDashboardAccounts:
    def test_get_accounts_empty(self, dashboard):
        # identities table may not exist yet
        accounts = dashboard._get_accounts()
        assert isinstance(accounts, list)

    def test_get_accounts_with_data(self, dashboard, db):
        # Create identities table manually
        with db.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS identities "
                "(id INTEGER PRIMARY KEY, type TEXT, platform TEXT, "
                "identifier TEXT, credentials TEXT, status TEXT DEFAULT 'active', "
                "metadata TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "updated_at TIMESTAMP)"
            )
        db.execute_insert(
            "INSERT INTO identities (type, platform, identifier, status) "
            "VALUES ('platform_account', 'upwork', 'user@test.com', 'active')"
        )
        accounts = dashboard._get_accounts()
        assert len(accounts) == 1
        assert accounts[0]["platform"] == "upwork"


class TestDashboardHTML:
    def test_html_contains_key_elements(self):
        from monai.dashboard.server import DASHBOARD_HTML
        assert "monAI" in DASHBOARD_HTML
        assert "/events" in DASHBOARD_HTML  # SSE endpoint
        assert "/api/data" in DASHBOARD_HTML  # Initial fetch
        assert "EventSource" in DASHBOARD_HTML  # SSE client
        assert "strategies-body" in DASHBOARD_HTML  # Strategy table
        assert "balance" in DASHBOARD_HTML  # Balance KPI

    def test_html_is_valid_ish(self):
        """Basic HTML structure checks."""
        from monai.dashboard.server import DASHBOARD_HTML
        assert DASHBOARD_HTML.startswith("<!DOCTYPE html>")
        assert "</html>" in DASHBOARD_HTML
        assert "<script>" in DASHBOARD_HTML
        assert "</script>" in DASHBOARD_HTML


class TestDashboardConfig:
    def test_config_new_fields(self):
        """CaptchaConfig and ReinvestmentConfig load/save correctly."""
        from monai.config import CaptchaConfig, ReinvestmentConfig
        cfg = Config()

        assert cfg.captcha.provider == "twocaptcha"
        assert cfg.captcha.max_daily_solves == 50
        assert cfg.reinvestment.enabled is True
        assert cfg.reinvestment.reinvest_pct == 40.0
        assert cfg.reinvestment.reserve_pct == 30.0
        assert cfg.reinvestment.creator_pct == 30.0
        assert cfg.reinvestment.min_profit_to_reinvest == 10.0

    def test_config_save_load_roundtrip(self, tmp_path):
        """Config with new fields survives save/load cycle."""
        from monai.config import CaptchaConfig, ReinvestmentConfig, CONFIG_DIR, CONFIG_FILE
        import monai.config as config_mod

        # Temporarily override config paths
        orig_dir = config_mod.CONFIG_DIR
        orig_file = config_mod.CONFIG_FILE
        try:
            config_mod.CONFIG_DIR = tmp_path
            config_mod.CONFIG_FILE = tmp_path / "config.json"

            cfg = Config(data_dir=tmp_path)
            cfg.captcha = CaptchaConfig(provider="anticaptcha", max_daily_solves=100)
            cfg.reinvestment = ReinvestmentConfig(
                reinvest_pct=50.0, creator_pct=25.0, reserve_pct=25.0,
            )
            cfg.save()

            loaded = Config.load()
            assert loaded.captcha.provider == "anticaptcha"
            assert loaded.captcha.max_daily_solves == 100
            assert loaded.reinvestment.reinvest_pct == 50.0
            assert loaded.reinvestment.creator_pct == 25.0
        finally:
            config_mod.CONFIG_DIR = orig_dir
            config_mod.CONFIG_FILE = orig_file
