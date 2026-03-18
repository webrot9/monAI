"""Ko-fi webhook provider — handles Ko-fi donation/payment webhooks.

Ko-fi webhooks are POST requests with:
- Content-Type: application/x-www-form-urlencoded
- Body: `data=<json-string>`
- Verification: JSON payload contains a `verification_token` field
  that must match the configured token from Ko-fi settings.

Reference: https://ko-fi.com/manage/webhooks
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

from monai.payments.base import PaymentProvider
from monai.payments.types import (
    PaymentIntent,
    PaymentResult,
    PaymentStatus,
    ProviderBalance,
    WebhookEvent,
    WebhookEventType,
    _to_decimal,
)

logger = logging.getLogger(__name__)


# Ko-fi webhook event type mapping
_KOFI_EVENT_MAP = {
    "Donation": WebhookEventType.PAYMENT_COMPLETED,
    "Subscription": WebhookEventType.SUBSCRIPTION_CREATED,
    "Shop Order": WebhookEventType.PAYMENT_COMPLETED,
    "Commission": WebhookEventType.PAYMENT_COMPLETED,
}


@dataclass
class KofiWebhookPayload:
    """Parsed Ko-fi webhook data."""
    verification_token: str
    message_id: str
    timestamp: str
    event_type: str  # Donation, Subscription, Shop Order, Commission
    is_public: bool
    from_name: str
    message: str
    amount: str
    url: str
    email: str
    currency: str
    is_subscription_payment: bool
    is_first_subscription_payment: bool
    kofi_transaction_id: str
    shop_items: list[dict[str, Any]]
    tier_name: str | None
    raw: dict[str, Any]


class KofiProvider(PaymentProvider):
    """Ko-fi payment/donation webhook handler.

    Ko-fi doesn't have a traditional payment API — it uses webhooks
    to notify about donations and purchases. This provider handles
    webhook verification and event parsing.
    """

    provider_name = "kofi"

    def __init__(self, verification_token: str = ""):
        self.verification_token = verification_token

    # ── Webhook Handling ─────────────────────────────────────────

    async def handle_webhook(self, payload: bytes,
                             headers: dict[str, str]) -> WebhookEvent | None:
        """Parse and verify a Ko-fi webhook.

        Ko-fi sends form-encoded data with a `data` field containing JSON.
        Verification is done by matching the verification_token in the payload.
        """
        parsed = self._parse_kofi_payload(payload)
        if not parsed:
            logger.warning("Failed to parse Ko-fi webhook payload")
            return None

        if not self._verify_token(parsed.verification_token):
            logger.warning("Ko-fi webhook verification token mismatch")
            return None

        event_type = _KOFI_EVENT_MAP.get(
            parsed.event_type, WebhookEventType.PAYMENT_COMPLETED
        )

        try:
            amount = _to_decimal(parsed.amount)
        except Exception:
            amount = _to_decimal(0)

        return WebhookEvent(
            event_type=event_type,
            provider="kofi",
            payment_ref=parsed.kofi_transaction_id or parsed.message_id,
            amount=amount,
            currency=parsed.currency or "EUR",
            customer_email=parsed.email,
            product=parsed.tier_name or parsed.event_type,
            metadata={
                "from_name": parsed.from_name,
                "message": parsed.message,
                "is_public": parsed.is_public,
                "is_subscription": parsed.is_subscription_payment,
                "is_first_subscription": parsed.is_first_subscription_payment,
                "shop_items": parsed.shop_items,
                "url": parsed.url,
            },
            raw=parsed.raw,
        )

    def _parse_kofi_payload(self, payload: bytes) -> KofiWebhookPayload | None:
        """Parse Ko-fi's form-encoded webhook into structured data."""
        try:
            # Ko-fi sends: data=<url-encoded-json>
            body_str = payload.decode("utf-8")

            # Extract the data field from form encoding
            data_json = None
            if body_str.startswith("data="):
                from urllib.parse import unquote
                data_json = unquote(body_str[5:])
            else:
                # Try parsing as raw JSON
                data_json = body_str

            data = json.loads(data_json)

            return KofiWebhookPayload(
                verification_token=data.get("verification_token", ""),
                message_id=data.get("message_id", ""),
                timestamp=data.get("timestamp", ""),
                event_type=data.get("type", "Donation"),
                is_public=data.get("is_public", False),
                from_name=data.get("from_name", ""),
                message=data.get("message", ""),
                amount=data.get("amount", "0"),
                url=data.get("url", ""),
                email=data.get("email", ""),
                currency=data.get("currency", "EUR"),
                is_subscription_payment=data.get("is_subscription_payment", False),
                is_first_subscription_payment=data.get("is_first_subscription_payment", False),
                kofi_transaction_id=data.get("kofi_transaction_id", ""),
                shop_items=data.get("shop_items") or [],
                tier_name=data.get("tier_name"),
                raw=data,
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error(f"Failed to parse Ko-fi payload: {e}")
            return None

    def _verify_token(self, token: str) -> bool:
        """Verify the Ko-fi verification token using constant-time comparison."""
        if not self.verification_token or not token:
            return False
        return hmac.compare_digest(self.verification_token, token)

    # ── PaymentProvider interface (Ko-fi is webhook-only) ────────

    async def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Ko-fi doesn't support programmatic payment creation."""
        return PaymentResult(
            success=False,
            status=PaymentStatus.FAILED,
            error="Ko-fi does not support programmatic payment creation. "
                  "Use the Ko-fi page URL instead.",
        )

    async def verify_payment(self, payment_ref: str) -> PaymentResult:
        """Ko-fi doesn't have a payment verification API."""
        return PaymentResult(
            success=False,
            status=PaymentStatus.FAILED,
            payment_ref=payment_ref,
            error="Ko-fi does not support payment verification via API.",
        )

    async def get_balance(self, account_id: str) -> ProviderBalance:
        """Ko-fi doesn't expose balance info."""
        return ProviderBalance(provider="kofi", account_id=account_id)

    async def send_payout(self, to_address: str, amount: float,
                          currency: str = "EUR", **kwargs: Any) -> PaymentResult:
        """Ko-fi doesn't support payouts."""
        return PaymentResult(
            success=False,
            status=PaymentStatus.FAILED,
            error="Ko-fi does not support payouts.",
        )
