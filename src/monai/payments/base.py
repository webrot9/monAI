"""Abstract base for payment providers."""

from __future__ import annotations

import abc
import logging
from typing import Any

from monai.payments.types import (
    PaymentIntent,
    PaymentResult,
    PaymentStatus,
    ProviderBalance,
    WebhookEvent,
)

logger = logging.getLogger(__name__)


def _resolve_proxy_url(explicit_url: str = "") -> str:
    """Resolve proxy URL: use explicit if given, else auto-detect from anonymizer.

    This ensures payment providers ALWAYS go through the proxy even if
    the caller forgets to pass proxy_url explicitly.
    """
    if explicit_url:
        return explicit_url
    try:
        from monai.utils.privacy import get_anonymizer
        anonymizer = get_anonymizer()
        url = anonymizer.get_proxy_url()
        if url:
            return url
    except Exception:
        pass
    return ""


class PaymentProvider(abc.ABC):
    """Base class all payment providers must implement."""

    provider_name: str = ""

    @abc.abstractmethod
    async def create_payment(self, intent: PaymentIntent) -> PaymentResult:
        """Create a payment link/invoice for a customer to pay."""

    @abc.abstractmethod
    async def verify_payment(self, payment_ref: str) -> PaymentResult:
        """Verify a payment by its reference (tx hash, charge ID, etc.)."""

    @abc.abstractmethod
    async def get_balance(self, account_id: str) -> ProviderBalance:
        """Get the current balance of an account."""

    @abc.abstractmethod
    async def handle_webhook(self, payload: bytes,
                             headers: dict[str, str]) -> WebhookEvent | None:
        """Parse and verify an incoming webhook. Returns None if invalid."""

    @abc.abstractmethod
    async def send_payout(self, to_address: str, amount: float,
                          currency: str = "EUR",
                          **kwargs: Any) -> PaymentResult:
        """Send funds to an address (for crypto) or initiate payout."""

    async def health_check(self) -> bool:
        """Check if the provider is operational."""
        return True


class CryptoProvider(PaymentProvider):
    """Extended base for crypto-specific operations."""

    @abc.abstractmethod
    async def generate_address(self, label: str = "") -> str:
        """Generate a new receiving address (or subaddress)."""

    @abc.abstractmethod
    async def get_tx_confirmations(self, tx_hash: str) -> int:
        """Get number of confirmations for a transaction."""

    @abc.abstractmethod
    async def estimate_fee(self, amount: float,
                           priority: str = "normal") -> float:
        """Estimate transaction fee for a given amount."""
