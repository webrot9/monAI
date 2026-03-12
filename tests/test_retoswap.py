"""Tests for RetoSwap/Haveno P2P exchange integration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.payments.retoswap_provider import (
    RetoSwapClient,
    RetoSwapError,
    TradeOffer,
    TradeResult,
    TradeStatus,
    PaymentMethod,
)


@pytest.fixture
def client():
    return RetoSwapClient(
        daemon_host="127.0.0.1",
        daemon_port=9999,
        daemon_password="test_pass",
        preferred_payment_method="SEPA",
        preferred_currency="EUR",
        price_margin_pct=-1.0,
    )


class TestRetoSwapClient:
    def test_init_defaults(self):
        c = RetoSwapClient()
        assert c.daemon_host == "127.0.0.1"
        assert c.daemon_port == 9999
        assert c.preferred_payment_method == "SEPA"
        assert c.preferred_currency == "EUR"
        assert c.price_margin_pct == -1.0

    def test_min_trade_amount(self, client):
        assert client.MIN_TRADE_XMR == 0.01

    def test_max_price_deviation(self, client):
        assert client.MAX_PRICE_DEVIATION_PCT == 10.0

    @pytest.mark.asyncio
    async def test_auto_sell_below_minimum(self, client):
        result = await client.auto_sell_xmr(amount_xmr=0.001)
        assert result.status == TradeStatus.FAILED
        assert "below minimum" in result.error

    @pytest.mark.asyncio
    async def test_auto_sell_no_market_price(self, client):
        client.get_market_price = AsyncMock(return_value=0.0)
        result = await client.auto_sell_xmr(amount_xmr=1.0)
        assert result.status == TradeStatus.FAILED
        assert "market price" in result.error

    @pytest.mark.asyncio
    async def test_auto_sell_takes_best_offer(self, client):
        client.get_market_price = AsyncMock(return_value=150.0)
        client.get_offers = AsyncMock(return_value=[
            {
                "id": "offer_123",
                "payment_method_id": "SEPA",
                "amount": int(2.0 * 1e12),
                "min_amount": int(0.1 * 1e12),
                "price": 149.0,
            },
            {
                "id": "offer_456",
                "payment_method_id": "SEPA",
                "amount": int(5.0 * 1e12),
                "min_amount": int(0.5 * 1e12),
                "price": 151.0,  # Better price
            },
        ])
        client.take_buy_offer = AsyncMock(return_value=TradeResult(
            trade_id="trade_789",
            status=TradeStatus.OFFER_TAKEN,
            amount_xmr=1.0,
        ))

        result = await client.auto_sell_xmr(amount_xmr=1.0)
        assert result.trade_id == "trade_789"
        assert result.status == TradeStatus.OFFER_TAKEN
        # Should take the higher-priced offer
        client.take_buy_offer.assert_called_once()
        call_args = client.take_buy_offer.call_args
        assert call_args.kwargs["offer_id"] == "offer_456"

    @pytest.mark.asyncio
    async def test_auto_sell_filters_wrong_payment_method(self, client):
        client.get_market_price = AsyncMock(return_value=150.0)
        client.get_offers = AsyncMock(return_value=[
            {
                "id": "offer_paypal",
                "payment_method_id": "PAYPAL",  # We want SEPA
                "amount": int(5.0 * 1e12),
                "min_amount": int(0.1 * 1e12),
                "price": 160.0,
            },
        ])
        client.create_sell_offer = AsyncMock(return_value=TradeOffer(
            offer_id="my_offer_1",
            price_eur=148.5,
        ))

        result = await client.auto_sell_xmr(amount_xmr=1.0)
        # Should fall through to posting own offer since no SEPA offers
        assert result.status == TradeStatus.OFFER_POSTED

    @pytest.mark.asyncio
    async def test_auto_sell_rejects_bad_price(self, client):
        client.get_market_price = AsyncMock(return_value=150.0)
        client.get_offers = AsyncMock(return_value=[
            {
                "id": "offer_lowball",
                "payment_method_id": "SEPA",
                "amount": int(5.0 * 1e12),
                "min_amount": int(0.1 * 1e12),
                "price": 100.0,  # 33% below market — too much deviation
            },
        ])
        client.create_sell_offer = AsyncMock(return_value=TradeOffer(
            offer_id="my_offer_2",
            price_eur=148.5,
        ))

        result = await client.auto_sell_xmr(amount_xmr=1.0)
        # Should reject the lowball and post own offer
        assert result.status == TradeStatus.OFFER_POSTED

    @pytest.mark.asyncio
    async def test_create_sell_offer_checks_minimum(self, client):
        result = await client.create_sell_offer(amount_xmr=0.001)
        assert result.offer_id == ""  # Empty = failed

    @pytest.mark.asyncio
    async def test_create_sell_offer_checks_market_price(self, client):
        client.get_market_price = AsyncMock(return_value=0.0)
        result = await client.create_sell_offer(amount_xmr=1.0)
        assert result.offer_id == ""


class TestTradeTypes:
    def test_trade_status_values(self):
        assert TradeStatus.COMPLETED == "completed"
        assert TradeStatus.OFFER_POSTED == "offer_posted"
        assert TradeStatus.DISPUTED == "disputed"

    def test_payment_methods(self):
        assert PaymentMethod.SEPA == "SEPA"
        assert PaymentMethod.CASH_BY_MAIL == "CASH_BY_MAIL"
        assert PaymentMethod.REVOLUT == "REVOLUT"

    def test_trade_offer_defaults(self):
        offer = TradeOffer()
        assert offer.direction == "SELL"
        assert offer.currency == "EUR"

    def test_trade_result_defaults(self):
        result = TradeResult()
        assert result.status == TradeStatus.FAILED
        assert result.amount_xmr == 0.0


class TestSweepEngineRetoSwapFlow:
    @pytest.mark.asyncio
    async def test_sweep_engine_selects_retoswap_flow(self):
        from monai.config import Config, RetoSwapConfig
        config = Config()
        config.retoswap = RetoSwapConfig(enabled=True)

        from unittest.mock import PropertyMock
        from monai.payments.sweep_engine import SweepEngine
        from monai.db.database import Database

        db = Database()
        engine = SweepEngine(config, db)

        # Mock corporate to return no LLC entities
        with patch.object(type(engine), 'corporate', new_callable=PropertyMock) as mock_corp:
            mock_corp_inst = MagicMock()
            mock_corp_inst.get_all_entities.return_value = []
            mock_corp.return_value = mock_corp_inst

            flow = engine.get_active_flow()
            assert flow == "crypto_retoswap"
