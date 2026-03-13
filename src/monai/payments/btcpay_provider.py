"""BTCPay Server provider — self-hosted crypto payment processing.

BTCPay Server is a free, open-source payment processor.
No KYC, no third party, fully self-hosted. Accepts BTC + Lightning.

API docs: https://docs.btcpayserver.org/API/Greenfield/v1/
Uses REST API via httpx — no SDK dependency.
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


class BTCPayProvider(PaymentProvider):
    """BTCPay Server Greenfield API integration."""

    provider_name = "btcpay"

    def __init__(
        self,
        server_url: str,
        api_key: str,
        store_id: str,
        webhook_secret: str = "",
        proxy_url: str = "",
    ):
        self.server_url = server_url.rstrip("/")
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
                    "Authorization": f"token {self.api_key}",
                    "Content-Type": "application/json",
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
        url = f"{self.server_url}/api/v1/{endpoint.lstrip('/')}"
        client = self._get_client()
        if method == "GET":
            resp = await client.get(url, params=data)
        elif method == "POST":
            resp = await client.post(url, json=data)
        elif method == "DELETE":
            resp = await client.delete(url)
        else:
            resp = await client.request(method, url, json=data)

        if resp.status_code >= 400:
            text = resp.text
            raise BTCPayAPIError(f"HTTP {resp.status_code}: {text}")

        if resp.status_code == 204:
            return {}
        return resp.json()

    async def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Create a BTCPay invoice (payment request)."""
        invoice_data: dict[str, Any] = {
            "amount": intent.amount,
            "currency": intent.currency,
        }

        metadata: dict[str, Any] = {}
        if intent.product:
            metadata["itemDesc"] = intent.product
        if intent.brand:
            metadata["brand"] = intent.brand
        if intent.customer_email:
            metadata["buyerEmail"] = intent.customer_email
        if intent.metadata.get("lead_id"):
            metadata["leadId"] = str(intent.metadata["lead_id"])

        if metadata:
            invoice_data["metadata"] = metadata

        checkout: dict[str, Any] = {}
        if intent.metadata.get("redirect_url"):
            checkout["redirectURL"] = intent.metadata["redirect_url"]
        if checkout:
            invoice_data["checkout"] = checkout

        try:
            result = await self._api_call(
                "POST", f"stores/{self.store_id}/invoices", invoice_data
            )
        except BTCPayAPIError as e:
            return PaymentResult(success=False, error=str(e))

        return PaymentResult(
            success=True,
            payment_ref=result.get("id", ""),
            amount=intent.amount,
            currency=intent.currency,
            status=PaymentStatus.PENDING,
            checkout_url=result.get("checkoutLink", ""),
            raw=result,
        )

    async def verify_payment(self, payment_ref: str) -> PaymentResult:
        """Verify invoice status by ID."""
        try:
            data = await self._api_call(
                "GET", f"stores/{self.store_id}/invoices/{payment_ref}"
            )
        except BTCPayAPIError as e:
            return PaymentResult(success=False, error=str(e))

        status = self._map_status(data.get("status", ""))
        amount = float(data.get("amount", 0))
        currency = data.get("currency", "EUR")

        return PaymentResult(
            success=True,
            payment_ref=payment_ref,
            amount=amount,
            currency=currency,
            status=status,
            raw=data,
        )

    async def get_balance(self, account_id: str = "") -> ProviderBalance:
        """Get on-chain wallet balance from BTCPay.

        BTCPay doesn't have a traditional "balance" — it's per-wallet.
        """
        try:
            # Get the store's BTC wallet balance
            data = await self._api_call(
                "GET", f"stores/{self.store_id}/payment-methods/BTC-CHAIN/wallet"
            )
            confirmed = float(data.get("confirmedBalance", 0))
            unconfirmed = float(data.get("unconfirmedBalance", 0))

            return ProviderBalance(
                available=confirmed,
                pending=unconfirmed,
                currency="BTC",
                provider=self.provider_name,
                account_id=account_id or self.store_id,
            )
        except BTCPayAPIError:
            return ProviderBalance(
                provider=self.provider_name,
                account_id=account_id or self.store_id,
            )

    async def handle_webhook(self, payload: bytes,
                             headers: dict[str, str]) -> WebhookEvent | None:
        """Parse BTCPay webhook event."""
        if not self.webhook_secret:
            logger.error("BTCPay webhook_secret not configured — rejecting webhook")
            return None
        sig = headers.get("btcpay-sig", "")
        if not self._verify_signature(payload, sig):
            logger.warning("Invalid BTCPay webhook signature")
            return None

        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return None

        event_type = event.get("type", "")
        type_map = {
            "InvoiceSettled": WebhookEventType.PAYMENT_COMPLETED,
            "InvoicePaymentSettled": WebhookEventType.PAYMENT_COMPLETED,
            "InvoiceProcessing": WebhookEventType.PAYMENT_COMPLETED,
            "InvoiceExpired": WebhookEventType.PAYMENT_FAILED,
            "InvoiceInvalid": WebhookEventType.PAYMENT_FAILED,
        }

        wh_type = type_map.get(event_type)
        if not wh_type:
            return None

        invoice_id = event.get("invoiceId", "")
        metadata = event.get("metadata", {})

        # Fetch full invoice to get amount
        amount = 0.0
        currency = "BTC"
        try:
            invoice = await self._api_call(
                "GET", f"stores/{self.store_id}/invoices/{invoice_id}"
            )
            amount = float(invoice.get("amount", 0))
            currency = invoice.get("currency", "BTC")
        except BTCPayAPIError as e:
            logger.warning(
                "Failed to fetch BTCPay invoice %s details: %s", invoice_id, e
            )

        return WebhookEvent(
            event_type=wh_type,
            provider=self.provider_name,
            payment_ref=invoice_id,
            amount=amount,
            currency=currency,
            metadata=metadata,
            raw=event,
        )

    @staticmethod
    def validate_btc_address(address: str) -> bool:
        """Validate Bitcoin address format before sending.

        Supports:
        - Legacy (P2PKH): starts with 1, 25-34 chars
        - P2SH: starts with 3, 25-34 chars
        - Bech32 (SegWit): starts with bc1, 42-62 chars
        """
        import re
        if not address:
            return False
        # Bech32 / Bech32m (native SegWit)
        if address.startswith("bc1"):
            return bool(re.match(r'^bc1[a-z0-9]{39,59}$', address))
        # Legacy (1...) or P2SH (3...)
        if address[0] in ('1', '3'):
            return bool(re.match(r'^[13][a-km-zA-HJ-NP-Z1-9]{24,33}$', address))
        return False

    async def send_payout(self, to_address: str, amount: float,
                          currency: str = "BTC", **kwargs: Any) -> PaymentResult:
        """Send BTC from BTCPay wallet to an address."""
        # Validate address format BEFORE attempting send (irreversible!)
        if not self.validate_btc_address(to_address):
            return PaymentResult(
                success=False,
                error=f"Invalid Bitcoin address format: {to_address[:20]}...",
            )

        try:
            # Create a transaction
            tx_data: dict[str, Any] = {
                "destinations": [
                    {"destination": to_address, "amount": str(amount)},
                ],
            }

            fee_rate = kwargs.get("fee_rate")
            if fee_rate:
                tx_data["feeRate"] = fee_rate

            result = await self._api_call(
                "POST",
                f"stores/{self.store_id}/payment-methods/BTC-CHAIN/wallet/transactions",
                tx_data,
            )

            tx_id = result.get("transactionHash", "")
            logger.info(f"BTC sent via BTCPay: {amount} BTC to {to_address[:12]}... tx={tx_id[:16]}...")

            return PaymentResult(
                success=True,
                payment_ref=tx_id,
                amount=amount,
                currency="BTC",
                status=PaymentStatus.PENDING,
                raw=result,
            )
        except BTCPayAPIError as e:
            return PaymentResult(success=False, error=str(e))

    async def create_webhook(self, url: str, events: list[str] | None = None) -> dict:
        """Register a webhook URL with BTCPay Server."""
        if events is None:
            events = [
                "InvoiceSettled",
                "InvoicePaymentSettled",
                "InvoiceExpired",
                "InvoiceInvalid",
            ]

        data: dict[str, Any] = {
            "url": url,
            "enabled": True,
            "events": events,
        }
        if self.webhook_secret:
            data["secret"] = self.webhook_secret

        return await self._api_call(
            "POST", f"stores/{self.store_id}/webhooks", data
        )

    def _verify_signature(self, payload: bytes, sig_header: str) -> bool:
        """Verify BTCPay webhook HMAC-SHA256 signature."""
        if not sig_header or not self.webhook_secret:
            return False

        # BTCPay sends: sha256=<hex>
        prefix = "sha256="
        if not sig_header.startswith(prefix):
            return False

        provided_sig = sig_header[len(prefix):]
        expected = hmac.new(
            self.webhook_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, provided_sig)

    @staticmethod
    def _map_status(status: str) -> PaymentStatus:
        return {
            "New": PaymentStatus.PENDING,
            "Processing": PaymentStatus.PENDING,
            "Settled": PaymentStatus.COMPLETED,
            "Expired": PaymentStatus.EXPIRED,
            "Invalid": PaymentStatus.FAILED,
        }.get(status, PaymentStatus.PENDING)

    async def health_check(self) -> bool:
        try:
            await self._api_call("GET", f"stores/{self.store_id}")
            return True
        except Exception:
            return False


class BTCPayAPIError(Exception):
    pass
