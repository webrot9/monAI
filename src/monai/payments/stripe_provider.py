"""Stripe payment provider — card payments under brand identity.

Each brand registers its own Stripe Connect account (Standard or Express).
Payments are collected by the brand, then crypto-swept to the creator.

Requires: pip install stripe
Stripe API key stored in brand_payment_accounts.metadata as JSON.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from monai.payments.base import PaymentProvider
from monai.payments.types import (
    PaymentIntent,
    PaymentResult,
    PaymentStatus,
    ProviderBalance,
    WebhookEvent,
    WebhookEventType,
)

logger = logging.getLogger(__name__)

STRIPE_API_BASE = "https://api.stripe.com/v1"


class StripeProvider(PaymentProvider):
    """Stripe integration for card payments under brand identities.

    Uses Stripe's REST API directly via httpx (no SDK dependency).
    Each brand has its own API key stored in the DB.
    """

    provider_name = "stripe"

    def __init__(
        self,
        api_key: str,
        webhook_secret: str = "",
        proxy_url: str = "",
    ):
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.proxy_url = proxy_url

    def _get_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": 30.0,
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        }
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        return httpx.AsyncClient(**kwargs)

    async def _api_call(self, method: str, endpoint: str,
                        data: dict | None = None) -> dict:
        """Make a Stripe API call."""
        url = f"{STRIPE_API_BASE}/{endpoint.lstrip('/')}"
        async with self._get_client() as client:
            if method == "GET":
                resp = await client.get(url, params=data)
            else:
                resp = await client.post(url, data=data)

            result = resp.json()
            if resp.status_code >= 400:
                error = result.get("error", {})
                raise StripeAPIError(
                    error.get("type", "api_error"),
                    error.get("message", f"HTTP {resp.status_code}"),
                    error.get("code", ""),
                )
            return result

    async def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Create a Stripe Checkout Session (payment link)."""
        amount_cents = int(intent.amount * 100)
        currency = intent.currency.lower()

        line_items = {
            "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][product_data][name]": intent.product or "Payment",
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][quantity]": "1",
            "mode": "payment",
            "success_url": intent.metadata.get("success_url", "https://example.com/success"),
            "cancel_url": intent.metadata.get("cancel_url", "https://example.com/cancel"),
        }

        if intent.customer_email:
            line_items["customer_email"] = intent.customer_email

        # Add metadata for brand attribution
        if intent.brand:
            line_items["metadata[brand]"] = intent.brand
        if intent.metadata.get("lead_id"):
            line_items["metadata[lead_id]"] = str(intent.metadata["lead_id"])

        try:
            session = await self._api_call("POST", "checkout/sessions", line_items)
        except StripeAPIError as e:
            return PaymentResult(success=False, error=str(e))

        return PaymentResult(
            success=True,
            payment_ref=session["id"],
            amount=intent.amount,
            currency=intent.currency,
            status=PaymentStatus.PENDING,
            checkout_url=session.get("url", ""),
            raw=session,
        )

    async def verify_payment(self, payment_ref: str) -> PaymentResult:
        """Verify a payment by Checkout Session ID or Payment Intent ID."""
        try:
            if payment_ref.startswith("cs_"):
                data = await self._api_call("GET", f"checkout/sessions/{payment_ref}")
                status = self._map_session_status(data.get("payment_status", ""))
                amount = (data.get("amount_total", 0) or 0) / 100
                currency = (data.get("currency", "eur") or "eur").upper()
            elif payment_ref.startswith("pi_"):
                data = await self._api_call("GET", f"payment_intents/{payment_ref}")
                status = self._map_intent_status(data.get("status", ""))
                amount = (data.get("amount", 0) or 0) / 100
                currency = (data.get("currency", "eur") or "eur").upper()
            else:
                # Try as charge ID
                data = await self._api_call("GET", f"charges/{payment_ref}")
                status = PaymentStatus.COMPLETED if data.get("paid") else PaymentStatus.FAILED
                amount = (data.get("amount", 0) or 0) / 100
                currency = (data.get("currency", "eur") or "eur").upper()
        except StripeAPIError as e:
            return PaymentResult(success=False, error=str(e))

        return PaymentResult(
            success=True,
            payment_ref=payment_ref,
            amount=amount,
            currency=currency,
            status=status,
            raw=data,
        )

    async def get_balance(self, account_id: str = "") -> ProviderBalance:
        """Get Stripe account balance."""
        try:
            data = await self._api_call("GET", "balance")
        except StripeAPIError as e:
            return ProviderBalance(provider=self.provider_name, account_id=account_id)

        available = 0.0
        pending = 0.0
        currency = "EUR"

        for bal in data.get("available", []):
            available += bal.get("amount", 0) / 100
            currency = bal.get("currency", "eur").upper()

        for bal in data.get("pending", []):
            pending += bal.get("amount", 0) / 100

        return ProviderBalance(
            available=available,
            pending=pending,
            currency=currency,
            provider=self.provider_name,
            account_id=account_id,
        )

    async def handle_webhook(self, payload: bytes,
                             headers: dict[str, str]) -> WebhookEvent | None:
        """Verify and parse a Stripe webhook event."""
        if self.webhook_secret:
            sig_header = headers.get("stripe-signature", "")
            if not self._verify_signature(payload, sig_header):
                logger.warning("Invalid Stripe webhook signature")
                return None

        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return None

        event_type = event.get("type", "")
        obj = event.get("data", {}).get("object", {})

        type_map = {
            "checkout.session.completed": WebhookEventType.PAYMENT_COMPLETED,
            "payment_intent.succeeded": WebhookEventType.PAYMENT_COMPLETED,
            "payment_intent.payment_failed": WebhookEventType.PAYMENT_FAILED,
            "charge.refunded": WebhookEventType.PAYMENT_REFUNDED,
            "charge.dispute.created": WebhookEventType.PAYMENT_DISPUTED,
            "payout.paid": WebhookEventType.PAYOUT_COMPLETED,
        }

        wh_type = type_map.get(event_type)
        if not wh_type:
            return None

        # Extract amount based on event type
        if event_type == "checkout.session.completed":
            amount = (obj.get("amount_total", 0) or 0) / 100
            payment_ref = obj.get("id", "")
            customer_email = obj.get("customer_email", "") or ""
        else:
            amount = (obj.get("amount", 0) or 0) / 100
            payment_ref = obj.get("id", "")
            customer_email = ""

        currency = (obj.get("currency", "eur") or "eur").upper()
        metadata = obj.get("metadata", {})

        return WebhookEvent(
            event_type=wh_type,
            provider=self.provider_name,
            payment_ref=payment_ref,
            amount=amount,
            currency=currency,
            customer_email=customer_email,
            product=metadata.get("product", ""),
            metadata=metadata,
            raw=event,
        )

    async def send_payout(self, to_address: str, amount: float,
                          currency: str = "EUR", **kwargs: Any) -> PaymentResult:
        """Stripe doesn't support arbitrary payouts.

        Payouts go to the connected bank account automatically.
        This is a no-op — sweeping is done via crypto.
        """
        return PaymentResult(
            success=False,
            error="Use crypto sweep instead of Stripe payout",
        )

    async def create_payment_link(self, amount: float, product: str,
                                  currency: str = "EUR") -> str:
        """Create a reusable Stripe Payment Link."""
        amount_cents = int(amount * 100)

        # First create a product
        product_data = await self._api_call("POST", "products", {
            "name": product,
        })

        # Create a price
        price_data = await self._api_call("POST", "prices", {
            "unit_amount": str(amount_cents),
            "currency": currency.lower(),
            "product": product_data["id"],
        })

        # Create payment link
        link_data = await self._api_call("POST", "payment_links", {
            "line_items[0][price]": price_data["id"],
            "line_items[0][quantity]": "1",
        })

        return link_data.get("url", "")

    def _verify_signature(self, payload: bytes, sig_header: str) -> bool:
        """Verify Stripe webhook signature (v1 scheme)."""
        if not sig_header or not self.webhook_secret:
            return False

        parts = {}
        for item in sig_header.split(","):
            key, _, value = item.strip().partition("=")
            parts[key] = value

        timestamp = parts.get("t", "")
        signature = parts.get("v1", "")

        if not timestamp or not signature:
            return False

        # Check timestamp tolerance (5 minutes)
        try:
            ts = int(timestamp)
        except ValueError:
            return False
        if abs(time.time() - ts) > 300:
            return False

        signed_payload = f"{timestamp}.".encode() + payload
        expected = hmac.new(
            self.webhook_secret.encode(),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    @staticmethod
    def _map_session_status(status: str) -> PaymentStatus:
        return {
            "paid": PaymentStatus.COMPLETED,
            "unpaid": PaymentStatus.PENDING,
            "no_payment_required": PaymentStatus.COMPLETED,
        }.get(status, PaymentStatus.PENDING)

    @staticmethod
    def _map_intent_status(status: str) -> PaymentStatus:
        return {
            "succeeded": PaymentStatus.COMPLETED,
            "processing": PaymentStatus.PENDING,
            "requires_payment_method": PaymentStatus.PENDING,
            "requires_confirmation": PaymentStatus.PENDING,
            "requires_action": PaymentStatus.PENDING,
            "canceled": PaymentStatus.FAILED,
        }.get(status, PaymentStatus.PENDING)

    async def health_check(self) -> bool:
        try:
            await self._api_call("GET", "balance")
            return True
        except Exception:
            return False


class StripeAPIError(Exception):
    def __init__(self, error_type: str, message: str, code: str = ""):
        self.error_type = error_type
        self.code = code
        super().__init__(f"Stripe {error_type}: {message} (code={code})")
