"""Tests for multi-currency exchange rate service and GL integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from monai.business.exchange_rates import ExchangeRateService, RateLimiter, normalize_to_eur
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


class TestLiveRateFetching:
    """Test ECB and CoinGecko API integration (mocked)."""

    @pytest.mark.asyncio
    async def test_fetch_ecb_rates(self, rates):
        """ECB CSV response parsed correctly."""
        csv_response = (
            "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,"
            "TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
            "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-03-11,1.0850,A"
        )
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.text = csv_response
        mock_resp.raise_for_status = lambda: None

        with patch("monai.business.exchange_rates.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)

            result = await rates._fetch_ecb_rates()

        assert ("EUR", "USD") in result
        assert result[("EUR", "USD")] == 1.085
        assert ("USD", "EUR") in result
        assert abs(result[("USD", "EUR")] - (1.0 / 1.085)) < 0.001

    @pytest.mark.asyncio
    async def test_fetch_coingecko_rates(self, rates):
        """CoinGecko JSON response parsed correctly."""
        json_response = {
            "bitcoin": {"eur": 84500.0, "usd": 91000.0},
            "monero": {"eur": 218.0, "usd": 235.0},
        }
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.json = lambda: json_response
        mock_resp.raise_for_status = lambda: None

        with patch("monai.business.exchange_rates.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)

            result = await rates._fetch_coingecko_rates()

        assert result[("BTC", "EUR")] == 84500.0
        assert result[("BTC", "USD")] == 91000.0
        assert result[("XMR", "EUR")] == 218.0
        assert result[("XMR", "USD")] == 235.0
        # Cross rate
        assert ("BTC", "XMR") in result
        assert abs(result[("BTC", "XMR")] - (84500.0 / 218.0)) < 1.0

    @pytest.mark.asyncio
    async def test_fetch_live_rates_stores_in_db(self, rates):
        """fetch_live_rates persists rates to DB."""
        ecb_csv = (
            "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,"
            "TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
            "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-03-11,1.09,A"
        )
        coingecko_json = {
            "bitcoin": {"eur": 85000.0, "usd": 92000.0},
            "monero": {"eur": 220.0, "usd": 238.0},
        }

        ecb_resp = AsyncMock()
        ecb_resp.text = ecb_csv
        ecb_resp.raise_for_status = lambda: None

        cg_resp = AsyncMock()
        cg_resp.json = lambda: coingecko_json
        cg_resp.raise_for_status = lambda: None

        async def mock_get(url, **kwargs):
            if "ecb" in url:
                return ecb_resp
            return cg_resp

        with patch("monai.business.exchange_rates.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = mock_get

            fetched = await rates.fetch_live_rates()

        # Should have stored EUR/USD + USD/EUR + crypto pairs
        assert len(fetched) > 0
        assert "EUR/USD" in fetched

        # Verify DB persistence
        history = rates.get_rate_history("EUR", "USD")
        assert len(history) >= 1
        assert any(h["source"] == "ecb" for h in history)

    @pytest.mark.asyncio
    async def test_ecb_failure_graceful(self, rates):
        """ECB API failure doesn't crash — returns empty."""
        with patch("monai.business.exchange_rates.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(side_effect=httpx.ConnectError("offline"))

            result = await rates._fetch_ecb_rates()

        assert result == {}

    @pytest.mark.asyncio
    async def test_coingecko_failure_graceful(self, rates):
        """CoinGecko API failure doesn't crash — returns empty."""
        with patch("monai.business.exchange_rates.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(side_effect=httpx.ConnectError("offline"))

            result = await rates._fetch_coingecko_rates()

        assert result == {}

    @pytest.mark.asyncio
    async def test_full_fetch_with_partial_failure(self, rates):
        """If one source fails, the other still works."""
        coingecko_json = {
            "bitcoin": {"eur": 85000.0, "usd": 92000.0},
            "monero": {"eur": 220.0, "usd": 238.0},
        }
        cg_resp = AsyncMock()
        cg_resp.json = lambda: coingecko_json
        cg_resp.raise_for_status = lambda: None

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "ecb" in url:
                raise httpx.ConnectError("ECB down")
            return cg_resp

        with patch("monai.business.exchange_rates.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = mock_get

            fetched = await rates.fetch_live_rates()

        # ECB failed but CoinGecko succeeded
        assert "BTC/EUR" in fetched
        assert "EUR/USD" not in fetched


class TestRateLimiter:
    """Tests for the token-bucket rate limiter."""

    def test_initial_state_allows_calls(self):
        """Fresh limiter allows calls."""
        limiter = RateLimiter({"test": (5, 60)})
        assert limiter.can_call("test") is True

    def test_respects_max_calls(self):
        """Blocks after max calls within window."""
        limiter = RateLimiter({"test": (3, 60)})
        for _ in range(3):
            assert limiter.can_call("test") is True
            limiter.record_call("test")
        assert limiter.can_call("test") is False

    def test_unknown_provider_always_allowed(self):
        """Providers not in limits are always allowed."""
        limiter = RateLimiter({"ecb": (5, 60)})
        assert limiter.can_call("unknown_api") is True

    def test_case_insensitive(self):
        """Provider names are case insensitive."""
        limiter = RateLimiter({"ecb": (2, 60)})
        limiter.record_call("ECB")
        limiter.record_call("Ecb")
        assert limiter.can_call("ecb") is False

    def test_time_until_available_zero_when_allowed(self):
        """Returns 0 when calls are available."""
        limiter = RateLimiter({"test": (5, 60)})
        assert limiter.time_until_available("test") == 0.0

    def test_time_until_available_positive_when_blocked(self):
        """Returns positive seconds when rate limited."""
        limiter = RateLimiter({"test": (1, 60)})
        limiter.record_call("test")
        wait = limiter.time_until_available("test")
        assert 0 < wait <= 60

    def test_window_expiry(self):
        """Calls expire after the window passes."""
        import time as _time
        limiter = RateLimiter({"test": (1, 1)})  # 1 call per 1 second
        limiter.record_call("test")
        assert limiter.can_call("test") is False
        _time.sleep(1.1)
        assert limiter.can_call("test") is True

    def test_default_limits(self):
        """Default limits for ECB and CoinGecko are set."""
        limiter = RateLimiter()
        assert limiter.can_call("ecb") is True
        assert limiter.can_call("coingecko") is True

    @pytest.mark.asyncio
    async def test_rate_limited_fetch_skips_provider(self, db):
        """When rate limited, fetch_live_rates skips that provider."""
        limiter = RateLimiter({"ecb": (0, 60), "coingecko": (0, 60)})
        rates = ExchangeRateService(db, rate_limiter=limiter)
        fetched = await rates.fetch_live_rates()
        # Both providers blocked → nothing fetched
        assert len(fetched) == 0
