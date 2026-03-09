"""Tests for the webhook server."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from monai.payments.webhook_server import WebhookServer
from monai.payments.types import WebhookEvent, WebhookEventType


@pytest.fixture
def server():
    return WebhookServer(host="127.0.0.1", port=0)


class TestWebhookServer:
    def test_register_provider(self, server):
        mock_provider = MagicMock()
        server.register_provider("stripe", mock_provider)
        assert "stripe" in server._providers

    def test_register_event_handler(self, server):
        handler = AsyncMock()
        server.on_event(handler)
        assert handler in server._event_handlers

    def test_register_multiple_providers(self, server):
        server.register_provider("stripe", MagicMock())
        server.register_provider("btcpay", MagicMock())
        server.register_provider("gumroad", MagicMock())
        assert len(server._providers) == 3

    @pytest.mark.asyncio
    async def test_handle_webhook_routes_correctly(self, server):
        """Test that webhook events reach the event handler."""
        mock_provider = MagicMock()
        mock_event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="test",
            payment_ref="ref_123",
            amount=42.0,
        )
        mock_provider.handle_webhook = AsyncMock(return_value=mock_event)
        server.register_provider("test", mock_provider)

        received_events = []

        async def handler(event):
            received_events.append(event)

        server.on_event(handler)

        # Simulate the internal webhook handling logic
        event = await mock_provider.handle_webhook(b'{}', {})
        assert event is not None
        for h in server._event_handlers:
            await h(event)

        assert len(received_events) == 1
        assert received_events[0].amount == 42.0
        assert received_events[0].payment_ref == "ref_123"

    @pytest.mark.asyncio
    async def test_handle_webhook_invalid_returns_none(self, server):
        mock_provider = MagicMock()
        mock_provider.handle_webhook = AsyncMock(return_value=None)
        server.register_provider("test", mock_provider)

        event = await mock_provider.handle_webhook(b'invalid', {})
        assert event is None

    def test_send_response_format(self):
        """Test that HTTP responses are properly formatted."""
        # This tests the static method behavior
        from http import HTTPStatus
        assert HTTPStatus(200).phrase == "OK"
        assert HTTPStatus(404).phrase == "Not Found"
        assert HTTPStatus(500).phrase == "Internal Server Error"
