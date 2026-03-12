"""End-to-end integration tests for the full payment flow.

Tests the complete lifecycle:
  Webhook received → Payment recorded → GL entry created →
  Sweep to creator → GL sweep entry → Balance sheet balanced
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.business.finance import GeneralLedger
from monai.payments.manager import UnifiedPaymentManager
from monai.payments.types import (
    PaymentResult,
    PaymentStatus,
    SweepResult,
    SweepStatus,
    WebhookEvent,
    WebhookEventType,
)


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.creator_wallet.xmr_address = "4" + "A" * 94
    config.creator_wallet.sweep_threshold_eur = 50.0
    config.creator_wallet.sweep_interval_hours = 24
    config.creator_wallet.min_confirmations_xmr = 10
    config.creator_wallet.min_confirmations_btc = 3
    config.monero.wallet_rpc_url = ""
    config.monero.rpc_user = ""
    config.monero.rpc_password = ""
    config.monero.proxy_url = ""
    return config


@pytest.fixture
def ledger(db):
    return GeneralLedger(db)


@pytest.fixture
def manager(mock_config, db, ledger):
    return UnifiedPaymentManager(mock_config, db, ledger=ledger)


class TestPaymentFlowE2E:
    """Test the complete payment flow from webhook to GL entry."""

    @pytest.mark.asyncio
    async def test_stripe_payment_creates_gl_entry(self, manager, ledger, db):
        """Webhook → payment recorded → GL entry with platform fee."""
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_stripe_001",
            amount=100.0,
            currency="EUR",
            customer_email="customer@test.com",
            product="Digital Template Pack",
            metadata={"brand": "test_brand"},
            raw={"id": "evt_1"},
        )

        # Need brand_payments_received table and platform_fees table
        _ensure_payment_tables(db)

        await manager._handle_webhook_event(event)

        # Verify GL entry was created
        entries = ledger.get_journal_entries(brand="test_brand")
        assert len(entries) == 1
        entry = entries[0]
        assert "stripe" in entry["description"].lower()
        assert entry["reference"] == "pi_stripe_001"
        assert entry["source"] == "webhook"

        # Verify it's a 3-line entry (cash + fee + revenue) since Stripe has fees
        assert len(entry["lines"]) == 3

        # Cash account (1010 = Cash - Stripe) should be debited with net amount
        cash_line = [l for l in entry["lines"] if l["account_code"] == "1010"][0]
        assert cash_line["debit"] > 0
        assert cash_line["credit"] == 0

        # Fee account (5200) should be debited
        fee_line = [l for l in entry["lines"] if l["account_code"] == "5200"][0]
        assert fee_line["debit"] > 0

        # Revenue account should be credited with gross amount
        rev_line = [l for l in entry["lines"]
                    if l["account_code"].startswith("4")][0]
        assert rev_line["credit"] == 100.0

        # Books must be balanced
        integrity = ledger.verify_integrity()
        assert integrity["balanced"]

    @pytest.mark.asyncio
    async def test_gumroad_payment_product_categorization(self, manager, ledger, db):
        """Revenue account selection based on product type."""
        _ensure_payment_tables(db)

        # Digital product → 4100
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="gumroad",
            payment_ref="gum_001",
            amount=29.99,
            currency="USD",
            product="Premium Ebook Collection",
            metadata={"brand": "ebook_brand"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        entries = ledger.get_journal_entries(brand="ebook_brand")
        assert len(entries) == 1
        rev_line = [l for l in entries[0]["lines"]
                    if l["account_code"].startswith("4")][0]
        assert rev_line["account_code"] == "4100"  # Digital products

    @pytest.mark.asyncio
    async def test_subscription_revenue_categorization(self, manager, ledger, db):
        """Subscription product → 4200 revenue account."""
        _ensure_payment_tables(db)

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="lemonsqueezy",
            payment_ref="ls_sub_001",
            amount=19.99,
            currency="USD",
            product="Pro SaaS Subscription Plan",
            metadata={"brand": "saas_brand"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        entries = ledger.get_journal_entries(brand="saas_brand")
        rev_line = [l for l in entries[0]["lines"]
                    if l["account_code"].startswith("4")][0]
        assert rev_line["account_code"] == "4200"  # Subscriptions

    @pytest.mark.asyncio
    async def test_refund_creates_reversal_gl_entry(self, manager, ledger, db):
        """Refund creates a reversal GL entry."""
        _ensure_payment_tables(db)

        # First: record the payment
        payment_event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_refund_test",
            amount=50.0,
            currency="EUR",
            product="Test Product",
            metadata={"brand": "refund_brand"},
            raw={},
        )
        await manager._handle_webhook_event(payment_event)

        # Then: refund it
        refund_event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_REFUNDED,
            provider="stripe",
            payment_ref="pi_refund_test",
            amount=50.0,
            currency="EUR",
            metadata={"brand": "refund_brand"},
            raw={},
        )
        await manager._handle_webhook_event(refund_event)

        # Should have 2 GL entries: payment + refund reversal
        entries = ledger.get_journal_entries(brand="refund_brand")
        assert len(entries) == 2

        # Refund entry should debit revenue and credit cash
        refund_entry = [e for e in entries if e["source"] == "webhook_refund"][0]
        rev_line = [l for l in refund_entry["lines"]
                    if l["account_code"].startswith("4")][0]
        assert rev_line["debit"] == 50.0  # Revenue reversed

        # Books still balanced
        assert ledger.verify_integrity()["balanced"]

    @pytest.mark.asyncio
    async def test_duplicate_webhook_no_double_gl(self, manager, ledger, db):
        """Duplicate webhooks should NOT create duplicate GL entries."""
        _ensure_payment_tables(db)

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="gumroad",
            payment_ref="gum_dup_001",
            amount=30.0,
            currency="EUR",
            product="Test",
            metadata={"brand": "dup_brand"},
            raw={},
        )

        # Send same webhook twice
        await manager._handle_webhook_event(event)
        await manager._handle_webhook_event(event)

        # Should only have 1 GL entry
        entries = ledger.get_journal_entries(brand="dup_brand")
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_multiple_payments_balance_sheet(self, manager, ledger, db):
        """Multiple payments across providers → balanced books."""
        _ensure_payment_tables(db)

        payments = [
            ("stripe", "pi_multi_1", 100.0, "EUR", "Freelance Service", "brand_a"),
            ("gumroad", "gum_multi_1", 49.99, "USD", "Digital Download", "brand_b"),
            ("lemonsqueezy", "ls_multi_1", 29.99, "USD", "SaaS Subscription", "brand_c"),
        ]

        for provider, ref, amount, currency, product, brand in payments:
            event = WebhookEvent(
                event_type=WebhookEventType.PAYMENT_COMPLETED,
                provider=provider,
                payment_ref=ref,
                amount=amount,
                currency=currency,
                product=product,
                metadata={"brand": brand},
                raw={},
            )
            await manager._handle_webhook_event(event)

        # All entries should be balanced
        integrity = ledger.verify_integrity()
        assert integrity["balanced"]
        assert integrity["trial_balance_ok"]

        # Balance sheet should balance
        bs = ledger.get_balance_sheet()
        assert bs["balanced"]
        assert bs["assets"] > 0
        assert bs["net_income"] > 0

    @pytest.mark.asyncio
    async def test_payment_without_ledger_still_works(self, mock_config, db):
        """Payment processing works fine without a ledger (backward compat)."""
        manager_no_gl = UnifiedPaymentManager(mock_config, db, ledger=None)
        _ensure_payment_tables(db)

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_no_gl_001",
            amount=75.0,
            currency="EUR",
            product="Service",
            metadata={"brand": "no_gl_brand"},
            raw={},
        )

        # Should not raise
        await manager_no_gl._handle_webhook_event(event)

    @pytest.mark.asyncio
    async def test_income_statement_after_payments(self, manager, ledger, db):
        """Income statement reflects all recorded payments."""
        _ensure_payment_tables(db)

        today = datetime.now().strftime("%Y-%m-%d")
        month_start = datetime.now().strftime("%Y-%m-01")

        # Record revenue
        for i in range(3):
            event = WebhookEvent(
                event_type=WebhookEventType.PAYMENT_COMPLETED,
                provider="gumroad",
                payment_ref=f"gum_is_{i}",
                amount=50.0,
                currency="EUR",
                product="Template",
                metadata={"brand": "is_brand"},
                raw={},
            )
            await manager._handle_webhook_event(event)

        income = ledger.get_income_statement(month_start, today)
        assert income["total_revenue"] == 150.0  # 3 × 50
        assert income["total_expenses"] > 0  # Platform fees
        assert income["net_income"] > 0
        assert income["net_income"] < 150.0  # Less than gross due to fees

    @pytest.mark.asyncio
    async def test_negative_amount_rejected_no_gl(self, manager, ledger, db):
        """Negative webhook amounts raise ValueError (saved to DLQ by webhook server)."""
        _ensure_payment_tables(db)

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_neg_001",
            amount=-50.0,
            currency="EUR",
            metadata={"brand": "neg_brand"},
            raw={},
        )
        with pytest.raises(ValueError, match="negative amount"):
            await manager._handle_webhook_event(event)

        # No GL entries should exist
        entries = ledger.get_journal_entries(brand="neg_brand")
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_suspicious_amount_rejected_no_gl(self, manager, ledger, db):
        """Amounts > €1M raise ValueError (saved to DLQ by webhook server)."""
        _ensure_payment_tables(db)

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_huge_001",
            amount=2_000_000.0,
            currency="EUR",
            metadata={"brand": "huge_brand"},
            raw={},
        )
        with pytest.raises(ValueError, match="suspicious amount"):
            await manager._handle_webhook_event(event)

        entries = ledger.get_journal_entries(brand="huge_brand")
        assert len(entries) == 0


class TestSweepGLIntegration:
    """Test sweep operations create proper GL entries."""

    @pytest.mark.asyncio
    async def test_sweep_brand_creates_gl_entry(self, manager, ledger, db):
        """Successful sweep creates a GL entry (debit creator payable, credit cash)."""
        # Mock the sweep engine to return success
        mock_result = SweepResult(
            success=True,
            sweep_id=1,
            tx_hash="abc123",
            status=SweepStatus.COMPLETED,
            amount_crypto=1.5,
        )
        manager.sweep_engine.sweep_brand = AsyncMock(return_value=mock_result)

        result = await manager.sweep_brand("test_brand", 100.0)
        assert result["success"]

        # Check GL entry
        entries = ledger.get_journal_entries(brand="test_brand")
        assert len(entries) == 1
        entry = entries[0]
        assert "sweep" in entry["description"].lower()
        assert entry["source"] == "sweep_engine"

        # Creator payable (2300) debited, Monero cash (1050) credited
        payable_line = [l for l in entry["lines"] if l["account_code"] == "2300"][0]
        assert payable_line["debit"] == 1.5

        cash_line = [l for l in entry["lines"] if l["account_code"] == "1050"][0]
        assert cash_line["credit"] == 1.5

        assert ledger.verify_integrity()["balanced"]

    @pytest.mark.asyncio
    async def test_failed_sweep_no_gl_entry(self, manager, ledger, db):
        """Failed sweep should NOT create a GL entry."""
        mock_result = SweepResult(
            success=False,
            error="No XMR available",
            status=SweepStatus.FAILED,
        )
        manager.sweep_engine.sweep_brand = AsyncMock(return_value=mock_result)

        result = await manager.sweep_brand("test_brand", 100.0)
        assert not result["success"]

        entries = ledger.get_journal_entries(brand="test_brand")
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_full_lifecycle_payment_to_sweep(self, manager, ledger, db):
        """Complete lifecycle: payment → GL → sweep → GL → balanced."""
        _ensure_payment_tables(db)

        # 1. Receive payment
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="gumroad",
            payment_ref="lifecycle_001",
            amount=200.0,
            currency="EUR",
            product="Premium Service",
            metadata={"brand": "lifecycle_brand"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        # 2. Sweep to creator
        mock_result = SweepResult(
            success=True,
            sweep_id=1,
            tx_hash="lifecycle_tx",
            status=SweepStatus.COMPLETED,
            amount_crypto=0.8,
        )
        manager.sweep_engine.sweep_brand = AsyncMock(return_value=mock_result)
        await manager.sweep_brand("lifecycle_brand", 200.0)

        # 3. Verify complete audit trail
        entries = ledger.get_journal_entries(brand="lifecycle_brand")
        assert len(entries) == 2  # Payment + sweep

        sources = {e["source"] for e in entries}
        assert "webhook" in sources
        assert "sweep_engine" in sources

        # 4. Books must still be balanced
        integrity = ledger.verify_integrity()
        assert integrity["balanced"]
        assert integrity["trial_balance_ok"]

        # 5. Income statement shows the revenue
        income = ledger.get_income_statement()
        assert income["total_revenue"] == 200.0


def _ensure_payment_tables(db):
    """Ensure payment tables exist and have a placeholder account for FK."""
    from monai.business.brand_payments import BrandPayments
    BrandPayments(db)  # Creates real schema
    # Insert a placeholder account (id=0) so FK constraints pass
    # when no matching collection account exists for a brand.
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO brand_payment_accounts "
            "(id, brand, provider, account_type, account_id, status) "
            "VALUES (0, '_placeholder', '_none', 'collection', '_placeholder', 'active')"
        )
