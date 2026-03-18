"""Shared types for the payment system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
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


MIN_PAYMENT_AMOUNT = Decimal("0.01")  # Minimum payment: 1 cent
MAX_PAYMENT_AMOUNT = Decimal("100000")  # Safety cap: €100k per single payment


@dataclass
class PaymentIntent:
    """Request to create a payment link or invoice."""
    amount: Decimal
    currency: str = "EUR"
    product: str = ""
    customer_email: str = ""
    brand: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Validate payment amount on creation."""
        # Normalize to Decimal (accept float/int/str for backward compat)
        if not isinstance(self.amount, Decimal):
            try:
                self.amount = _to_decimal(self.amount)
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError(f"Payment amount must be a number, got {type(self.amount).__name__}")
        if self.amount.is_nan():
            raise ValueError("Payment amount cannot be NaN")
        if self.amount.is_infinite():
            raise ValueError("Payment amount cannot be infinite")
        if self.amount < MIN_PAYMENT_AMOUNT:
            raise ValueError(
                f"Payment amount {self.amount} below minimum {MIN_PAYMENT_AMOUNT}"
            )
        if self.amount > MAX_PAYMENT_AMOUNT:
            raise ValueError(
                f"Payment amount {self.amount} exceeds maximum {MAX_PAYMENT_AMOUNT}"
            )

    @property
    def amount_decimal(self) -> Decimal:
        return self.amount

    @property
    def amount_cents(self) -> int:
        """Amount in cents — safe integer conversion for Stripe/Gumroad."""
        return int(self.amount * 100)


def _normalize_amount(value: Decimal | float | int | str) -> Decimal:
    """Normalize an amount field to Decimal, defaulting to zero."""
    if isinstance(value, Decimal):
        return value
    try:
        return _to_decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


@dataclass
class PaymentResult:
    """Result of a payment verification or creation."""
    success: bool
    payment_ref: str = ""
    amount: Decimal = Decimal("0")
    currency: str = "EUR"
    status: PaymentStatus = PaymentStatus.PENDING
    checkout_url: str = ""  # For payment links
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.amount = _normalize_amount(self.amount)

    @property
    def amount_decimal(self) -> Decimal:
        return self.amount


@dataclass
class WebhookEvent:
    """Parsed webhook event from a payment provider."""
    event_type: WebhookEventType
    provider: str
    payment_ref: str
    amount: Decimal = Decimal("0")
    currency: str = "EUR"
    customer_email: str = ""
    product: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        self.amount = _normalize_amount(self.amount)

    @property
    def amount_decimal(self) -> Decimal:
        return self.amount


@dataclass
class ProviderBalance:
    """Balance info from a payment provider."""
    available: Decimal = Decimal("0")
    pending: Decimal = Decimal("0")
    currency: str = "EUR"
    provider: str = ""
    account_id: str = ""
    last_checked: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        self.available = _normalize_amount(self.available)
        self.pending = _normalize_amount(self.pending)

    @property
    def available_decimal(self) -> Decimal:
        return self.available

    @property
    def pending_decimal(self) -> Decimal:
        return self.pending


@dataclass
class SweepRequest:
    """Request to sweep funds from brand to creator."""
    brand: str
    from_account_id: int
    to_address: str  # Creator's crypto address
    amount: Decimal
    currency: str = "EUR"
    method: str = "crypto_xmr"  # crypto_xmr, crypto_btc_coinjoin, crypto_btc_direct
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.amount = _normalize_amount(self.amount)

    @property
    def amount_decimal(self) -> Decimal:
        return self.amount


@dataclass
class SweepResult:
    """Result of a sweep operation."""
    success: bool
    sweep_id: int = 0
    tx_hash: str = ""
    status: SweepStatus = SweepStatus.PENDING
    amount_crypto: Decimal = Decimal("0")  # Amount in crypto units sent
    fee: Decimal = Decimal("0")
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.amount_crypto = _normalize_amount(self.amount_crypto)
        self.fee = _normalize_amount(self.fee)

    @property
    def amount_decimal(self) -> Decimal:
        return self.amount_crypto

    @property
    def fee_decimal(self) -> Decimal:
        return self.fee
