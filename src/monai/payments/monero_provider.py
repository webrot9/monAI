"""Monero payment provider — privacy-first cryptocurrency integration.

Uses Monero wallet RPC (monero-wallet-rpc) for all operations.
Each brand gets its own subaddress for payment separation.
Sweeps go directly XMR→XMR to the creator's wallet — untraceable by design.

Requires a running monero-wallet-rpc instance:
    monero-wallet-rpc --rpc-bind-port 18082 --wallet-file brand_wallet \
        --password '' --disable-rpc-login
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from monai.payments.base import CryptoProvider
from monai.payments.types import (
    PaymentIntent,
    PaymentResult,
    PaymentStatus,
    ProviderBalance,
    WebhookEvent,
    WebhookEventType,
)

logger = logging.getLogger(__name__)

# Monero has 12 decimal places (piconero)
ATOMIC_UNITS_PER_XMR = 1_000_000_000_000

# Minimum confirmations before considering payment settled
DEFAULT_MIN_CONFIRMATIONS = 10


class MoneroProvider(CryptoProvider):
    """Monero wallet RPC integration for receiving and sending XMR."""

    provider_name = "crypto_xmr"

    def __init__(
        self,
        wallet_rpc_url: str = "http://127.0.0.1:18082",
        rpc_user: str = "",
        rpc_password: str = "",
        min_confirmations: int = DEFAULT_MIN_CONFIRMATIONS,
        proxy_url: str = "",
    ):
        self.wallet_rpc_url = wallet_rpc_url.rstrip("/")
        self.rpc_user = rpc_user
        self.rpc_password = rpc_password
        self.min_confirmations = min_confirmations
        self.proxy_url = proxy_url
        self._request_id = 0
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create httpx client with optional proxy for Tor routing."""
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {"timeout": 30.0}
            from monai.payments.base import _resolve_proxy_url
            proxy = _resolve_proxy_url(self.proxy_url)
            if proxy:
                kwargs["proxy"] = proxy
            if self.rpc_user and self.rpc_password:
                kwargs["auth"] = (self.rpc_user, self.rpc_password)
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def _rpc_call(self, method: str, params: dict | None = None) -> dict:
        """Make a JSON-RPC 2.0 call to monero-wallet-rpc."""
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": str(self._request_id),
            "method": method,
        }
        if params:
            payload["params"] = params

        client = self._get_client()
        resp = await client.post(
            f"{self.wallet_rpc_url}/json_rpc",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            err = data["error"]
            raise MoneroRPCError(err.get("code", -1), err.get("message", "Unknown RPC error"))

        return data.get("result", {})

    # ── CryptoProvider interface ────────────────────────────────

    async def generate_address(self, label: str = "") -> str:
        """Generate a new subaddress for receiving payments.

        Each brand/product gets its own subaddress so payments are separated
        but all land in the same wallet.
        """
        params: dict[str, Any] = {"account_index": 0}
        if label:
            params["label"] = label
        result = await self._rpc_call("create_address", params)
        address = result["address"]
        logger.info(f"Generated XMR subaddress: {address[:12]}... (label: {label})")
        return address

    async def get_tx_confirmations(self, tx_hash: str) -> int:
        """Get confirmations for a specific transaction."""
        result = await self._rpc_call("get_transfer_by_txid", {"txid": tx_hash})
        transfer = result.get("transfer", {})
        return transfer.get("confirmations", 0)

    async def estimate_fee(self, amount: float, priority: str = "normal") -> float:
        """Estimate fee for sending XMR.

        Priority levels: unimportant(1), normal(2), elevated(3), priority(4)
        """
        priority_map = {
            "low": 1, "unimportant": 1,
            "normal": 2, "default": 2,
            "elevated": 3, "high": 3,
            "priority": 4, "urgent": 4,
        }
        p = priority_map.get(priority, 2)
        amount_atomic = int(amount * ATOMIC_UNITS_PER_XMR)

        # Use a dummy address to estimate — we won't actually send
        result = await self._rpc_call("get_address", {"account_index": 0})
        own_address = result["address"]

        try:
            result = await self._rpc_call("transfer", {
                "destinations": [{"amount": amount_atomic, "address": own_address}],
                "priority": p,
                "do_not_relay": True,  # Don't actually send
                "get_tx_metadata": False,
            })
            fee_atomic = result.get("fee", 0)
            return fee_atomic / ATOMIC_UNITS_PER_XMR
        except MoneroRPCError:
            # Fallback estimate: ~0.00005 XMR typical fee
            return 0.00005

    # ── PaymentProvider interface ───────────────────────────────

    async def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Create a payment request by generating a subaddress.

        For Monero, "creating a payment" means generating a receive address
        the customer sends XMR to. Amount is specified off-band (invoice, UI).
        """
        label = f"{intent.brand}:{intent.product}" if intent.brand else intent.product
        address = await self.generate_address(label=label)

        return PaymentResult(
            success=True,
            payment_ref=address,  # The subaddress IS the payment ref
            amount=intent.amount,
            currency="XMR",
            status=PaymentStatus.PENDING,
            checkout_url=f"monero:{address}?tx_amount={intent.amount}",
            raw={"subaddress": address, "label": label},
        )

    async def verify_payment(self, payment_ref: str) -> PaymentResult:
        """Verify payment by checking incoming transfers to a subaddress or tx hash.

        payment_ref can be either a tx hash or a subaddress.
        """
        # Try as tx hash first (64 hex chars)
        if len(payment_ref) == 64 and all(c in "0123456789abcdef" for c in payment_ref.lower()):
            return await self._verify_by_txid(payment_ref)

        # Otherwise treat as subaddress — check incoming transfers
        return await self._verify_by_address(payment_ref)

    async def _verify_by_txid(self, tx_hash: str) -> PaymentResult:
        """Verify by transaction hash."""
        try:
            result = await self._rpc_call("get_transfer_by_txid", {"txid": tx_hash})
        except MoneroRPCError as e:
            return PaymentResult(success=False, error=str(e))

        transfer = result.get("transfer", {})
        amount_atomic = transfer.get("amount", 0)
        confirmations = transfer.get("confirmations", 0)
        amount_xmr = amount_atomic / ATOMIC_UNITS_PER_XMR

        if confirmations >= self.min_confirmations:
            status = PaymentStatus.COMPLETED
        elif confirmations > 0:
            status = PaymentStatus.PENDING
        else:
            status = PaymentStatus.PENDING

        return PaymentResult(
            success=True,
            payment_ref=tx_hash,
            amount=amount_xmr,
            currency="XMR",
            status=status,
            raw={"confirmations": confirmations, "transfer": transfer},
        )

    async def _verify_by_address(self, address: str) -> PaymentResult:
        """Check incoming transfers to a subaddress."""
        result = await self._rpc_call("get_transfers", {
            "in": True,
            "pending": True,
            "pool": True,
            "filter_by_height": False,
            "subaddr_indices": [],  # All subaddresses
        })

        total = 0
        confirmed = 0
        for tx in result.get("in", []) + result.get("pending", []) + result.get("pool", []):
            if tx.get("address") == address:
                amount = tx.get("amount", 0) / ATOMIC_UNITS_PER_XMR
                total += amount
                if tx.get("confirmations", 0) >= self.min_confirmations:
                    confirmed += amount

        if total == 0:
            return PaymentResult(
                success=True, payment_ref=address, amount=0,
                currency="XMR", status=PaymentStatus.PENDING,
            )

        status = PaymentStatus.COMPLETED if confirmed > 0 else PaymentStatus.PENDING
        return PaymentResult(
            success=True, payment_ref=address, amount=total,
            currency="XMR", status=status,
            raw={"total_received": total, "confirmed": confirmed},
        )

    async def get_balance(self, account_id: str = "") -> ProviderBalance:
        """Get wallet balance."""
        result = await self._rpc_call("get_balance", {"account_index": 0})
        balance = result.get("balance", 0) / ATOMIC_UNITS_PER_XMR
        unlocked = result.get("unlocked_balance", 0) / ATOMIC_UNITS_PER_XMR

        return ProviderBalance(
            available=unlocked,
            pending=balance - unlocked,
            currency="XMR",
            provider=self.provider_name,
            account_id=account_id or "primary",
        )

    async def handle_webhook(self, payload: bytes,
                             headers: dict[str, str]) -> WebhookEvent | None:
        """Monero doesn't use webhooks — we poll instead.

        This method is a no-op. Use poll_incoming() for new payments.
        """
        return None

    async def send_payout(self, to_address: str, amount: float,
                          currency: str = "XMR",
                          priority: str = "normal",
                          **kwargs: Any) -> PaymentResult:
        """Send XMR to an address. This is the actual money transfer.

        This is the core sweep operation — sends XMR from the brand wallet
        to the creator's anonymous wallet.
        """
        priority_map = {
            "low": 1, "normal": 2, "elevated": 3, "priority": 4,
        }
        p = priority_map.get(priority, 2)
        amount_atomic = int(amount * ATOMIC_UNITS_PER_XMR)

        try:
            result = await self._rpc_call("transfer", {
                "destinations": [{"amount": amount_atomic, "address": to_address}],
                "priority": p,
                "ring_size": 16,  # Higher ring size = more privacy
                "get_tx_key": True,
            })
        except MoneroRPCError as e:
            logger.error(f"XMR send failed: {e}")
            return PaymentResult(
                success=False,
                error=str(e),
                amount=amount,
                currency="XMR",
            )

        tx_hash = result.get("tx_hash", "")
        fee_atomic = result.get("fee", 0)
        fee_xmr = fee_atomic / ATOMIC_UNITS_PER_XMR

        logger.info(
            f"XMR sent: {amount:.12f} XMR to {to_address[:12]}... "
            f"tx={tx_hash[:16]}... fee={fee_xmr:.8f} XMR"
        )

        return PaymentResult(
            success=True,
            payment_ref=tx_hash,
            amount=amount,
            currency="XMR",
            status=PaymentStatus.PENDING,  # Needs confirmations
            raw={
                "tx_hash": tx_hash,
                "tx_key": result.get("tx_key", ""),
                "fee": fee_xmr,
                "amount_atomic": amount_atomic,
            },
        )

    # ── Monero-specific methods ─────────────────────────────────

    async def poll_incoming(self, min_height: int = 0) -> list[dict[str, Any]]:
        """Poll for incoming transfers since a given block height.

        Used instead of webhooks. Should be called periodically.
        Returns list of new incoming transfers.
        """
        params: dict[str, Any] = {
            "in": True,
            "pending": True,
            "pool": True,
        }
        if min_height > 0:
            params["filter_by_height"] = True
            params["min_height"] = min_height

        result = await self._rpc_call("get_transfers", params)

        transfers = []
        for category in ("in", "pending", "pool"):
            for tx in result.get(category, []):
                transfers.append({
                    "tx_hash": tx.get("txid", ""),
                    "amount": tx.get("amount", 0) / ATOMIC_UNITS_PER_XMR,
                    "address": tx.get("address", ""),
                    "confirmations": tx.get("confirmations", 0),
                    "height": tx.get("height", 0),
                    "timestamp": tx.get("timestamp", 0),
                    "category": category,
                    "subaddr_index": tx.get("subaddr_index", {}).get("minor", 0),
                })

        return transfers

    async def get_wallet_height(self) -> int:
        """Get current wallet sync height."""
        result = await self._rpc_call("get_height")
        return result.get("height", 0)

    async def get_primary_address(self) -> str:
        """Get the wallet's primary address."""
        result = await self._rpc_call("get_address", {"account_index": 0})
        return result["address"]

    async def get_all_subaddresses(self) -> list[dict[str, Any]]:
        """List all subaddresses in the wallet."""
        result = await self._rpc_call("get_address", {"account_index": 0})
        return [
            {
                "index": addr["address_index"],
                "address": addr["address"],
                "label": addr.get("label", ""),
                "used": addr.get("used", False),
            }
            for addr in result.get("addresses", [])
        ]

    async def health_check(self) -> bool:
        """Check if wallet RPC is reachable and synced."""
        try:
            result = await self._rpc_call("get_height")
            return result.get("height", 0) > 0
        except Exception:
            return False


class MoneroRPCError(Exception):
    """Error from Monero wallet RPC."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Monero RPC error {code}: {message}")
