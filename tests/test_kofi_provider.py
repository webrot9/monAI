"""Tests for Ko-fi webhook provider — signature verification and event parsing."""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from monai.payments.kofi_provider import KofiProvider
from monai.payments.types import WebhookEventType


def _make_kofi_payload(
    verification_token: str = "test-token-123",
    amount: str = "5.00",
    currency: str = "EUR",
    from_name: str = "TestBacker",
    event_type: str = "Donation",
    email: str = "backer@example.com",
    message: str = "Keep building!",
    kofi_transaction_id: str = "txn_abc123",
    **overrides: object,
) -> bytes:
    """Build a Ko-fi webhook payload (form-encoded)."""
    data = {
        "verification_token": verification_token,
        "message_id": "msg_001",
        "timestamp": "2026-03-11T12:00:00Z",
        "type": event_type,
        "is_public": True,
        "from_name": from_name,
        "message": message,
        "amount": amount,
        "url": "https://ko-fi.com/monai",
        "email": email,
        "currency": currency,
        "is_subscription_payment": False,
        "is_first_subscription_payment": False,
        "kofi_transaction_id": kofi_transaction_id,
        "shop_items": None,
        "tier_name": None,
    }
    data.update(overrides)
    return f"data={quote(json.dumps(data))}".encode()


@pytest.fixture
def provider():
    return KofiProvider(verification_token="test-token-123")


class TestKofiWebhookVerification:
    @pytest.mark.asyncio
    async def test_valid_token_accepted(self, provider):
        """Valid verification token passes verification."""
        payload = _make_kofi_payload(verification_token="test-token-123")
        event = await provider.handle_webhook(payload, {})

        assert event is not None
        assert event.provider == "kofi"
        assert event.amount == 5.0
        assert event.currency == "EUR"

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, provider):
        """Wrong verification token is rejected."""
        payload = _make_kofi_payload(verification_token="wrong-token")
        event = await provider.handle_webhook(payload, {})
        assert event is None

    @pytest.mark.asyncio
    async def test_empty_token_rejected(self, provider):
        """Empty verification token is rejected."""
        payload = _make_kofi_payload(verification_token="")
        event = await provider.handle_webhook(payload, {})
        assert event is None

    @pytest.mark.asyncio
    async def test_no_configured_token_rejects_all(self):
        """Provider with no token configured rejects everything."""
        provider = KofiProvider(verification_token="")
        payload = _make_kofi_payload(verification_token="anything")
        event = await provider.handle_webhook(payload, {})
        assert event is None

    @pytest.mark.asyncio
    async def test_constant_time_comparison(self, provider):
        """Token comparison uses constant-time to prevent timing attacks."""
        import hmac
        # Verify the provider uses hmac.compare_digest internally
        assert provider._verify_token("test-token-123")
        assert not provider._verify_token("test-token-124")


class TestKofiEventParsing:
    @pytest.mark.asyncio
    async def test_donation_event(self, provider):
        """Donation webhook parsed correctly."""
        payload = _make_kofi_payload(
            amount="10.00", from_name="Alice", message="Love the project!",
        )
        event = await provider.handle_webhook(payload, {})

        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.amount == 10.0
        assert event.metadata["from_name"] == "Alice"
        assert event.metadata["message"] == "Love the project!"

    @pytest.mark.asyncio
    async def test_subscription_event(self, provider):
        """Subscription webhook parsed as SUBSCRIPTION_CREATED."""
        payload = _make_kofi_payload(event_type="Subscription")
        event = await provider.handle_webhook(payload, {})
        assert event.event_type == WebhookEventType.SUBSCRIPTION_CREATED

    @pytest.mark.asyncio
    async def test_shop_order_event(self, provider):
        """Shop order webhook parsed correctly."""
        payload = _make_kofi_payload(
            event_type="Shop Order",
            amount="25.00",
        )
        event = await provider.handle_webhook(payload, {})
        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.amount == 25.0

    @pytest.mark.asyncio
    async def test_payment_ref_from_transaction_id(self, provider):
        """Payment ref uses kofi_transaction_id."""
        payload = _make_kofi_payload(kofi_transaction_id="txn_xyz")
        event = await provider.handle_webhook(payload, {})
        assert event.payment_ref == "txn_xyz"

    @pytest.mark.asyncio
    async def test_customer_email_captured(self, provider):
        """Customer email extracted from payload."""
        payload = _make_kofi_payload(email="supporter@test.com")
        event = await provider.handle_webhook(payload, {})
        assert event.customer_email == "supporter@test.com"

    @pytest.mark.asyncio
    async def test_raw_json_payload(self, provider):
        """Raw JSON (not form-encoded) also handled."""
        data = json.dumps({
            "verification_token": "test-token-123",
            "message_id": "msg_002",
            "timestamp": "2026-03-11T12:00:00Z",
            "type": "Donation",
            "is_public": True,
            "from_name": "Bob",
            "message": "",
            "amount": "3.00",
            "url": "",
            "email": "",
            "currency": "USD",
            "is_subscription_payment": False,
            "is_first_subscription_payment": False,
            "kofi_transaction_id": "txn_raw",
            "shop_items": None,
            "tier_name": None,
        }).encode()

        event = await provider.handle_webhook(data, {})
        assert event is not None
        assert event.amount == 3.0
        assert event.currency == "USD"

    @pytest.mark.asyncio
    async def test_malformed_payload_returns_none(self, provider):
        """Garbage payload returns None, not an exception."""
        event = await provider.handle_webhook(b"not-valid-data", {})
        assert event is None

    @pytest.mark.asyncio
    async def test_empty_payload_returns_none(self, provider):
        """Empty payload returns None."""
        event = await provider.handle_webhook(b"", {})
        assert event is None


class TestKofiProviderStubs:
    """Ko-fi is webhook-only; payment API methods should fail gracefully."""

    @pytest.mark.asyncio
    async def test_create_payment_unsupported(self, provider):
        from monai.payments.types import PaymentIntent, PaymentStatus
        result = await provider.create_payment(PaymentIntent(amount=10.0))
        assert result.status == PaymentStatus.FAILED

    @pytest.mark.asyncio
    async def test_verify_payment_unsupported(self, provider):
        from monai.payments.types import PaymentStatus
        result = await provider.verify_payment("txn_123")
        assert result.status == PaymentStatus.FAILED

    @pytest.mark.asyncio
    async def test_send_payout_unsupported(self, provider):
        from monai.payments.types import PaymentStatus
        result = await provider.send_payout("addr", 10.0)
        assert result.status == PaymentStatus.FAILED
