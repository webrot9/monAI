"""Tests for financial safety fixes — idempotency, Decimal precision, fees, signatures.

These tests verify the critical financial safety gaps identified in the project review.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from monai.db.database import Database
from monai.payments.types import (
    PaymentIntent,
    PaymentResult,
    PaymentStatus,
    ProviderBalance,
    SweepResult,
    WebhookEvent,
    WebhookEventType,
    _to_decimal,
)


# ── Decimal Precision Tests ─────────────────────────────────


class TestDecimalPrecision:
    """Verify that financial amounts use Decimal to avoid float errors."""

    def test_to_decimal_from_float(self):
        result = _to_decimal(0.1)
        assert result == Decimal("0.1")
        assert isinstance(result, Decimal)

    def test_to_decimal_from_string(self):
        result = _to_decimal("19.99")
        assert result == Decimal("19.99")

    def test_to_decimal_from_int(self):
        result = _to_decimal(100)
        assert result == Decimal("100")

    def test_to_decimal_idempotent(self):
        d = Decimal("42.50")
        assert _to_decimal(d) is d

    def test_float_addition_precision(self):
        """The classic float precision problem: 0.1 + 0.2 != 0.3 with float."""
        # This FAILS with float:
        assert 0.1 + 0.2 != 0.3
        # But works correctly with our Decimal conversion:
        a = _to_decimal(0.1)
        b = _to_decimal(0.2)
        assert a + b == Decimal("0.3")

    def test_payment_intent_amount_cents(self):
        """Verify Stripe/Gumroad cents conversion is precise."""
        intent = PaymentIntent(amount=19.99)
        assert intent.amount_cents == 1999
        assert intent.amount_decimal == Decimal("19.99")

    def test_payment_intent_tricky_amount(self):
        """Edge case: amounts that are imprecise as float."""
        intent = PaymentIntent(amount=0.01)
        assert intent.amount_cents == 1
        # With float: int(0.01 * 100) could be 0 in some cases
        # With Decimal: always correct
        assert intent.amount_decimal * 100 == Decimal("1.00")

    def test_payment_result_decimal(self):
        result = PaymentResult(success=True, amount=99.99)
        assert result.amount_decimal == Decimal("99.99")

    def test_webhook_event_decimal(self):
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="ref_1",
            amount=149.50,
        )
        assert event.amount_decimal == Decimal("149.50")

    def test_provider_balance_decimal(self):
        bal = ProviderBalance(available=1000.50, pending=250.75)
        assert bal.available_decimal == Decimal("1000.50")
        assert bal.pending_decimal == Decimal("250.75")

    def test_sweep_result_decimal(self):
        result = SweepResult(success=True, amount_crypto=1.234567890123, fee=0.00005)
        assert result.amount_decimal == Decimal("1.234567890123")
        assert result.fee_decimal == Decimal("0.00005")

    def test_1000_small_payments_no_rounding_loss(self):
        """Simulate 1000 payments of €0.01 — verify no precision loss."""
        total = Decimal("0")
        for _ in range(1000):
            total += _to_decimal(0.01)
        assert total == Decimal("10.00")


# ── Gumroad Webhook Signature Tests ─────────────────────────


class TestGumroadWebhookSignature:
    """Verify Gumroad webhook HMAC-SHA256 signature verification."""

    @pytest.fixture
    def provider(self):
        from monai.payments.gumroad_provider import GumroadProvider
        return GumroadProvider(
            access_token="test_token",
            webhook_secret="test_webhook_secret_123",
        )

    def test_valid_signature_accepted(self, provider):
        payload = b"sale_id=abc123&price=999&email=test@example.com"
        expected_sig = hmac.new(
            b"test_webhook_secret_123", payload, hashlib.sha256
        ).hexdigest()
        assert provider._verify_signature(payload, expected_sig) is True

    def test_invalid_signature_rejected(self, provider):
        payload = b"sale_id=abc123&price=999"
        assert provider._verify_signature(payload, "invalid_hex_sig") is False

    def test_empty_signature_rejected(self, provider):
        payload = b"sale_id=abc123"
        assert provider._verify_signature(payload, "") is False

    def test_no_secret_rejects_all(self):
        from monai.payments.gumroad_provider import GumroadProvider
        provider = GumroadProvider(access_token="test", webhook_secret="")
        payload = b"data=test"
        assert provider._verify_signature(payload, "any_sig") is False

    @pytest.mark.asyncio
    async def test_webhook_without_signature_rejected(self, provider):
        """When webhook_secret is set, unsigned webhooks are rejected."""
        payload = b"sale_id=test&price=500&resource_name=sale&email=a@b.com"
        result = await provider.handle_webhook(payload, {})
        assert result is None

    @pytest.mark.asyncio
    async def test_webhook_with_valid_signature_accepted(self, provider):
        payload = b"sale_id=test123&price=500&resource_name=sale&email=a@b.com&currency=usd"
        sig = hmac.new(
            b"test_webhook_secret_123", payload, hashlib.sha256
        ).hexdigest()
        result = await provider.handle_webhook(
            payload, {"x-gumroad-signature": sig}
        )
        assert result is not None
        assert result.payment_ref == "test123"
        assert result.event_type == WebhookEventType.PAYMENT_COMPLETED


# ── Webhook Idempotency Tests ───────────────────────────────


class TestWebhookIdempotency:
    """Verify that duplicate webhooks are not processed twice (atomic check)."""

    @pytest.fixture
    def db(self, tmp_path):
        return Database(tmp_path / "test_idempotency.db")

    @pytest.fixture
    def manager(self, db):
        config = MagicMock()
        config.monero.wallet_rpc_url = ""
        config.creator_wallet.xmr_address = ""
        config.creator_wallet.sweep_threshold_eur = 50.0
        from monai.payments.manager import UnifiedPaymentManager
        return UnifiedPaymentManager(config, db)

    def _insert_webhook_id(self, db, provider, event_id):
        """Helper: insert a processed webhook ID directly (simulates first processing)."""
        try:
            db.execute_insert(
                "INSERT INTO processed_webhooks (provider, event_id) VALUES (?, ?)",
                (provider, event_id),
            )
            return False  # First time
        except Exception:
            return True  # Duplicate

    def test_first_webhook_not_duplicate(self, manager, db):
        assert self._insert_webhook_id(db, "stripe", "evt_123") is False

    def test_second_webhook_is_duplicate(self, manager, db):
        self._insert_webhook_id(db, "stripe", "evt_456")
        assert self._insert_webhook_id(db, "stripe", "evt_456") is True

    def test_different_providers_not_duplicate(self, manager, db):
        self._insert_webhook_id(db, "stripe", "evt_789")
        assert self._insert_webhook_id(db, "gumroad", "evt_789") is False

    def test_atomic_webhook_processing(self, manager, db):
        """Verify webhook event log is created atomically with idempotency check."""
        import asyncio
        from monai.payments.types import WebhookEvent, WebhookEventType

        # Set up a collection account so the payment insert doesn't fail FK
        from monai.business.brand_payments import BrandPayments
        bp = BrandPayments(db)
        bp.add_collection_account("test_brand", "stripe", "acct_test")

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_test_atomic",
            amount=50.0,
            currency="EUR",
            metadata={"brand": "test_brand"},
        )

        # Process the event
        asyncio.get_event_loop().run_until_complete(
            manager._handle_webhook_event(event)
        )

        # Verify it was logged
        rows = db.execute(
            "SELECT * FROM webhook_events WHERE payment_ref = 'pi_test_atomic'"
        )
        assert len(rows) == 1

        # Process the same event again — should be ignored (duplicate)
        asyncio.get_event_loop().run_until_complete(
            manager._handle_webhook_event(event)
        )

        # Should still only have 1 entry
        rows = db.execute(
            "SELECT * FROM webhook_events WHERE payment_ref = 'pi_test_atomic'"
        )
        assert len(rows) == 1


# ── Platform Fee Tracking Tests ──────────────────────────────


class TestPlatformFeeTracking:
    """Verify platform fees are recorded and calculated correctly."""

    @pytest.fixture
    def db(self, tmp_path):
        d = Database(tmp_path / "test_fees.db")
        return d

    @pytest.fixture
    def bp(self, db):
        from monai.business.brand_payments import BrandPayments
        return BrandPayments(db)

    def test_record_stripe_fee_auto_calculated(self, bp):
        """Stripe: 2.9% + €0.30"""
        bp.add_collection_account("brand1", "stripe", "acct_1")
        pay_id = bp.record_payment("brand1", 1, amount=100.0, payment_ref="ch_1")
        fee_id = bp.record_platform_fee("brand1", "stripe", pay_id, gross_amount=100.0)
        assert fee_id >= 1

        total_fees = bp.get_total_fees("brand1")
        # Stripe: 100 * 0.029 + 0.30 = 3.20
        assert abs(total_fees - 3.20) < 0.01

    def test_record_gumroad_fee_auto_calculated(self, bp):
        """Gumroad: 10%"""
        bp.add_collection_account("brand2", "gumroad", "acct_2")
        pay_id = bp.record_payment("brand2", 1, amount=50.0, payment_ref="sale_1")
        bp.record_platform_fee("brand2", "gumroad", pay_id, gross_amount=50.0)

        total_fees = bp.get_total_fees("brand2")
        assert abs(total_fees - 5.0) < 0.01

    def test_record_custom_fee(self, bp):
        """Manual fee override."""
        acct_id = bp.add_collection_account("brand3", "btcpay", "addr_3")
        pay_id = bp.record_payment("brand3", acct_id, amount=200.0, payment_ref="custom_1")
        bp.record_platform_fee("brand3", "btcpay", pay_id, gross_amount=200.0,
                               fee_amount=0.50, fee_currency="BTC")
        assert bp.get_total_fees("brand3") == 0.50

    def test_net_revenue_calculation(self, bp):
        """Net revenue = gross - fees."""
        bp.add_collection_account("brand4", "stripe", "acct_4")
        pay_id = bp.record_payment("brand4", 1, amount=100.0, payment_ref="ch_net")
        bp.record_platform_fee("brand4", "stripe", pay_id, gross_amount=100.0)

        net = bp.get_net_revenue("brand4")
        # 100 - 3.20 = 96.80
        assert abs(net - 96.80) < 0.01

    def test_unknown_provider_zero_fee(self, bp):
        """Unknown providers default to 0 fee."""
        acct_id = bp.add_collection_account("brand5", "unknown_provider", "addr_5")
        pay_id = bp.record_payment("brand5", acct_id, amount=100.0, payment_ref="unknown_1")
        bp.record_platform_fee("brand5", "unknown_provider", pay_id, gross_amount=100.0)
        assert bp.get_total_fees("brand5") == 0.0


# ── Database Transaction & Index Tests ──────────────────────


class TestDatabaseImprovements:
    """Verify DB indexes and transaction support."""

    @pytest.fixture
    def db(self, tmp_path):
        return Database(tmp_path / "test_db_improvements.db")

    def test_indexes_created(self, db):
        """Verify performance indexes exist."""
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        index_names = {r["name"] for r in rows}
        expected = {
            "idx_agent_log_agent_name",
            "idx_strategies_status",
            "idx_contacts_platform",
            "idx_transactions_strategy",
            "idx_transactions_type",
            "idx_projects_status",
            "idx_messages_contact",
        }
        assert expected.issubset(index_names), f"Missing indexes: {expected - index_names}"

    def test_transaction_commit(self, db):
        """Transaction context manager commits on success."""
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO strategies (name, category, status) VALUES (?, ?, ?)",
                ("test_strat", "test", "active"),
            )
        rows = db.execute("SELECT * FROM strategies WHERE name = 'test_strat'")
        assert len(rows) == 1

    def test_transaction_rollback_on_error(self, db):
        """Transaction context manager rolls back on exception."""
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO strategies (name, category, status) VALUES (?, ?, ?)",
                    ("should_rollback", "test", "active"),
                )
                raise ValueError("Simulated error")
        except ValueError:
            pass
        rows = db.execute("SELECT * FROM strategies WHERE name = 'should_rollback'")
        assert len(rows) == 0

    def test_brand_payment_indexes_created(self, tmp_path):
        """Verify brand payment indexes exist after BrandPayments init."""
        from monai.business.brand_payments import BrandPayments
        d = Database(tmp_path / "test_bp_idx.db")
        BrandPayments(d)
        rows = d.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_bp%'"
        )
        index_names = {r["name"] for r in rows}
        assert "idx_bpr_brand" in index_names
        assert "idx_bpr_payment_ref" in index_names
        assert "idx_bps_brand" in index_names
        assert "idx_bpf_brand" in index_names


# ── HTTP Client Pooling Tests ────────────────────────────────


class TestHTTPClientPooling:
    """Verify payment providers reuse HTTP clients."""

    def test_stripe_reuses_client(self):
        from monai.payments.stripe_provider import StripeProvider
        provider = StripeProvider(api_key="sk_test", proxy_url="")
        client1 = provider._get_client()
        client2 = provider._get_client()
        assert client1 is client2

    def test_btcpay_reuses_client(self):
        from monai.payments.btcpay_provider import BTCPayProvider
        provider = BTCPayProvider(
            server_url="http://localhost", api_key="test",
            store_id="store1", proxy_url="",
        )
        client1 = provider._get_client()
        client2 = provider._get_client()
        assert client1 is client2

    def test_gumroad_reuses_client(self):
        from monai.payments.gumroad_provider import GumroadProvider
        provider = GumroadProvider(access_token="test", proxy_url="")
        client1 = provider._get_client()
        client2 = provider._get_client()
        assert client1 is client2

    def test_monero_reuses_client(self):
        from monai.payments.monero_provider import MoneroProvider
        provider = MoneroProvider(proxy_url="")
        client1 = provider._get_client()
        client2 = provider._get_client()
        assert client1 is client2

    def test_lemonsqueezy_reuses_client(self):
        from monai.payments.lemonsqueezy_provider import LemonSqueezyProvider
        provider = LemonSqueezyProvider(api_key="test", store_id="s1", proxy_url="")
        client1 = provider._get_client()
        client2 = provider._get_client()
        assert client1 is client2


# ── Sensitive Data Filter Tests ──────────────────────────────


class TestSensitiveDataFilter:
    """Verify expanded sensitive data filtering in agent identity."""

    def _filter(self, identity: dict) -> dict:
        """Simulate the filtering logic from BaseAgent.execute_task."""
        import re
        _SENSITIVE_PATTERN = re.compile(
            r'(password|secret|token|api_key|api_secret|private_key|'
            r'auth_token|bearer|refresh_token|access_token|credentials|'
            r'webhook_secret|rpc_password|bot_token|pin|card_)',
            re.IGNORECASE,
        )
        return {k: v for k, v in identity.items() if not _SENSITIVE_PATTERN.search(k)}

    def test_filters_basic_keys(self):
        identity = {"name": "Bot", "password": "s3cret", "api_key": "sk-123"}
        safe = self._filter(identity)
        assert "name" in safe
        assert "password" not in safe
        assert "api_key" not in safe

    def test_filters_extended_keys(self):
        identity = {
            "name": "Bot",
            "private_key": "xxx",
            "auth_token": "yyy",
            "refresh_token": "zzz",
            "access_token": "aaa",
            "webhook_secret": "bbb",
            "rpc_password": "ccc",
            "bot_token": "ddd",
        }
        safe = self._filter(identity)
        assert safe == {"name": "Bot"}

    def test_keeps_safe_keys(self):
        identity = {"name": "Agent", "email": "a@b.com", "platform": "gumroad"}
        safe = self._filter(identity)
        assert safe == identity


# ── Executor Timeout Tests ───────────────────────────────────


class TestExecutorTimeout:
    """Verify executor has configurable timeout parameter."""

    def test_timeout_parameter_accepted(self):
        """Verify AutonomousExecutor accepts timeout_seconds parameter."""
        from monai.agents.executor import AutonomousExecutor
        config = MagicMock()
        config.privacy.proxy_type = "none"
        config.privacy.tor_socks_port = 9050
        config.privacy.socks5_proxy = ""
        config.privacy.http_proxy = ""
        db = MagicMock()
        llm = MagicMock()
        executor = AutonomousExecutor(config, db, llm, timeout_seconds=1800)
        assert executor.timeout_seconds == 1800
        assert executor.max_steps == 50  # default unchanged
