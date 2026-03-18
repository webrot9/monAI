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
    _to_decimal,
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

        If ``variant_id`` is provided in metadata, uses that existing variant.
        Otherwise, auto-creates a product and variant from the intent fields,
        making the provider fully self-service.
        """
        variant_id = intent.metadata.get("variant_id")

        if not variant_id:
            # Auto-create product + variant so callers don't need to pre-provision.
            product_name = intent.product or intent.metadata.get("product_name", "Product")
            description = intent.metadata.get("description", "")
            try:
                created = await self.create_product(
                    name=product_name,
                    description=description,
                    price_cents=intent.amount_cents,
                )
                variant_id = created["variant"].get("id", "")
            except (LemonSqueezyAPIError, KeyError) as e:
                return PaymentResult(
                    success=False,
                    error=f"Failed to auto-create product/variant: {e}",
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

        amount = _to_decimal(attrs.get("total", 0)) / 100
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
        """Get store net revenue: paid orders minus refunded orders.

        LS doesn't have a balance endpoint, so we sum orders and subtract
        refunded ones to get an accurate sweepable amount.
        """
        try:
            result = await self._api_call(
                "GET", f"orders",
                {"filter[store_id]": self.store_id, "page[size]": "100"},
            )
            orders = result.get("data", [])
            paid_total = _to_decimal(0)
            refunded_total = _to_decimal(0)
            for o in orders:
                attrs = o.get("attributes", {})
                amount = _to_decimal(attrs.get("total", 0)) / 100
                status = attrs.get("status", "")
                if status == "paid":
                    paid_total += amount
                elif status == "refunded":
                    refunded_total += amount

            net = paid_total - refunded_total
            return ProviderBalance(
                available=max(net, _to_decimal(0)),
                pending=_to_decimal(0),
                currency="USD",
                provider=self.provider_name,
                account_id=account_id or self.store_id,
            )
        except LemonSqueezyAPIError:
            return ProviderBalance(provider=self.provider_name)

    async def handle_webhook(self, payload: bytes,
                             headers: dict[str, str]) -> WebhookEvent | None:
        """Parse and verify Lemon Squeezy webhook."""
        if not self.webhook_secret:
            logger.error("LemonSqueezy webhook_secret not configured — rejecting webhook")
            return None
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
        amount = _to_decimal(attrs.get("total", 0)) / 100
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

    # ── Product & Variant Management ─────────────────────────

    async def create_product(
        self,
        name: str,
        description: str = "",
        price_cents: int = 0,
        store_id: str = "",
    ) -> dict:
        """Create a product AND its default variant on Lemon Squeezy.

        LS auto-creates one variant per product. We then update that
        variant with the requested price.

        Args:
            name: Product name shown to customers.
            description: Product description (HTML supported).
            price_cents: Price in cents (e.g. 999 = $9.99).
            store_id: Override store ID (defaults to self.store_id).

        Returns:
            Dict with ``product`` and ``variant`` API response data.
        """
        sid = store_id or self.store_id
        product_body = {
            "data": {
                "type": "products",
                "attributes": {
                    "name": name,
                    "description": description or name,
                },
                "relationships": {
                    "store": {
                        "data": {"type": "stores", "id": str(sid)},
                    },
                },
            },
        }

        product_result = await self._api_call("POST", "products", product_body)
        product_data = product_result.get("data", {})
        product_id = product_data.get("id", "")

        logger.info(f"Created LS product '{name}' (id={product_id})")

        # Fetch the auto-created default variant for this product.
        variants_result = await self._api_call(
            "GET", "variants", {"filter[product_id]": product_id},
        )
        variants = variants_result.get("data", [])
        if not variants:
            raise LemonSqueezyAPIError(
                f"Product {product_id} created but no default variant found"
            )

        variant_data = variants[0]
        variant_id = variant_data.get("id", "")

        # Update the variant with the correct price.
        if price_cents:
            variant_body = {
                "data": {
                    "type": "variants",
                    "id": str(variant_id),
                    "attributes": {
                        "price": price_cents,
                    },
                },
            }
            variant_data = (
                await self._api_call("PATCH", f"variants/{variant_id}", variant_body)
            ).get("data", variant_data)
            logger.info(
                f"Updated LS variant {variant_id} price to {price_cents} cents"
            )

        return {"product": product_data, "variant": variant_data}

    async def list_products(self) -> list[dict]:
        """List all products in the configured store.

        Returns:
            List of product resource objects from the JSON API response.
        """
        result = await self._api_call(
            "GET", "products", {"filter[store_id]": self.store_id},
        )
        return result.get("data", [])

    async def health_check(self) -> bool:
        try:
            await self._api_call("GET", "users/me")
            return True
        except Exception:
            return False


class LemonSqueezyAPIError(Exception):
    pass
