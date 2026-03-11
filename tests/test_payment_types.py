"""Tests for payment types and base abstractions."""

import pytest

from monai.payments.types import (
    PaymentIntent,
    PaymentResult,
    PaymentStatus,
    ProviderBalance,
    SweepRequest,
    SweepResult,
    SweepStatus,
    WebhookEvent,
    WebhookEventType,
)


class TestPaymentStatus:
    def test_status_values(self):
        assert PaymentStatus.PENDING == "pending"
        assert PaymentStatus.COMPLETED == "completed"
        assert PaymentStatus.FAILED == "failed"
        assert PaymentStatus.REFUNDED == "refunded"

    def test_status_is_string(self):
        assert isinstance(PaymentStatus.COMPLETED, str)
        assert PaymentStatus.COMPLETED == "completed"


class TestPaymentIntent:
    def test_defaults(self):
        intent = PaymentIntent(amount=100.0)
        assert intent.amount == 100.0
        assert intent.currency == "EUR"
        assert intent.product == ""
        assert intent.brand == ""
        assert intent.metadata == {}

    def test_full_construction(self):
        intent = PaymentIntent(
            amount=49.99,
            currency="USD",
            product="E-book: AI Guide",
            customer_email="customer@example.com",
            brand="micro_saas",
            metadata={"lead_id": 42},
        )
        assert intent.amount == 49.99
        assert intent.currency == "USD"
        assert intent.brand == "micro_saas"
        assert intent.metadata["lead_id"] == 42


    def test_rejects_zero_amount(self):
        with pytest.raises(ValueError, match="below minimum"):
            PaymentIntent(amount=0)

    def test_rejects_negative_amount(self):
        with pytest.raises(ValueError, match="below minimum"):
            PaymentIntent(amount=-10.0)

    def test_rejects_excessive_amount(self):
        with pytest.raises(ValueError, match="exceeds maximum"):
            PaymentIntent(amount=200_000.0)

    def test_rejects_nan_amount(self):
        with pytest.raises(ValueError, match="NaN"):
            PaymentIntent(amount=float("nan"))

    def test_minimum_amount_accepted(self):
        intent = PaymentIntent(amount=0.01)
        assert intent.amount == 0.01


class TestPaymentResult:
    def test_success_result(self):
        result = PaymentResult(
            success=True,
            payment_ref="cs_test_123",
            amount=100.0,
            currency="EUR",
            status=PaymentStatus.COMPLETED,
            checkout_url="https://checkout.stripe.com/session/123",
        )
        assert result.success is True
        assert result.payment_ref == "cs_test_123"
        assert result.status == PaymentStatus.COMPLETED
        assert "stripe.com" in result.checkout_url

    def test_failure_result(self):
        result = PaymentResult(success=False, error="Card declined")
        assert result.success is False
        assert result.error == "Card declined"
        assert result.status == PaymentStatus.PENDING  # default

    def test_raw_data_preserved(self):
        raw = {"id": "cs_123", "object": "checkout.session"}
        result = PaymentResult(success=True, raw=raw)
        assert result.raw["id"] == "cs_123"


class TestWebhookEvent:
    def test_construction(self):
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="cs_abc",
            amount=50.0,
            currency="EUR",
            customer_email="test@test.com",
        )
        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.provider == "stripe"
        assert event.amount == 50.0


class TestProviderBalance:
    def test_defaults(self):
        bal = ProviderBalance()
        assert bal.available == 0.0
        assert bal.pending == 0.0
        assert bal.currency == "EUR"

    def test_with_values(self):
        bal = ProviderBalance(available=1.5, pending=0.3, currency="XMR")
        assert bal.available == 1.5
        assert bal.pending == 0.3
        assert bal.currency == "XMR"


class TestSweepTypes:
    def test_sweep_request(self):
        req = SweepRequest(
            brand="newsletter",
            from_account_id=1,
            to_address="4" + "A" * 94,  # XMR-like address
            amount=100.0,
        )
        assert req.brand == "newsletter"
        assert req.method == "crypto_xmr"

    def test_sweep_result_success(self):
        result = SweepResult(
            success=True,
            sweep_id=42,
            tx_hash="ab" * 32,
            status=SweepStatus.COMPLETED,
            amount_crypto=0.5,
            fee=0.00005,
        )
        assert result.success is True
        assert result.status == SweepStatus.COMPLETED
        assert result.amount_crypto == 0.5

    def test_sweep_result_failure(self):
        result = SweepResult(
            success=False,
            error="Wallet offline",
            status=SweepStatus.FAILED,
        )
        assert result.success is False
        assert "Wallet offline" in result.error

    def test_sweep_status_values(self):
        assert SweepStatus.PENDING == "pending"
        assert SweepStatus.MIXING == "mixing"
        assert SweepStatus.COMPLETED == "completed"
        assert SweepStatus.FAILED == "failed"
