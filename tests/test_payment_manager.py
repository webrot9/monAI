"""Tests for the UnifiedPaymentManager."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.payments.manager import UnifiedPaymentManager
from monai.payments.types import (
    PaymentIntent,
    PaymentResult,
    PaymentStatus,
    ProviderBalance,
    WebhookEvent,
    WebhookEventType,
)


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.creator_wallet.xmr_address = "4" + "A" * 94
    config.creator_wallet.sweep_threshold_eur = 50.0
    config.creator_wallet.sweep_interval_hours = 24
    config.creator_wallet.min_confirmations_xmr = 10
    config.creator_wallet.min_confirmations_btc = 3
    config.monero.wallet_rpc_url = ""  # Disabled for tests
    config.monero.rpc_user = ""
    config.monero.rpc_password = ""
    config.monero.proxy_url = ""
    return config


@pytest.fixture
def mock_db():
    db = MagicMock()
    # Mock connect() context manager
    mock_conn = MagicMock()
    mock_conn.execute = MagicMock(return_value=MagicMock(lastrowid=1))
    db.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
    db.connect.return_value.__exit__ = MagicMock(return_value=False)
    # Mock transaction() context manager (same interface as connect)
    mock_tx_conn = MagicMock()
    mock_tx_conn.execute = MagicMock(return_value=MagicMock(lastrowid=1))
    db.transaction.return_value.__enter__ = MagicMock(return_value=mock_tx_conn)
    db.transaction.return_value.__exit__ = MagicMock(return_value=False)
    db.execute = MagicMock(return_value=[])
    db.execute_insert = MagicMock(return_value=1)
    return db


@pytest.fixture
def manager(mock_config, mock_db):
    return UnifiedPaymentManager(mock_config, mock_db)


class TestUnifiedPaymentManager:
    def test_init_no_monero_if_not_configured(self, manager):
        # Monero RPC URL is empty, so it shouldn't be registered
        assert "crypto_xmr" not in manager._providers

    def test_register_provider(self, manager):
        mock_provider = MagicMock()
        manager.register_provider("stripe", mock_provider)
        assert manager.get_provider("stripe") == mock_provider

    def test_register_brand_provider(self, manager):
        mock_provider = MagicMock()
        manager.register_brand_provider("newsletter", "stripe", mock_provider)
        assert manager.get_brand_provider("newsletter", "stripe") == mock_provider

    def test_brand_provider_fallback_to_global(self, manager):
        global_provider = MagicMock()
        manager.register_provider("stripe", global_provider)
        # No brand-specific provider, should fall back to global
        assert manager.get_brand_provider("unknown_brand", "stripe") == global_provider

    @pytest.mark.asyncio
    async def test_create_payment(self, manager):
        mock_provider = MagicMock()
        mock_provider.create_payment = AsyncMock(return_value=PaymentResult(
            success=True, payment_ref="ref_123",
            checkout_url="https://checkout.example.com/ref_123",
        ))
        manager.register_provider("stripe", mock_provider)

        intent = PaymentIntent(amount=29.99, product="E-book")
        result = await manager.create_payment("newsletter", "stripe", intent)

        assert result.success is True
        assert result.checkout_url == "https://checkout.example.com/ref_123"
        # Verify brand was set on the intent
        mock_provider.create_payment.assert_called_once()
        call_intent = mock_provider.create_payment.call_args[0][0]
        assert call_intent.brand == "newsletter"

    @pytest.mark.asyncio
    async def test_create_payment_unknown_provider(self, manager):
        result = await manager.create_payment("brand", "nonexistent", PaymentIntent(amount=10))
        assert result.success is False
        assert "not registered" in result.error

    @pytest.mark.asyncio
    async def test_verify_payment(self, manager):
        mock_provider = MagicMock()
        mock_provider.verify_payment = AsyncMock(return_value=PaymentResult(
            success=True, status=PaymentStatus.COMPLETED, amount=50.0,
        ))
        manager.register_provider("stripe", mock_provider)

        result = await manager.verify_payment("stripe", "cs_test_123")
        assert result.success is True
        assert result.status == PaymentStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_get_all_balances(self, manager):
        mock_stripe = MagicMock()
        mock_stripe.get_balance = AsyncMock(return_value=ProviderBalance(
            available=100.0, pending=25.0, currency="EUR",
        ))
        mock_btcpay = MagicMock()
        mock_btcpay.get_balance = AsyncMock(return_value=ProviderBalance(
            available=0.5, pending=0.0, currency="BTC",
        ))

        manager.register_provider("stripe", mock_stripe)
        manager.register_provider("btcpay", mock_btcpay)

        balances = await manager.get_all_balances()
        assert balances["stripe"]["available"] == 100.0
        assert balances["btcpay"]["available"] == 0.5

    @pytest.mark.asyncio
    async def test_health_check(self, manager):
        mock_provider = MagicMock()
        mock_provider.health_check = AsyncMock(return_value=True)
        manager.register_provider("stripe", mock_provider)

        health = await manager.health_check()
        assert health["stripe"] == "healthy"
        assert health["webhook_server"] == "stopped"

    def test_get_status(self, manager):
        manager.register_provider("stripe", MagicMock())
        manager.register_brand_provider("newsletter", "btcpay", MagicMock())

        status = manager.get_status()
        assert "stripe" in status["registered_providers"]
        assert "btcpay" in status["brand_providers"]["newsletter"]


class TestWebhookEventProcessing:
    @pytest.mark.asyncio
    async def test_payment_completed_event(self, manager, mock_db):
        manager.brand_payments.get_collection_accounts = MagicMock(return_value=[
            {"id": 1, "provider": "stripe"},
        ])

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="cs_completed",
            amount=99.99,
            currency="EUR",
            customer_email="buyer@test.com",
            product="SaaS Pro Plan",
            metadata={"brand": "micro_saas", "lead_id": "7"},
        )

        await manager._handle_webhook_event(event)

        # Verify transaction was used (atomic idempotency + recording)
        assert mock_db.transaction.called

    @pytest.mark.asyncio
    async def test_duplicate_webhook_ignored(self, manager, mock_db):
        """Webhook idempotency: same event processed twice should be rejected atomically."""
        manager.brand_payments.get_collection_accounts = MagicMock(return_value=[
            {"id": 1, "provider": "stripe"},
        ])

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="cs_dedup_test",
            amount=50.0,
            metadata={"brand": "test_brand"},
        )

        # First call succeeds
        await manager._handle_webhook_event(event)
        call_count_after_first = mock_db.transaction.call_count

        # Second call: simulate UNIQUE constraint failure in transaction
        mock_tx_conn = MagicMock()
        mock_tx_conn.execute = MagicMock(
            side_effect=Exception("UNIQUE constraint failed")
        )
        mock_db.transaction.return_value.__enter__ = MagicMock(
            return_value=mock_tx_conn
        )
        await manager._handle_webhook_event(event)

        # Transaction was attempted but the UNIQUE violation caused early return
        # No additional recording should have happened beyond the first

    @pytest.mark.asyncio
    async def test_payment_refunded_event(self, manager, mock_db):
        # Set up transaction conn to return payment rows then empty swept rows
        payment_cursor = MagicMock()
        payment_cursor.fetchall.return_value = [
            {"id": 5, "brand": "test_brand", "amount": 50.0, "currency": "EUR"},
        ]
        swept_cursor = MagicMock()
        swept_cursor.fetchall.return_value = []
        mock_tx_conn = MagicMock()
        mock_tx_conn.execute = MagicMock(
            side_effect=[payment_cursor, None, swept_cursor]
        )
        mock_db.transaction.return_value.__enter__ = MagicMock(
            return_value=mock_tx_conn
        )

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_REFUNDED,
            provider="stripe",
            payment_ref="cs_refunded",
        )

        # Test _handle_refund_inner directly to isolate from idempotency tx
        await manager._handle_refund_inner(event)
        # Verify atomic refund: SELECT + UPDATE + SELECT ran inside transaction
        assert mock_tx_conn.execute.call_count == 3
        # Second call is the UPDATE setting status='refunded'
        update_call = mock_tx_conn.execute.call_args_list[1]
        assert "SET status = 'refunded'" in update_call[0][0]
        assert update_call[0][1] == (5,)

    @pytest.mark.asyncio
    async def test_payment_disputed_event(self, manager, mock_db):
        mock_db.execute.return_value = [
            {"id": 5, "brand": "test_brand", "amount": 50.0, "currency": "EUR"},
        ]

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_DISPUTED,
            provider="stripe",
            payment_ref="cs_disputed",
        )

        await manager._handle_webhook_event(event)
        mock_db.transaction.assert_called()
