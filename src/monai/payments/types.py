"""Shared types for the payment system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class PaymentStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"
    DISPUTED = "disputed"
    EXPIRED = "expired"


class SweepStatus(str, Enum):
    PENDING = "pending"
    MIXING = "mixing"
    BROADCASTING = "broadcasting"
    CONFIRMING = "confirming"
    COMPLETED = "completed"
    FAILED = "failed"


class WebhookEventType(str, Enum):
    PAYMENT_COMPLETED = "payment.completed"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_REFUNDED = "payment.refunded"
    PAYMENT_DISPUTED = "payment.disputed"
    PAYOUT_COMPLETED = "payout.completed"
    SUBSCRIPTION_CREATED = "subscription.created"
    SUBSCRIPTION_CANCELLED = "subscription.cancelled"


def _to_decimal(value: float | int | str | Decimal) -> Decimal:
    """Safely convert a value to Decimal for financial precision."""
    if isinstance(value, Decimal):
        return value
    # Convert via string to avoid float imprecision (e.g. Decimal(0.1) != Decimal("0.1"))
    return Decimal(str(value))


@dataclass
class PaymentIntent:
    """Request to create a payment link or invoice."""
    amount: float
    currency: str = "EUR"
    product: str = ""
    customer_email: str = ""
    brand: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def amount_decimal(self) -> Decimal:
        return _to_decimal(self.amount)

    @property
    def amount_cents(self) -> int:
        """Amount in cents — safe integer conversion for Stripe/Gumroad."""
        return int(self.amount_decimal * 100)


@dataclass
class PaymentResult:
    """Result of a payment verification or creation."""
    success: bool
    payment_ref: str = ""
    amount: float = 0.0
    currency: str = "EUR"
    status: PaymentStatus = PaymentStatus.PENDING
    checkout_url: str = ""  # For payment links
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def amount_decimal(self) -> Decimal:
        return _to_decimal(self.amount)


@dataclass
class WebhookEvent:
    """Parsed webhook event from a payment provider."""
    event_type: WebhookEventType
    provider: str
    payment_ref: str
    amount: float = 0.0
    currency: str = "EUR"
    customer_email: str = ""
    product: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def amount_decimal(self) -> Decimal:
        return _to_decimal(self.amount)


@dataclass
class ProviderBalance:
    """Balance info from a payment provider."""
    available: float = 0.0
    pending: float = 0.0
    currency: str = "EUR"
    provider: str = ""
    account_id: str = ""
    last_checked: datetime = field(default_factory=datetime.now)

    @property
    def available_decimal(self) -> Decimal:
        return _to_decimal(self.available)

    @property
    def pending_decimal(self) -> Decimal:
        return _to_decimal(self.pending)


@dataclass
class SweepRequest:
    """Request to sweep funds from brand to creator."""
    brand: str
    from_account_id: int
    to_address: str  # Creator's crypto address
    amount: float
    currency: str = "EUR"
    method: str = "crypto_xmr"  # crypto_xmr, crypto_btc_coinjoin, crypto_btc_direct
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def amount_decimal(self) -> Decimal:
        return _to_decimal(self.amount)


@dataclass
class SweepResult:
    """Result of a sweep operation."""
    success: bool
    sweep_id: int = 0
    tx_hash: str = ""
    status: SweepStatus = SweepStatus.PENDING
    amount_crypto: float = 0.0  # Amount in crypto units sent
    fee: float = 0.0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def amount_decimal(self) -> Decimal:
        return _to_decimal(self.amount_crypto)

    @property
    def fee_decimal(self) -> Decimal:
        return _to_decimal(self.fee)
