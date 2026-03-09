"""Payment processing — real integrations for collecting and sweeping money.

Architecture:
    Customer → Brand Payment Account (Stripe/BTCPay/Gumroad/LemonSqueezy)
    Brand Balance → Sweep Engine → Crypto Gateway (Monero/Bitcoin)
    Crypto Gateway → Creator Wallet (XMR address)
"""

from monai.payments.base import PaymentProvider, PaymentStatus, WebhookEvent
from monai.payments.types import (
    PaymentIntent,
    PaymentResult,
    SweepRequest,
    SweepResult,
    ProviderBalance,
)

__all__ = [
    "PaymentProvider",
    "PaymentStatus",
    "WebhookEvent",
    "PaymentIntent",
    "PaymentResult",
    "SweepRequest",
    "SweepResult",
    "ProviderBalance",
]
