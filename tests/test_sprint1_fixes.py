"""Tests for Sprint 1 critical bug fixes.

Covers:
- Amount precision (Decimal rounding before DB storage)
- Webhook amount validation (zero, NaN, negative, edge cases)
- Gumroad amount parsing robustness
- PaymentIntent inf validation
"""

import math
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock

import pytest

from monai.business.brand_payments import BrandPayments
from monai.business.finance import GeneralLedger
from monai.payments.gumroad_provider import GumroadProvider
from monai.payments.manager import UnifiedPaymentManager
from monai.payments.types import PaymentIntent, WebhookEvent, WebhookEventType


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def mock_config():
    config = MagicMock()
    config.creator_wallet.xmr_address = "4" + "A" * 94
    config.creator_wallet.sweep_threshold_eur = 50.0
    config.creator_wallet.sweep_interval_hours = 24
    config.creator_wallet.min_confirmations_xmr = 10
    config.creator_wallet.min_confirmations_btc = 3
    config.monero.wallet_rpc_url = ""
    config.monero.rpc_user = ""
    config.monero.rpc_password = ""
    config.monero.proxy_url = ""
    return config


@pytest.fixture
def ledger(db):
    return GeneralLedger(db)


@pytest.fixture
def manager(mock_config, db, ledger):
    return UnifiedPaymentManager(mock_config, db, ledger=ledger)


@pytest.fixture
def brand_payments(db):
    return BrandPayments(db)


def _ensure_payment_tables(db):
    BrandPayments(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO brand_payment_accounts "
            "(id, brand, provider, account_type, account_id, status) "
            "VALUES (0, '_placeholder', '_none', 'collection', '_placeholder', 'active')"
        )


# ── PaymentIntent Validation ─────────────────────────────────

class TestPaymentIntentValidation:
    def test_rejects_infinity(self):
        with pytest.raises(ValueError, match="infinite"):
            PaymentIntent(amount=float("inf"))

    def test_rejects_negative_infinity(self):
        with pytest.raises(ValueError, match="infinite"):
            PaymentIntent(amount=float("-inf"))

    def test_rejects_nan(self):
        with pytest.raises(ValueError, match="NaN"):
            PaymentIntent(amount=float("nan"))

    def test_rejects_zero(self):
        with pytest.raises(ValueError, match="below minimum"):
            PaymentIntent(amount=0.0)

    def test_rejects_over_max(self):
        with pytest.raises(ValueError, match="exceeds maximum"):
            PaymentIntent(amount=100_001.0)

    def test_accepts_min_amount(self):
        intent = PaymentIntent(amount=0.01)
        assert intent.amount == 0.01

    def test_accepts_max_amount(self):
        intent = PaymentIntent(amount=100_000.0)
        assert intent.amount == 100_000.0

    def test_amount_cents_precision(self):
        """amount_cents should not suffer from float precision."""
        intent = PaymentIntent(amount=19.99)
        assert intent.amount_cents == 1999


# ── Webhook Amount Validation ─────────────────────────────────

class TestWebhookAmountValidation:
    @pytest.mark.asyncio
    async def test_rejects_zero_amount_completed(self, manager, db):
        """Zero-amount payment.completed should be rejected."""
        _ensure_payment_tables(db)
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_zero_001",
            amount=0.0,
            currency="EUR",
            metadata={"brand": "test"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        # Should not be recorded
        rows = db.execute(
            "SELECT * FROM webhook_events WHERE payment_ref = 'pi_zero_001'"
        )
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_allows_zero_amount_refund(self, manager, db):
        """Zero-amount refund is allowed (partial refund of $0 / free item)."""
        _ensure_payment_tables(db)
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_REFUNDED,
            provider="stripe",
            payment_ref="pi_refund_zero",
            amount=0.0,
            currency="EUR",
            metadata={"brand": "test"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        rows = db.execute(
            "SELECT * FROM webhook_events WHERE payment_ref = 'pi_refund_zero'"
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_rejects_nan_amount(self, manager, db):
        """NaN amounts should be rejected."""
        _ensure_payment_tables(db)
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_nan_001",
            amount=float("nan"),
            currency="EUR",
            metadata={"brand": "test"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        rows = db.execute(
            "SELECT * FROM webhook_events WHERE payment_ref = 'pi_nan_001'"
        )
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_rejects_negative_amount(self, manager, db):
        _ensure_payment_tables(db)
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_neg_002",
            amount=-100.0,
            currency="EUR",
            metadata={"brand": "test"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        rows = db.execute(
            "SELECT * FROM webhook_events WHERE payment_ref = 'pi_neg_002'"
        )
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_rejects_over_1m(self, manager, db):
        _ensure_payment_tables(db)
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_big_002",
            amount=5_000_000.0,
            currency="EUR",
            metadata={"brand": "test"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        rows = db.execute(
            "SELECT * FROM webhook_events WHERE payment_ref = 'pi_big_002'"
        )
        assert len(rows) == 0


# ── Amount Precision ──────────────────────────────────────────

class TestAmountPrecision:
    @pytest.mark.asyncio
    async def test_payment_amount_rounded_in_db(self, manager, db, ledger):
        """Amounts with floating-point noise should be stored rounded."""
        _ensure_payment_tables(db)

        # 0.1 + 0.2 in IEEE 754 = 0.30000000000000004
        amount = 0.1 + 0.2
        assert amount != 0.3  # Proves the float imprecision

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="gumroad",
            payment_ref="gum_precision_001",
            amount=amount,
            currency="EUR",
            product="Test",
            metadata={"brand": "precision_brand"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        rows = db.execute(
            "SELECT amount FROM brand_payments_received "
            "WHERE payment_ref = 'gum_precision_001'"
        )
        assert len(rows) == 1
        stored_amount = rows[0]["amount"]
        assert stored_amount == 0.3  # Should be rounded to 0.30

    @pytest.mark.asyncio
    async def test_fee_amount_rounded_in_db(self, manager, db, ledger):
        """Platform fee should be rounded to 2 decimals."""
        _ensure_payment_tables(db)

        # Amount that produces non-round fee: 33.33 * 2.9% + 0.30
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_fee_precision",
            amount=33.33,
            currency="EUR",
            product="Test",
            metadata={"brand": "fee_brand"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        rows = db.execute(
            "SELECT fee_amount FROM platform_fees "
            "WHERE payment_id = (SELECT id FROM brand_payments_received "
            "WHERE payment_ref = 'pi_fee_precision')"
        )
        assert len(rows) == 1
        fee = rows[0]["fee_amount"]
        # Fee should be rounded: 33.33 * 0.029 + 0.30 = 1.26657 → 1.27
        assert fee == 1.27

    def test_brand_payments_record_rounds(self, brand_payments):
        """BrandPayments.record_payment rounds amount."""
        # Create placeholder account for FK
        with brand_payments.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO brand_payment_accounts "
                "(id, brand, provider, account_type, account_id, status) "
                "VALUES (0, '_placeholder', '_none', 'collection', '_placeholder', 'active')"
            )
        pay_id = brand_payments.record_payment(
            brand="test", account_id=0,
            amount=19.999999999999998,  # float noise
            product="Test", currency="EUR",
        )
        rows = brand_payments.db.execute(
            "SELECT amount FROM brand_payments_received WHERE id = ?",
            (pay_id,),
        )
        assert rows[0]["amount"] == 20.0


# ── Gumroad Amount Parsing ────────────────────────────────────

class TestGumroadAmountParsing:
    @pytest.fixture
    def gumroad(self):
        return GumroadProvider(
            access_token="test_token",
            webhook_secret="test_secret",
        )

    @pytest.mark.asyncio
    async def test_normal_cents_amount(self, gumroad):
        """Standard Gumroad price in cents."""
        import hashlib, hmac
        from urllib.parse import urlencode
        payload_data = {
            "seller_id": "seller1",
            "product_id": "prod1",
            "product_name": "Ebook",
            "price": "500",
            "currency": "usd",
            "email": "buyer@test.com",
            "sale_id": "sale_001",
            "resource_name": "sale",
        }
        payload = urlencode(payload_data).encode()
        sig = hmac.new(b"test_secret", payload, hashlib.sha256).hexdigest()

        event = await gumroad.handle_webhook(
            payload, {"x-gumroad-signature": sig}
        )
        assert event is not None
        assert event.amount == 5.0  # 500 cents = $5.00

    @pytest.mark.asyncio
    async def test_zero_price(self, gumroad):
        """Zero price (free product) should still parse."""
        import hashlib, hmac
        from urllib.parse import urlencode
        payload_data = {
            "seller_id": "seller1",
            "product_id": "prod1",
            "product_name": "Free Ebook",
            "price": "0",
            "currency": "usd",
            "email": "buyer@test.com",
            "sale_id": "sale_free_001",
            "resource_name": "sale",
        }
        payload = urlencode(payload_data).encode()
        sig = hmac.new(b"test_secret", payload, hashlib.sha256).hexdigest()

        event = await gumroad.handle_webhook(
            payload, {"x-gumroad-signature": sig}
        )
        assert event is not None
        assert event.amount == 0.0

    @pytest.mark.asyncio
    async def test_rejects_without_signature(self, gumroad):
        """Must reject webhooks without signature."""
        from urllib.parse import urlencode
        payload = urlencode({"sale_id": "test", "price": "500"}).encode()
        event = await gumroad.handle_webhook(payload, {})
        assert event is None

    @pytest.mark.asyncio
    async def test_rejects_without_secret(self):
        """Must reject all webhooks when secret not configured."""
        provider = GumroadProvider(access_token="test", webhook_secret="")
        from urllib.parse import urlencode
        payload = urlencode({"sale_id": "test", "price": "500"}).encode()
        event = await provider.handle_webhook(payload, {})
        assert event is None
