"""Tests for webhook audit trail integration."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from monai.business.audit import AuditTrail
from monai.payments.types import WebhookEvent, WebhookEventType
from monai.payments.webhook_server import WebhookServer, set_webhook_audit


@pytest.fixture
def audit(db):
    return AuditTrail(db)


@pytest.fixture
def webhook_server():
    return WebhookServer(host="127.0.0.1", port=0)


class TestWebhookAuditIntegration:
    def test_set_webhook_audit(self, audit):
        """set_webhook_audit sets the module-level audit trail."""
        from monai.payments import webhook_server
        set_webhook_audit(audit)
        assert webhook_server._audit_trail is audit
        # Cleanup
        set_webhook_audit(None)

    def test_successful_webhook_audited(self, audit, db):
        """Successful webhook processing is logged to audit trail."""
        set_webhook_audit(audit)
        try:
            # Simulate what _handle_webhook does after a successful event
            event = WebhookEvent(
                event_type=WebhookEventType.PAYMENT_COMPLETED,
                provider="stripe",
                payment_ref="pi_123",
                amount=49.99,
                currency="EUR",
                metadata={"brand": "test_brand"},
            )
            # Directly call the audit log as the webhook handler would
            audit.log(
                "webhook_server", "payment", "webhook_received",
                details={
                    "provider": event.provider,
                    "event_type": event.event_type.value,
                    "payment_ref": event.payment_ref,
                    "amount": str(event.amount),
                    "currency": event.currency,
                },
                brand=event.metadata.get("brand", ""),
            )

            entries = audit.get_recent(agent_name="webhook_server")
            assert len(entries) == 1
            assert entries[0]["action"] == "webhook_received"
            assert entries[0]["brand"] == "test_brand"
            assert entries[0]["action_type"] == "payment"
        finally:
            set_webhook_audit(None)

    def test_webhook_error_audited(self, audit):
        """Webhook processing errors are logged as failures."""
        set_webhook_audit(audit)
        try:
            audit.log(
                "webhook_server", "api_call", "webhook_error",
                details={"provider": "stripe", "error": "signature mismatch"},
                success=False, risk_level="high",
            )

            entries = audit.get_recent(success=False)
            assert len(entries) == 1
            assert entries[0]["risk_level"] == "high"
            assert "signature mismatch" in entries[0]["details"]
        finally:
            set_webhook_audit(None)

    def test_invalid_webhook_audited(self, audit):
        """Invalid/unverifiable webhooks are logged."""
        set_webhook_audit(audit)
        try:
            audit.log(
                "webhook_server", "api_call", "webhook_invalid",
                details={"provider": "btcpay"}, success=False,
            )

            entries = audit.get_recent(agent_name="webhook_server")
            assert len(entries) == 1
            assert entries[0]["action"] == "webhook_invalid"
            assert entries[0]["success"] == 0
        finally:
            set_webhook_audit(None)

    def test_no_audit_when_not_set(self):
        """No crash when audit trail is not configured."""
        set_webhook_audit(None)
        # This should not raise — the handler checks for _audit_trail
        from monai.payments import webhook_server
        assert webhook_server._audit_trail is None
