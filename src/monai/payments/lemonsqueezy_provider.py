"""Lemon Squeezy payment provider — SaaS and digital product payments.

Lemon Squeezy is a merchant of record: they handle tax, billing,
and compliance. We receive webhooks on sales/subscriptions.

API docs: https://docs.lemonsqueezy.com/api
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
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

LS_API_BASE = "https://api.lemonsqueezy.com/v1"


class LemonSqueezyProvider(PaymentProvider):
    """Lemon Squeezy integration for SaaS and digital product payments."""

    provider_name = "lemonsqueezy"

    def __init__(
        self,
        api_key: str,
        store_id: str,
        webhook_secret: str = "",
        proxy_url: str = "",
    ):
        self.api_key = api_key
        self.store_id = store_id
        self.webhook_secret = webhook_secret
        self.proxy_url = proxy_url
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {
                "timeout": 30.0,
                "headers": {
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/vnd.api+json",
                    "Content-Type": "application/vnd.api+json",
                },
            }
            from monai.payments.base import _resolve_proxy_url
            proxy = _resolve_proxy_url(self.proxy_url)
            if proxy:
                kwargs["proxy"] = proxy
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def _api_call(self, method: str, endpoint: str,
                        data: dict | None = None) -> dict:
        url = f"{LS_API_BASE}/{endpoint.lstrip('/')}"
        client = self._get_client()
        if method == "GET":
            resp = await client.get(url, params=data)
        elif method == "POST":
            resp = await client.post(url, json=data)
        elif method == "PATCH":
            resp = await client.patch(url, json=data)
        else:
            resp = await client.request(method, url, json=data)

        if resp.status_code >= 400:
            raise LemonSqueezyAPIError(f"HTTP {resp.status_code}: {resp.text}")

        return resp.json() if resp.text else {}

    async def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Create a checkout link via Lemon Squeezy API.

        Requires an existing product/variant. The checkout URL
        is what customers use to pay.
        """
        variant_id = intent.metadata.get("variant_id")
        if not variant_id:
            return PaymentResult(
                success=False,
                error="variant_id required in metadata for Lemon Squeezy checkout",
            )

        checkout_data = {
            "data": {
                "type": "checkouts",
                "attributes": {
                    "checkout_data": {
                        "email": intent.customer_email or "",
                        "custom": {
                            "brand": intent.brand,
                        },
                    },
                },
                "relationships": {
                    "store": {
                        "data": {"type": "stores", "id": self.store_id},
                    },
                    "variant": {
                        "data": {"type": "variants", "id": str(variant_id)},
                    },
                },
            },
        }

        try:
            result = await self._api_call("POST", "checkouts", checkout_data)
            attrs = result.get("data", {}).get("attributes", {})
            checkout_url = attrs.get("url", "")
        except LemonSqueezyAPIError as e:
            return PaymentResult(success=False, error=str(e))

        return PaymentResult(
            success=True,
            payment_ref=result.get("data", {}).get("id", ""),
            amount=intent.amount,
            currency=intent.currency,
            status=PaymentStatus.PENDING,
            checkout_url=checkout_url,
            raw=result,
        )

    async def verify_payment(self, payment_ref: str) -> PaymentResult:
        """Verify an order by ID."""
        try:
            result = await self._api_call("GET", f"orders/{payment_ref}")
            attrs = result.get("data", {}).get("attributes", {})
        except LemonSqueezyAPIError as e:
            return PaymentResult(success=False, error=str(e))

        status_str = attrs.get("status", "")
        if status_str == "paid":
            status = PaymentStatus.COMPLETED
        elif status_str == "refunded":
            status = PaymentStatus.REFUNDED
        else:
            status = PaymentStatus.PENDING

        amount = float(attrs.get("total", 0)) / 100
        currency = attrs.get("currency", "USD").upper()

        return PaymentResult(
            success=True,
            payment_ref=payment_ref,
            amount=amount,
            currency=currency,
            status=status,
            raw=result,
        )

    async def get_balance(self, account_id: str = "") -> ProviderBalance:
        """Get store revenue (LS doesn't have a balance endpoint, we sum orders)."""
        try:
            result = await self._api_call(
                "GET", f"orders",
                {"filter[store_id]": self.store_id, "page[size]": "100"},
            )
            orders = result.get("data", [])
            total = sum(
                float(o.get("attributes", {}).get("total", 0)) / 100
                for o in orders
                if o.get("attributes", {}).get("status") == "paid"
            )
            return ProviderBalance(
                available=total,
                currency="USD",
                provider=self.provider_name,
                account_id=account_id or self.store_id,
            )
        except LemonSqueezyAPIError:
            return ProviderBalance(provider=self.provider_name)

    async def handle_webhook(self, payload: bytes,
                             headers: dict[str, str]) -> WebhookEvent | None:
        """Parse and verify Lemon Squeezy webhook."""
        if self.webhook_secret:
            sig = headers.get("x-signature", "")
            if not self._verify_signature(payload, sig):
                logger.warning("Invalid Lemon Squeezy webhook signature")
                return None

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None

        event_name = data.get("meta", {}).get("event_name", "")
        type_map = {
            "order_created": WebhookEventType.PAYMENT_COMPLETED,
            "order_refunded": WebhookEventType.PAYMENT_REFUNDED,
            "subscription_created": WebhookEventType.SUBSCRIPTION_CREATED,
            "subscription_cancelled": WebhookEventType.SUBSCRIPTION_CANCELLED,
            "subscription_payment_success": WebhookEventType.PAYMENT_COMPLETED,
            "subscription_payment_failed": WebhookEventType.PAYMENT_FAILED,
        }

        wh_type = type_map.get(event_name)
        if not wh_type:
            return None

        attrs = data.get("data", {}).get("attributes", {})
        order_id = str(data.get("data", {}).get("id", ""))
        amount = float(attrs.get("total", 0)) / 100
        currency = attrs.get("currency", "USD").upper()
        customer_email = attrs.get("user_email", "")

        custom_data = data.get("meta", {}).get("custom_data", {})

        return WebhookEvent(
            event_type=wh_type,
            provider=self.provider_name,
            payment_ref=order_id,
            amount=amount,
            currency=currency,
            customer_email=customer_email,
            product=attrs.get("product_name", ""),
            metadata=custom_data,
            raw=data,
        )

    async def send_payout(self, to_address: str, amount: float,
                          currency: str = "USD", **kwargs: Any) -> PaymentResult:
        """LS handles payouts to connected bank automatically."""
        return PaymentResult(
            success=False,
            error="Lemon Squeezy handles payouts automatically. Use crypto sweep.",
        )

    def _verify_signature(self, payload: bytes, signature: str) -> bool:
        """Verify HMAC-SHA256 webhook signature."""
        if not signature or not self.webhook_secret:
            return False

        expected = hmac.new(
            self.webhook_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    async def health_check(self) -> bool:
        try:
            await self._api_call("GET", "users/me")
            return True
        except Exception:
            return False


class LemonSqueezyAPIError(Exception):
    pass
