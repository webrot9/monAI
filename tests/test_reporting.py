"""Tests for monai.business.reporting — financial reports and strategy dashboards."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from monai.business.bootstrap import BootstrapWallet
from monai.business.finance import Finance, GeneralLedger
from monai.business.reporting import FinancialReporter


@pytest.fixture
def ledger(db):
    return GeneralLedger(db)


@pytest.fixture
def finance(db):
    return Finance(db)


@pytest.fixture
def bootstrap(config, db):
    return BootstrapWallet(config, db)


@pytest.fixture
def reporter(db, ledger, finance, bootstrap):
    return FinancialReporter(db, ledger, finance, bootstrap=bootstrap)


class TestFinancialReporter:
    def test_generate_monthly_report(self, reporter, ledger):
        """Monthly report includes income statement and balance sheet."""
        # Record some data
        ledger.record_revenue(
            amount=500.0, revenue_account="4000", cash_account="1010",
            description="Service revenue", source="test",
        )
        ledger.record_expense(
            amount=50.0, expense_account="5200", cash_account="1010",
            description="Platform fee", source="test",
        )

        report = reporter.generate_monthly_report()
        assert report["period"]
        assert report["income_statement"]["total_revenue"] == 500.0
        assert report["income_statement"]["total_expenses"] == 50.0
        assert report["income_statement"]["net_income"] == 450.0
        assert report["balance_sheet"]["balanced"]
        assert report["integrity"]["balanced"]
        assert "bootstrap" in report

    def test_format_telegram_report(self, reporter, ledger):
        """Telegram formatting produces readable markdown."""
        ledger.record_revenue(
            amount=200.0, revenue_account="4100", cash_account="1020",
            description="Product sale", source="test",
        )

        report = reporter.generate_monthly_report()
        msg = reporter.format_telegram_report(report)

        assert "*Financial Report" in msg
        assert "*P&L (Income Statement)*" in msg
        assert "*Balance Sheet*" in msg
        assert "```" in msg
        assert "200" in msg

    def test_format_telegram_report_no_data(self, reporter):
        """Report with no data still formats correctly."""
        report = reporter.generate_monthly_report()
        msg = reporter.format_telegram_report(report)
        assert "*Financial Report" in msg
        assert "0.00" in msg

    def test_format_telegram_report_shows_bootstrap(self, reporter, config):
        """Bootstrap section shown when not self-sustaining."""
        report = reporter.generate_monthly_report()
        msg = reporter.format_telegram_report(report)
        assert "*Bootstrap Funding*" in msg

    def test_generate_daily_snapshot(self, reporter, ledger):
        """Daily snapshot shows today's and MTD figures."""
        ledger.record_revenue(
            amount=100.0, revenue_account="4000", cash_account="1010",
            description="Today's sale", source="test",
        )

        snapshot = reporter.generate_daily_snapshot()
        assert "*Daily Snapshot" in snapshot
        assert "```" in snapshot
        assert "100" in snapshot

    def test_should_send_monthly_report(self, reporter):
        """Monthly report flag based on day of month."""
        result = reporter.should_send_monthly_report()
        expected = datetime.now().day == 1
        assert result == expected

    def test_should_send_weekly_report(self, reporter):
        """Weekly report flag based on day of week."""
        result = reporter.should_send_weekly_report()
        expected = datetime.now().weekday() == 0
        assert result == expected


class TestStrategyDashboard:
    def _create_strategy(self, db, name, category="digital_products", budget=100):
        return db.execute_insert(
            "INSERT INTO strategies (name, category, allocated_budget, status) "
            "VALUES (?, ?, ?, 'active')",
            (name, category, budget),
        )

    def _record_tx(self, db, strategy_id, amount, tx_type="revenue"):
        db.execute_insert(
            "INSERT INTO transactions (strategy_id, amount, type, category, description) "
            "VALUES (?, ?, ?, ?, 'test tx')",
            (strategy_id, amount, tx_type, tx_type),
        )

    def test_generate_strategy_dashboard_empty(self, reporter):
        """Dashboard with no strategies."""
        dashboard = reporter.generate_strategy_dashboard()
        assert "No active strategies" in dashboard

    def test_generate_strategy_dashboard_with_data(self, reporter, db):
        """Dashboard with strategies shows table."""
        sid = self._create_strategy(db, "ebook_launch")
        self._record_tx(db, sid, 500.0, "revenue")
        self._record_tx(db, sid, 100.0, "expense")

        dashboard = reporter.generate_strategy_dashboard()
        assert "*Strategy Performance Dashboard*" in dashboard
        assert "ebook_launch" in dashboard
        assert "TOTAL" in dashboard
        assert "ROI" in dashboard

    def test_get_strategy_performance(self, reporter, db):
        """Performance analysis returns actionable data."""
        s1 = self._create_strategy(db, "winner", budget=200)
        self._record_tx(db, s1, 500.0, "revenue")
        self._record_tx(db, s1, 100.0, "expense")

        s2 = self._create_strategy(db, "loser", budget=200)
        self._record_tx(db, s2, 10.0, "revenue")
        self._record_tx(db, s2, 150.0, "expense")

        perf = reporter.get_strategy_performance()
        assert perf["total_revenue"] == 510.0
        assert perf["total_expenses"] == 250.0
        assert perf["total_net"] == 260.0
        assert perf["overall_roi_pct"] > 0
        assert len(perf["strategies"]) == 2

        # Winner should be first (sorted by net desc)
        assert perf["strategies"][0]["name"] == "winner"
        assert perf["strategies"][0]["roi_pct"] > 0
        assert perf["strategies"][1]["name"] == "loser"
        assert perf["strategies"][1]["net"] < 0

    def test_strategy_performance_recommendations(self, reporter, db):
        """Recommendations generated based on performance."""
        # Strategy with negative 7d and 30d and budget > 50% used → pause
        s1 = self._create_strategy(db, "bleeding", budget=100)
        self._record_tx(db, s1, 5.0, "revenue")
        self._record_tx(db, s1, 80.0, "expense")

        perf = reporter.get_strategy_performance()
        strat = perf["strategies"][0]
        # With net < 0, it should be at least "review"
        assert strat["recommendation"] in ("review", "pause")

    def test_strategy_performance_empty(self, reporter):
        """Performance analysis with no strategies."""
        perf = reporter.get_strategy_performance()
        assert perf["strategies"] == []
        assert perf["total_revenue"] == 0
        assert perf["overall_roi_pct"] == 0


class TestBootstrapGLIntegration:
    def test_contribution_creates_gl_entry(self, config, db):
        """Crowdfunding contribution creates a GL entry."""
        ledger = GeneralLedger(db)
        wallet = BootstrapWallet(config, db, ledger=ledger)

        campaign_id = wallet.create_campaign(
            platform="kofi", title="Test", description="Test", goal_amount=500,
        )
        wallet.record_contribution(campaign_id, 50.0, "TestBacker")

        entries = ledger.get_journal_entries()
        assert len(entries) == 1
        assert entries[0]["source"] == "bootstrap_crowdfunding"

        # Cash (1060 Ko-fi) debited, Revenue (4400 crowdfunding) credited
        lines = entries[0]["lines"]
        cash_line = [l for l in lines if l["account_code"] == "1060"][0]
        assert cash_line["debit"] == 50.0
        rev_line = [l for l in lines if l["account_code"] == "4400"][0]
        assert rev_line["credit"] == 50.0

        assert ledger.verify_integrity()["balanced"]

    def test_creator_seed_creates_equity_entry(self, config, db):
        """Creator seed donation creates equity GL entry (not revenue)."""
        ledger = GeneralLedger(db)
        wallet = BootstrapWallet(config, db, ledger=ledger)

        campaign_id = wallet.create_campaign(
            platform="kofi", title="Test", description="Test",
        )
        wallet.record_creator_donation(campaign_id, 200.0, alias="Anonymous")

        entries = ledger.get_journal_entries()
        assert len(entries) == 1
        assert entries[0]["source"] == "bootstrap_seed"

        # Cash debited, Equity (3000) credited
        lines = entries[0]["lines"]
        equity_line = [l for l in lines if l["account_code"] == "3000"][0]
        assert equity_line["credit"] == 200.0

        assert ledger.verify_integrity()["balanced"]

    def test_spend_creates_expense_entry(self, config, db):
        """Bootstrap spend creates expense GL entry."""
        ledger = GeneralLedger(db)
        wallet = BootstrapWallet(config, db, ledger=ledger)

        # Fund via crowdfunding first
        campaign_id = wallet.create_campaign(
            platform="kofi", title="Test", description="Test",
        )
        wallet.record_contribution(campaign_id, 100.0, "Backer")

        # Spend from crowdfunding
        wallet.spend_crowdfunding(20.0, "Domain purchase", "domain", "namecheap.com")

        entries = ledger.get_journal_entries()
        assert len(entries) == 2  # contribution + spend

        spend_entry = [e for e in entries if e["source"] == "bootstrap"][0]
        # Domain expense (5300) debited, Cash (1000) credited
        exp_line = [l for l in spend_entry["lines"] if l["account_code"] == "5300"][0]
        assert exp_line["debit"] == 20.0

        assert ledger.verify_integrity()["balanced"]

    def test_no_ledger_still_works(self, config, db):
        """Bootstrap works without ledger (backward compat)."""
        wallet = BootstrapWallet(config, db)  # No ledger
        campaign_id = wallet.create_campaign(
            platform="kofi", title="Test", description="Test",
        )
        wallet.record_contribution(campaign_id, 50.0, "Backer")
        # Should not raise
        assert wallet.get_crowdfunding_total_raised() == 50.0


class TestAutoPause:
    """Test auto-pause of underperforming strategies via lifecycle integration."""

    def _create_strategy(self, db, name, budget=100):
        return db.execute_insert(
            "INSERT INTO strategies (name, category, allocated_budget, status) "
            "VALUES (?, 'digital_products', ?, 'active')",
            (name, budget),
        )

    def _record_tx(self, db, strategy_id, amount, tx_type="revenue"):
        db.execute_insert(
            "INSERT INTO transactions (strategy_id, amount, type, category, description) "
            "VALUES (?, ?, ?, ?, 'test')",
            (strategy_id, amount, tx_type, tx_type),
        )

    def test_pause_recommendation_triggers_lifecycle_pause(self, db, config):
        """Strategy recommended for pause gets paused via lifecycle."""
        from monai.business.strategy_lifecycle import StrategyLifecycle

        ledger = GeneralLedger(db)
        finance = Finance(db)
        reporter = FinancialReporter(db, ledger, finance)
        lifecycle = StrategyLifecycle(db)

        # Create a badly performing strategy: net < 0, budget > 50% used
        sid = self._create_strategy(db, "money_pit", budget=100)
        self._record_tx(db, sid, 5.0, "revenue")
        self._record_tx(db, sid, 80.0, "expense")

        perf = reporter.get_strategy_performance()
        to_pause = perf["strategies_to_pause"]
        assert len(to_pause) >= 1

        # Simulate orchestrator auto-pause
        for s in to_pause:
            if lifecycle.can_transition(s["id"], "paused"):
                lifecycle.pause(s["id"], reason=f"Auto: net={s['net']:.2f}")

        # Verify strategy is now paused
        assert lifecycle.get_status(sid) == "paused"
        assert not lifecycle.is_runnable(sid)

    def test_profitable_strategy_not_paused(self, db, config):
        """Profitable strategy should not be recommended for pause."""
        ledger = GeneralLedger(db)
        finance = Finance(db)
        reporter = FinancialReporter(db, ledger, finance)

        sid = self._create_strategy(db, "winner", budget=200)
        self._record_tx(db, sid, 500.0, "revenue")
        self._record_tx(db, sid, 100.0, "expense")

        perf = reporter.get_strategy_performance()
        pause_ids = [s["id"] for s in perf["strategies_to_pause"]]
        assert sid not in pause_ids

    def test_already_paused_strategy_skipped(self, db, config):
        """Already-paused strategy can't be paused again."""
        from monai.business.strategy_lifecycle import StrategyLifecycle

        lifecycle = StrategyLifecycle(db)

        sid = self._create_strategy(db, "already_paused", budget=100)
        lifecycle.pause(sid, reason="manual")

        # can_transition should return False for paused→paused
        assert not lifecycle.can_transition(sid, "paused")
