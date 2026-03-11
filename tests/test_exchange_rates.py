"""Tests for multi-currency exchange rate service and GL integration."""

from __future__ import annotations

import pytest

from monai.business.exchange_rates import ExchangeRateService, normalize_to_eur
from monai.business.finance import GeneralLedger


@pytest.fixture
def rates(db):
    return ExchangeRateService(db)


@pytest.fixture
def ledger(db):
    return GeneralLedger(db)


class TestExchangeRateService:
    def test_same_currency_returns_one(self, rates):
        """Same currency always returns rate of 1.0."""
        assert rates.get_rate("EUR", "EUR") == 1.0
        assert rates.get_rate("USD", "USD") == 1.0
        assert rates.get_rate("BTC", "BTC") == 1.0

    def test_set_and_get_rate(self, rates):
        """Manually set rates are retrievable."""
        rates.set_rate("EUR", "USD", 1.10, source="test")
        assert rates.get_rate("EUR", "USD") == 1.10

    def test_inverse_rate_cached(self, rates):
        """Setting EUR/USD also makes USD/EUR available."""
        rates.set_rate("EUR", "USD", 1.10, source="test")
        inverse = rates.get_rate("USD", "EUR")
        assert abs(inverse - (1.0 / 1.10)) < 0.0001

    def test_rate_persisted_to_db(self, rates, db):
        """Rates are stored in the database."""
        rates.set_rate("EUR", "USD", 1.12, source="api")
        rows = db.execute(
            "SELECT * FROM exchange_rates WHERE base_currency = 'EUR' "
            "AND quote_currency = 'USD'"
        )
        assert len(rows) == 1
        assert rows[0]["rate"] == 1.12
        assert rows[0]["source"] == "api"

    def test_fallback_rates(self, rates):
        """Fallback rates returned when no cached/DB rate exists."""
        # EUR/USD fallback should be around 1.08
        rate = rates.get_rate("EUR", "USD")
        assert 0.5 < rate < 2.0  # Sanity check

    def test_unknown_pair_returns_zero(self, rates):
        """Unknown currency pair returns 0."""
        assert rates.get_rate("ZZZ", "YYY") == 0.0

    def test_convert_same_currency(self, rates):
        """Convert same currency returns same amount."""
        assert rates.convert(100.0, "EUR", "EUR") == 100.0

    def test_convert_eur_to_usd(self, rates):
        """Convert EUR to USD uses rate."""
        rates.set_rate("EUR", "USD", 1.10)
        result = rates.convert(100.0, "EUR", "USD")
        assert result == 110.0

    def test_convert_usd_to_eur(self, rates):
        """Convert USD to EUR uses inverse."""
        rates.set_rate("EUR", "USD", 1.10)
        result = rates.convert(110.0, "USD", "EUR")
        assert abs(result - 100.0) < 0.01

    def test_convert_btc_to_eur(self, rates):
        """Convert BTC to EUR."""
        rates.set_rate("BTC", "EUR", 85000.0)
        result = rates.convert(0.5, "BTC", "EUR")
        assert result == 42500.0

    def test_convert_xmr_to_eur(self, rates):
        """Convert XMR to EUR."""
        rates.set_rate("XMR", "EUR", 220.0)
        result = rates.convert(10.0, "XMR", "EUR")
        assert result == 2200.0

    def test_case_insensitive(self, rates):
        """Currency codes are case insensitive."""
        rates.set_rate("eur", "usd", 1.10)
        assert rates.get_rate("EUR", "USD") == 1.10
        assert rates.convert(100.0, "eur", "USD") == 110.0

    def test_rate_history(self, rates):
        """Rate history tracks multiple entries."""
        rates.set_rate("EUR", "USD", 1.08, source="day1")
        rates.set_rate("EUR", "USD", 1.10, source="day2")
        rates.set_rate("EUR", "USD", 1.12, source="day3")

        history = rates.get_rate_history("EUR", "USD")
        assert len(history) == 3
        # All rates recorded
        recorded_rates = {h["rate"] for h in history}
        assert recorded_rates == {1.08, 1.10, 1.12}

    def test_get_all_latest_rates(self, rates):
        """Get latest rate for each pair."""
        rates.set_rate("EUR", "USD", 1.08)
        rates.set_rate("EUR", "USD", 1.10)  # Override
        rates.set_rate("BTC", "EUR", 85000.0)

        latest = rates.get_all_latest_rates()
        assert len(latest) >= 2

    def test_cache_ttl(self, rates):
        """Expired cache falls through to DB."""
        rates.cache_ttl = 0  # Expire immediately
        rates.set_rate("EUR", "USD", 1.10)
        # Should still work via DB fallback
        assert rates.get_rate("EUR", "USD") == 1.10


class TestNormalizeToEur:
    def test_all_eur(self, rates):
        """All EUR amounts just sum directly."""
        amounts = [(100.0, "EUR"), (200.0, "EUR"), (50.0, "EUR")]
        assert normalize_to_eur(amounts, rates) == 350.0

    def test_mixed_currencies(self, rates):
        """Mixed currencies converted to EUR."""
        rates.set_rate("USD", "EUR", 0.90)
        amounts = [(100.0, "EUR"), (100.0, "USD")]
        result = normalize_to_eur(amounts, rates)
        assert result == 190.0  # 100 + 100*0.90

    def test_crypto_conversion(self, rates):
        """Crypto amounts converted to EUR."""
        rates.set_rate("BTC", "EUR", 85000.0)
        rates.set_rate("XMR", "EUR", 220.0)
        amounts = [(0.01, "BTC"), (5.0, "XMR")]
        result = normalize_to_eur(amounts, rates)
        assert result == 1950.0  # 850 + 1100


class TestGLMultiCurrency:
    def test_record_usd_revenue(self, ledger, rates):
        """GL entries can store USD amounts."""
        ledger.record_revenue(
            amount=100.0,
            revenue_account="4000",
            cash_account="1010",
            description="USD sale",
            currency="USD",
        )

        entries = ledger.get_journal_entries()
        assert len(entries) == 1
        lines = entries[0]["lines"]
        assert all(l["currency"] == "USD" for l in lines)

    def test_normalized_income_statement(self, ledger, rates):
        """Normalized income statement converts all to EUR."""
        rates.set_rate("USD", "EUR", 0.90)

        # EUR revenue
        ledger.record_revenue(
            amount=200.0, revenue_account="4000", cash_account="1010",
            description="EUR sale", currency="EUR",
        )
        # USD revenue
        ledger.record_revenue(
            amount=100.0, revenue_account="4000", cash_account="1010",
            description="USD sale", currency="USD",
        )
        # EUR expense
        ledger.record_expense(
            amount=50.0, expense_account="5200", cash_account="1010",
            description="Platform fee", currency="EUR",
        )

        report = ledger.get_income_statement_normalized(rates, "EUR")
        # 200 EUR + 100 USD * 0.90 = 290 EUR revenue
        assert report["total_revenue"] == 290.0
        assert report["total_expenses"] == 50.0
        assert report["net_income"] == 240.0
        assert report["target_currency"] == "EUR"

    def test_normalized_statement_shows_fx_rate(self, ledger, rates):
        """Each line includes original currency and FX rate."""
        rates.set_rate("USD", "EUR", 0.92)

        ledger.record_revenue(
            amount=500.0, revenue_account="4100", cash_account="1020",
            description="Gumroad sale", currency="USD",
        )

        report = ledger.get_income_statement_normalized(rates, "EUR")
        usd_line = [r for r in report["revenue"] if r.get("original_currency") == "USD"]
        assert len(usd_line) == 1
        assert usd_line[0]["fx_rate"] == 0.92
        assert usd_line[0]["balance"] == 460.0  # 500 * 0.92

    def test_btc_revenue_normalized(self, ledger, rates):
        """BTC revenue normalizes to EUR correctly."""
        rates.set_rate("BTC", "EUR", 85000.0)

        ledger.record_revenue(
            amount=0.01, revenue_account="4000", cash_account="1040",
            description="BTC payment", currency="BTC",
        )

        report = ledger.get_income_statement_normalized(rates, "EUR")
        assert report["total_revenue"] == 850.0  # 0.01 BTC * 85000
