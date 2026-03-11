"""Tests for multi-brand GL segmentation (per-brand P&L)."""

from __future__ import annotations

import pytest

from monai.business.finance import GeneralLedger


@pytest.fixture
def ledger(db):
    return GeneralLedger(db)


class TestBrandSegmentation:
    def _record_branded_revenue(self, ledger, brand, amount, account="4000"):
        ledger.record_revenue(
            amount=amount, revenue_account=account, cash_account="1010",
            description=f"{brand} sale", brand=brand, source="test",
        )

    def _record_branded_expense(self, ledger, brand, amount, account="5200"):
        ledger.record_expense(
            amount=amount, expense_account=account, cash_account="1010",
            description=f"{brand} cost", brand=brand, source="test",
        )

    def test_get_brands_empty(self, ledger):
        """No entries → no brands."""
        assert ledger.get_brands() == []

    def test_get_brands_returns_distinct(self, ledger):
        """Returns unique brand names sorted."""
        self._record_branded_revenue(ledger, "zeta_brand", 100)
        self._record_branded_revenue(ledger, "alpha_brand", 200)
        self._record_branded_revenue(ledger, "alpha_brand", 50)
        brands = ledger.get_brands()
        assert brands == ["alpha_brand", "zeta_brand"]

    def test_income_statement_by_brand(self, ledger):
        """Brand-specific P&L only includes that brand's entries."""
        self._record_branded_revenue(ledger, "brand_a", 500)
        self._record_branded_revenue(ledger, "brand_b", 300)
        self._record_branded_expense(ledger, "brand_a", 100)
        self._record_branded_expense(ledger, "brand_b", 200)

        pnl_a = ledger.get_income_statement_by_brand("brand_a")
        assert pnl_a["brand"] == "brand_a"
        assert pnl_a["total_revenue"] == 500.0
        assert pnl_a["total_expenses"] == 100.0
        assert pnl_a["net_income"] == 400.0

        pnl_b = ledger.get_income_statement_by_brand("brand_b")
        assert pnl_b["total_revenue"] == 300.0
        assert pnl_b["total_expenses"] == 200.0
        assert pnl_b["net_income"] == 100.0

    def test_brand_pnl_excludes_other_brands(self, ledger):
        """Brand A's P&L has zero from Brand B."""
        self._record_branded_revenue(ledger, "brand_a", 1000)
        self._record_branded_revenue(ledger, "brand_b", 500)

        pnl_a = ledger.get_income_statement_by_brand("brand_a")
        assert pnl_a["total_revenue"] == 1000.0  # Not 1500

    def test_all_brands_pnl(self, ledger):
        """Multi-brand P&L aggregates all brands plus unbranded."""
        self._record_branded_revenue(ledger, "brand_a", 500)
        self._record_branded_revenue(ledger, "brand_b", 300)
        self._record_branded_expense(ledger, "brand_a", 100)

        # Also record unbranded entry
        ledger.record_revenue(
            amount=200.0, revenue_account="4000", cash_account="1010",
            description="Unbranded sale", source="test",
        )

        result = ledger.get_all_brands_pnl()
        assert len(result["brands"]) == 2

        # Find brand_a
        brand_a = next(b for b in result["brands"] if b["brand"] == "brand_a")
        assert brand_a["total_revenue"] == 500.0
        assert brand_a["net_income"] == 400.0

        # Unbranded captures the rest
        assert result["unbranded"]["total_revenue"] == 200.0

        # Consolidated matches overall
        assert result["consolidated"]["total_revenue"] == 1000.0
        assert result["consolidated"]["net_income"] == 900.0

    def test_all_brands_pnl_no_brands(self, ledger):
        """All-brands P&L with no branded entries."""
        ledger.record_revenue(
            amount=100.0, revenue_account="4000", cash_account="1010",
            description="Sale", source="test",
        )
        result = ledger.get_all_brands_pnl()
        assert result["brands"] == []
        assert result["unbranded"]["total_revenue"] == 100.0
        assert result["consolidated"]["total_revenue"] == 100.0

    def test_format_brand_pnl_telegram(self, ledger):
        """Telegram format includes brand table."""
        self._record_branded_revenue(ledger, "brand_a", 500)
        self._record_branded_expense(ledger, "brand_a", 100)
        self._record_branded_revenue(ledger, "brand_b", 300)

        msg = ledger.format_brand_pnl_telegram()
        assert "Multi-Brand P&L" in msg
        assert "brand_a" in msg
        assert "brand_b" in msg
        assert "TOTAL" in msg
        assert "```" in msg

    def test_brand_integrity_preserved(self, ledger):
        """Branded entries don't break overall ledger integrity."""
        self._record_branded_revenue(ledger, "brand_a", 500)
        self._record_branded_expense(ledger, "brand_a", 100)
        self._record_branded_revenue(ledger, "brand_b", 300)
        ledger.record_revenue(
            amount=200.0, revenue_account="4000", cash_account="1010",
            description="Unbranded", source="test",
        )

        integrity = ledger.verify_integrity()
        assert integrity["balanced"]
        assert integrity["trial_balance_ok"]

    def test_brand_pnl_empty_brand(self, ledger):
        """P&L for non-existent brand returns zeros."""
        pnl = ledger.get_income_statement_by_brand("nonexistent")
        assert pnl["total_revenue"] == 0.0
        assert pnl["total_expenses"] == 0.0
        assert pnl["net_income"] == 0.0
