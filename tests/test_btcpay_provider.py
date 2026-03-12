"""Tests for BTCPay Server payment provider."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.payments.btcpay_provider import BTCPayProvider, BTCPayAPIError
from monai.payments.types import PaymentIntent, PaymentStatus, WebhookEventType


@pytest.fixture
def provider():
    return BTCPayProvider(
        server_url="https://btcpay.test.com",
        api_key="api_test_key",
        store_id="store_123",
        webhook_secret="wh_secret_456",
    )


def mock_api_response(data: dict, status: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.json.return_value = data
    mock_resp.text = json.dumps(data)
    return mock_resp


class TestBTCPayProvider:
    @pytest.mark.asyncio
    async def test_create_invoice(self, provider):
        invoice = {
            "id": "inv_test_123",
            "checkoutLink": "https://btcpay.test.com/checkout/inv_test_123",
            "amount": 50.0,
            "currency": "EUR",
        }
        mock_resp = mock_api_response(invoice)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            intent = PaymentIntent(amount=50.0, product="SaaS Monthly", brand="micro_saas")
            result = await provider.create_payment(intent)

        assert result.success is True
        assert result.payment_ref == "inv_test_123"
        assert "btcpay.test.com" in result.checkout_url

    @pytest.mark.asyncio
    async def test_verify_invoice_settled(self, provider):
        invoice = {
            "id": "inv_test_123",
            "status": "Settled",
            "amount": "50.0",
            "currency": "EUR",
        }
        mock_resp = mock_api_response(invoice)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            result = await provider.verify_payment("inv_test_123")

        assert result.success is True
        assert result.status == PaymentStatus.COMPLETED
        assert result.amount == 50.0

    @pytest.mark.asyncio
    async def test_verify_invoice_expired(self, provider):
        invoice = {"id": "inv_expired", "status": "Expired", "amount": "25.0", "currency": "EUR"}
        mock_resp = mock_api_response(invoice)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            result = await provider.verify_payment("inv_expired")

        assert result.status == PaymentStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_send_payout(self, provider):
        tx_data = {"transactionHash": "abc123def456"}
        mock_resp = mock_api_response(tx_data)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            # Use a realistic-length bech32 address for validation
            result = await provider.send_payout(
                "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", 0.01, "BTC"
            )

        assert result.success is True
        assert result.payment_ref == "abc123def456"


class TestBTCPayWebhook:
    def test_valid_webhook_invoice_settled(self, provider):
        event_data = {
            "type": "InvoiceSettled",
            "invoiceId": "inv_settled_123",
            "metadata": {"brand": "newsletter"},
        }
        payload = json.dumps(event_data).encode()

        sig = hmac.new(
            provider.webhook_secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        headers = {"btcpay-sig": f"sha256={sig}"}

        # Mock the invoice fetch for amount
        invoice = {"amount": "99.0", "currency": "EUR"}
        mock_resp = mock_api_response(invoice)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            import asyncio
            event = asyncio.get_event_loop().run_until_complete(
                provider.handle_webhook(payload, headers)
            )

        assert event is not None
        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.payment_ref == "inv_settled_123"

    def test_invalid_webhook_signature(self, provider):
        payload = b'{"type": "InvoiceSettled", "invoiceId": "x"}'
        headers = {"btcpay-sig": "sha256=invalidsig"}

        import asyncio
        event = asyncio.get_event_loop().run_until_complete(
            provider.handle_webhook(payload, headers)
        )
        assert event is None

    def test_status_mapping(self):
        assert BTCPayProvider._map_status("Settled") == PaymentStatus.COMPLETED
        assert BTCPayProvider._map_status("New") == PaymentStatus.PENDING
        assert BTCPayProvider._map_status("Expired") == PaymentStatus.EXPIRED
        assert BTCPayProvider._map_status("Invalid") == PaymentStatus.FAILED
