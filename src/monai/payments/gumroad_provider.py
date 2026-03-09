"""Gumroad payment provider — digital product sales platform.

Gumroad handles all payment processing (merchant of record).
We receive webhook pings when sales happen and track payouts.

API docs: https://app.gumroad.com/api
Webhooks: Gumroad sends POST to our webhook URL on each sale.
"""

from __future__ import annotations

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

GUMROAD_API_BASE = "https://api.gumroad.com/v2"


class GumroadProvider(PaymentProvider):
    """Gumroad integration for digital product sales."""

    provider_name = "gumroad"

    def __init__(self, access_token: str, proxy_url: str = ""):
        self.access_token = access_token
        self.proxy_url = proxy_url

    def _get_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": 30.0}
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        return httpx.AsyncClient(**kwargs)

    async def _api_call(self, method: str, endpoint: str,
                        data: dict | None = None) -> dict:
        url = f"{GUMROAD_API_BASE}/{endpoint.lstrip('/')}"
        params = {"access_token": self.access_token}
        if data:
            params.update(data)

        async with self._get_client() as client:
            if method == "GET":
                resp = await client.get(url, params=params)
            else:
                resp = await client.post(url, data=params)

            result = resp.json()
            if not result.get("success", False):
                raise GumroadAPIError(result.get("message", f"HTTP {resp.status_code}"))
            return result

    async def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Create a Gumroad product (products are the "payment link" on Gumroad).

        Creating a product = creating a way for customers to pay.
        """
        data = {
            "name": intent.product or "Digital Product",
            "price": int(intent.amount * 100),  # cents
        }
        if intent.metadata.get("description"):
            data["description"] = intent.metadata["description"]

        try:
            result = await self._api_call("POST", "products", data)
            product = result.get("product", {})
        except GumroadAPIError as e:
            return PaymentResult(success=False, error=str(e))

        return PaymentResult(
            success=True,
            payment_ref=product.get("id", ""),
            amount=intent.amount,
            currency=intent.currency,
            status=PaymentStatus.PENDING,
            checkout_url=product.get("short_url", ""),
            raw=product,
        )

    async def verify_payment(self, payment_ref: str) -> PaymentResult:
        """Verify a sale by its ID."""
        try:
            result = await self._api_call("GET", f"sales/{payment_ref}")
            sale = result.get("sale", {})
        except GumroadAPIError as e:
            return PaymentResult(success=False, error=str(e))

        price = float(sale.get("price", 0)) / 100
        refunded = sale.get("refunded", False)

        if refunded:
            status = PaymentStatus.REFUNDED
        else:
            status = PaymentStatus.COMPLETED

        return PaymentResult(
            success=True,
            payment_ref=payment_ref,
            amount=price,
            currency=sale.get("currency", "usd").upper(),
            status=status,
            raw=sale,
        )

    async def get_balance(self, account_id: str = "") -> ProviderBalance:
        """Get total sales balance (Gumroad doesn't expose wallet balance directly)."""
        try:
            result = await self._api_call("GET", "sales")
            sales = result.get("sales", [])

            total = sum(
                float(s.get("price", 0)) / 100
                for s in sales
                if not s.get("refunded", False)
            )
            return ProviderBalance(
                available=total,
                pending=0,
                currency="USD",
                provider=self.provider_name,
                account_id=account_id,
            )
        except GumroadAPIError:
            return ProviderBalance(provider=self.provider_name)

    async def handle_webhook(self, payload: bytes,
                             headers: dict[str, str]) -> WebhookEvent | None:
        """Parse Gumroad webhook (ping).

        Gumroad sends form-encoded POST data on each sale.
        """
        try:
            # Gumroad sends form-encoded data
            from urllib.parse import parse_qs
            data = {k: v[0] for k, v in parse_qs(payload.decode("utf-8")).items()}
        except Exception:
            return None

        if not data:
            return None

        # Determine event type from resource_name
        resource = data.get("resource_name", "sale")
        refunded = data.get("refunded", "false").lower() == "true"
        disputed = data.get("disputed", "false").lower() == "true"

        if disputed:
            event_type = WebhookEventType.PAYMENT_DISPUTED
        elif refunded:
            event_type = WebhookEventType.PAYMENT_REFUNDED
        elif resource == "sale":
            event_type = WebhookEventType.PAYMENT_COMPLETED
        else:
            return None

        price = float(data.get("price", 0)) / 100
        currency = data.get("currency", "usd").upper()

        return WebhookEvent(
            event_type=event_type,
            provider=self.provider_name,
            payment_ref=data.get("sale_id", ""),
            amount=price,
            currency=currency,
            customer_email=data.get("email", ""),
            product=data.get("product_name", ""),
            metadata={
                "product_id": data.get("product_id", ""),
                "seller_id": data.get("seller_id", ""),
                "order_number": data.get("order_number", ""),
            },
            raw=data,
        )

    async def send_payout(self, to_address: str, amount: float,
                          currency: str = "USD", **kwargs: Any) -> PaymentResult:
        """Gumroad handles payouts to connected bank automatically."""
        return PaymentResult(
            success=False,
            error="Gumroad handles payouts to bank automatically. Use crypto sweep.",
        )

    async def get_products(self) -> list[dict[str, Any]]:
        """List all products on the Gumroad account."""
        result = await self._api_call("GET", "products")
        return result.get("products", [])

    async def health_check(self) -> bool:
        try:
            await self._api_call("GET", "user")
            return True
        except Exception:
            return False


class GumroadAPIError(Exception):
    pass
