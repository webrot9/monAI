"""Tests for Stripe payment provider."""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.payments.stripe_provider import StripeProvider, StripeAPIError
from monai.payments.types import PaymentIntent, PaymentStatus, WebhookEventType


@pytest.fixture
def provider():
    return StripeProvider(
        api_key="sk_test_123",
        webhook_secret="whsec_test_456",
    )


def mock_stripe_response(data: dict, status: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.json.return_value = data
    return mock_resp


class TestStripeProvider:
    @pytest.mark.asyncio
    async def test_create_payment_session(self, provider):
        session_data = {
            "id": "cs_test_abc",
            "url": "https://checkout.stripe.com/c/pay/cs_test_abc",
            "payment_status": "unpaid",
        }
        mock_resp = mock_stripe_response(session_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            intent = PaymentIntent(
                amount=29.99, currency="EUR", product="AI Guide",
                brand="newsletter",
                metadata={"success_url": "https://brand.com/thanks"},
            )
            result = await provider.create_payment(intent)

        assert result.success is True
        assert result.payment_ref == "cs_test_abc"
        assert "checkout.stripe.com" in result.checkout_url

    @pytest.mark.asyncio
    async def test_create_payment_api_error(self, provider):
        error_data = {
            "error": {
                "type": "invalid_request_error",
                "message": "Invalid API key",
                "code": "api_key_invalid",
            },
        }
        mock_resp = mock_stripe_response(error_data, status=401)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            intent = PaymentIntent(amount=10.0, product="test")
            result = await provider.create_payment(intent)

        assert result.success is False
        assert "Invalid API key" in result.error

    @pytest.mark.asyncio
    async def test_verify_checkout_session(self, provider):
        session_data = {
            "id": "cs_test_abc",
            "payment_status": "paid",
            "amount_total": 2999,
            "currency": "eur",
        }
        mock_resp = mock_stripe_response(session_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.verify_payment("cs_test_abc")

        assert result.success is True
        assert result.amount == 29.99
        assert result.currency == "EUR"
        assert result.status == PaymentStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_verify_payment_intent(self, provider):
        pi_data = {
            "id": "pi_test_xyz",
            "status": "succeeded",
            "amount": 5000,
            "currency": "eur",
        }
        mock_resp = mock_stripe_response(pi_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.verify_payment("pi_test_xyz")

        assert result.status == PaymentStatus.COMPLETED
        assert result.amount == 50.0

    @pytest.mark.asyncio
    async def test_get_balance(self, provider):
        balance_data = {
            "available": [{"amount": 10000, "currency": "eur"}],
            "pending": [{"amount": 2500, "currency": "eur"}],
        }
        mock_resp = mock_stripe_response(balance_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            balance = await provider.get_balance()

        assert balance.available == 100.0
        assert balance.pending == 25.0
        assert balance.currency == "EUR"


class TestStripeWebhook:
    def test_valid_webhook(self, provider):
        event_data = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_completed",
                    "amount_total": 4999,
                    "currency": "eur",
                    "customer_email": "buyer@example.com",
                    "metadata": {"brand": "micro_saas"},
                },
            },
        }
        payload = json.dumps(event_data).encode()

        # Generate valid signature
        timestamp = str(int(time.time()))
        signed = f"{timestamp}.".encode() + payload
        sig = hmac.new(
            provider.webhook_secret.encode(), signed, hashlib.sha256
        ).hexdigest()
        headers = {"stripe-signature": f"t={timestamp},v1={sig}"}

        import asyncio
        event = asyncio.run(
            provider.handle_webhook(payload, headers)
        )

        assert event is not None
        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.amount == 49.99
        assert event.customer_email == "buyer@example.com"
        assert event.metadata["brand"] == "micro_saas"

    def test_invalid_signature(self, provider):
        payload = b'{"type": "test"}'
        headers = {"stripe-signature": "t=123,v1=invalidsig"}

        import asyncio
        event = asyncio.run(
            provider.handle_webhook(payload, headers)
        )

        assert event is None

    def test_expired_timestamp(self, provider):
        payload = b'{"type": "checkout.session.completed", "data": {"object": {}}}'
        old_timestamp = str(int(time.time()) - 600)  # 10 minutes ago
        signed = f"{old_timestamp}.".encode() + payload
        sig = hmac.new(
            provider.webhook_secret.encode(), signed, hashlib.sha256
        ).hexdigest()
        headers = {"stripe-signature": f"t={old_timestamp},v1={sig}"}

        import asyncio
        event = asyncio.run(
            provider.handle_webhook(payload, headers)
        )

        assert event is None


class TestStripeStatusMapping:
    def test_session_status_mapping(self):
        assert StripeProvider._map_session_status("paid") == PaymentStatus.COMPLETED
        assert StripeProvider._map_session_status("unpaid") == PaymentStatus.PENDING
        assert StripeProvider._map_session_status("unknown") == PaymentStatus.PENDING

    def test_intent_status_mapping(self):
        assert StripeProvider._map_intent_status("succeeded") == PaymentStatus.COMPLETED
        assert StripeProvider._map_intent_status("canceled") == PaymentStatus.FAILED
        assert StripeProvider._map_intent_status("processing") == PaymentStatus.PENDING
