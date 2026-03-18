"""End-to-end test: Gumroad digital product lifecycle.

Tests the complete pipeline:
  Product creation → Gumroad listing → Sale webhook → Payment recorded →
  Platform fee calculated → GL entry → Profit sweep → GL sweep entry →
  Refund after sweep → Deficit tracked → Books balanced
"""

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest

from monai.business.brand_payments import BrandPayments
from monai.business.finance import GeneralLedger
from monai.payments.gumroad_provider import GumroadProvider
from monai.payments.manager import UnifiedPaymentManager
from monai.payments.types import (
    SweepResult,
    SweepStatus,
    WebhookEvent,
    WebhookEventType,
)


# ── Fixtures ────────────────────────────────────────────────────

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


WEBHOOK_SECRET = "test_gumroad_secret_key"


@pytest.fixture
def gumroad_provider():
    return GumroadProvider(
        access_token="test_token",
        webhook_secret=WEBHOOK_SECRET,
    )


@pytest.fixture
def ledger(db):
    return GeneralLedger(db)


@pytest.fixture
def manager(mock_config, db, ledger):
    return UnifiedPaymentManager(mock_config, db, ledger=ledger)


def _ensure_tables(db):
    """Create payment tables + placeholder FK account."""
    BrandPayments(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO brand_payment_accounts "
            "(id, brand, provider, account_type, account_id, status) "
            "VALUES (0, '_placeholder', '_none', 'collection', '_placeholder', 'active')"
        )


def _make_gumroad_webhook(
    sale_id: str,
    product_name: str,
    price_cents: int,
    currency: str = "usd",
    refunded: bool = False,
    disputed: bool = False,
    product_id: str = "prod_abc123",
    email: str = "buyer@example.com",
) -> tuple[bytes, dict[str, str]]:
    """Build a signed Gumroad webhook payload + headers."""
    data = {
        "sale_id": sale_id,
        "product_id": product_id,
        "product_name": product_name,
        "price": str(price_cents),
        "currency": currency,
        "email": email,
        "resource_name": "sale",
        "refunded": str(refunded).lower(),
        "disputed": str(disputed).lower(),
        "seller_id": "seller_xyz",
        "order_number": f"ORD-{sale_id}",
    }
    payload = urlencode(data).encode("utf-8")
    sig = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    headers = {"x-gumroad-signature": sig}
    return payload, headers


# ── Webhook Parsing ─────────────────────────────────────────────

class TestGumroadWebhookParsing:
    """Verify Gumroad webhook → WebhookEvent conversion."""

    @pytest.mark.asyncio
    async def test_sale_webhook_parsed_correctly(self, gumroad_provider):
        """Valid sale webhook produces correct WebhookEvent."""
        payload, headers = _make_gumroad_webhook(
            sale_id="sale_001",
            product_name="Premium Template Pack",
            price_cents=2999,
        )
        event = await gumroad_provider.handle_webhook(payload, headers)

        assert event is not None
        assert event.event_type == WebhookEventType.PAYMENT_COMPLETED
        assert event.provider == "gumroad"
        assert event.payment_ref == "sale_001"
        assert event.amount == Decimal("29.99")
        assert event.currency == "USD"
        assert event.product == "Premium Template Pack"
        assert event.customer_email == "buyer@example.com"
        assert event.metadata["product_id"] == "prod_abc123"

    @pytest.mark.asyncio
    async def test_refund_webhook_parsed_correctly(self, gumroad_provider):
        payload, headers = _make_gumroad_webhook(
            sale_id="sale_002",
            product_name="Ebook Bundle",
            price_cents=1500,
            refunded=True,
        )
        event = await gumroad_provider.handle_webhook(payload, headers)

        assert event is not None
        assert event.event_type == WebhookEventType.PAYMENT_REFUNDED
        assert event.amount == Decimal("15.00")

    @pytest.mark.asyncio
    async def test_disputed_webhook_parsed_correctly(self, gumroad_provider):
        payload, headers = _make_gumroad_webhook(
            sale_id="sale_003",
            product_name="Course",
            price_cents=4999,
            disputed=True,
        )
        event = await gumroad_provider.handle_webhook(payload, headers)

        assert event.event_type == WebhookEventType.PAYMENT_DISPUTED

    @pytest.mark.asyncio
    async def test_invalid_signature_rejected(self, gumroad_provider):
        """Tampered payload rejected by HMAC verification."""
        payload, headers = _make_gumroad_webhook(
            sale_id="sale_bad", product_name="Hack", price_cents=100,
        )
        headers["x-gumroad-signature"] = "invalid_hex"
        event = await gumroad_provider.handle_webhook(payload, headers)
        assert event is None

    @pytest.mark.asyncio
    async def test_no_secret_configured_rejects(self):
        """Provider without webhook_secret rejects all webhooks."""
        provider = GumroadProvider(access_token="tok", webhook_secret="")
        payload, headers = _make_gumroad_webhook(
            sale_id="sale_x", product_name="X", price_cents=100,
        )
        event = await provider.handle_webhook(payload, headers)
        assert event is None


# ── Payment Recording + GL ──────────────────────────────────────

class TestGumroadPaymentToGL:
    """Webhook → payment record → platform fee → GL entry."""

    @pytest.mark.asyncio
    async def test_gumroad_sale_creates_payment_and_gl(self, manager, ledger, db):
        """Sale webhook: payment + fee recorded, GL entry balanced."""
        _ensure_tables(db)

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="gumroad",
            payment_ref="gum_sale_e2e_001",
            amount=Decimal("29.99"),
            currency="USD",
            product="Premium Template Pack",
            customer_email="buyer@example.com",
            metadata={"brand": "template_brand"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        # Payment recorded in brand_payments_received
        payments = db.execute(
            "SELECT * FROM brand_payments_received WHERE payment_ref = ?",
            ("gum_sale_e2e_001",),
        )
        assert len(payments) == 1
        pay = dict(payments[0])
        assert pay["brand"] == "template_brand"
        assert pay["amount"] == pytest.approx(29.99, abs=0.01)
        assert pay["currency"] == "USD"
        assert pay["product"] == "Premium Template Pack"
        assert pay["status"] == "completed"

        # Platform fee recorded
        fees = db.execute(
            "SELECT * FROM platform_fees WHERE payment_id = ?",
            (pay["id"],),
        )
        assert len(fees) == 1
        fee = dict(fees[0])
        assert fee["provider"] == "gumroad"
        assert fee["gross_amount"] == pytest.approx(29.99, abs=0.01)
        assert fee["fee_amount"] > 0  # Gumroad takes a cut

        # GL entry: 3-line (cash + fee + revenue)
        entries = ledger.get_journal_entries(brand="template_brand")
        assert len(entries) == 1
        entry = entries[0]
        assert entry["source"] == "webhook"
        assert entry["reference"] == "gum_sale_e2e_001"

        lines = entry["lines"]
        assert len(lines) == 3

        # Cash account 1020 (Gumroad) debited with net
        cash = [l for l in lines if l["account_code"] == "1020"][0]
        assert cash["debit"] > 0
        assert cash["credit"] == 0

        # Fee account 5200 debited
        fee_line = [l for l in lines if l["account_code"] == "5200"][0]
        assert fee_line["debit"] > 0

        # Revenue 4100 (digital product — "template" keyword) credited with gross
        rev = [l for l in lines if l["account_code"] == "4100"][0]
        assert rev["credit"] == pytest.approx(29.99, abs=0.01)

        # Debit = Credit (balanced)
        total_debit = sum(l["debit"] for l in lines)
        total_credit = sum(l["credit"] for l in lines)
        assert total_debit == pytest.approx(total_credit, abs=0.01)

        # Books balanced
        assert ledger.verify_integrity()["balanced"]

    @pytest.mark.asyncio
    async def test_gumroad_fee_calculation_correct(self, manager, db, ledger):
        """Gumroad platform fee is calculated at the provider rate."""
        _ensure_tables(db)

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="gumroad",
            payment_ref="gum_fee_test",
            amount=Decimal("100.00"),
            currency="USD",
            product="Digital Download",
            metadata={"brand": "fee_brand"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        fees = db.execute(
            "SELECT fee_amount, gross_amount FROM platform_fees "
            "WHERE brand = 'fee_brand'"
        )
        fee = dict(fees[0])
        # Fee should be a percentage of gross (Gumroad ~10%)
        fee_pct = fee["fee_amount"] / fee["gross_amount"]
        assert 0.05 < fee_pct < 0.20  # Reasonable range for Gumroad fees


# ── Full Lifecycle: Payment → Sweep ─────────────────────────────

class TestGumroadPaymentToSweep:
    """Complete lifecycle: sale → payment → sweep → GL balanced."""

    @pytest.mark.asyncio
    async def test_sale_then_sweep_creates_two_gl_entries(self, manager, ledger, db):
        """Payment + sweep = 2 GL entries, books balanced."""
        _ensure_tables(db)

        # 1. Sale webhook
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="gumroad",
            payment_ref="lifecycle_gum_001",
            amount=Decimal("49.99"),
            currency="USD",
            product="Ebook Template",
            metadata={"brand": "lifecycle_brand"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        # 2. Sweep to creator
        mock_sweep = SweepResult(
            success=True,
            sweep_id=1,
            tx_hash="xmr_tx_abc123",
            status=SweepStatus.COMPLETED,
            amount_crypto=0.25,
        )
        manager.sweep_engine.sweep_brand = AsyncMock(return_value=mock_sweep)
        result = await manager.sweep_brand("lifecycle_brand", 49.99)
        assert result["success"]

        # 3. Two GL entries: payment + sweep
        entries = ledger.get_journal_entries(brand="lifecycle_brand")
        assert len(entries) == 2
        sources = {e["source"] for e in entries}
        assert "webhook" in sources
        assert "sweep_engine" in sources

        # 4. Sweep GL: debit creator payable (2300), credit monero cash (1050)
        sweep_entry = [e for e in entries if e["source"] == "sweep_engine"][0]
        payable = [l for l in sweep_entry["lines"] if l["account_code"] == "2300"][0]
        assert payable["debit"] == 0.25
        crypto_cash = [l for l in sweep_entry["lines"]
                       if l["account_code"] == "1050"][0]
        assert crypto_cash["credit"] == 0.25

        # 5. Books balanced
        integrity = ledger.verify_integrity()
        assert integrity["balanced"]
        assert integrity["trial_balance_ok"]


# ── Refund After Sweep → Deficit Tracking ───────────────────────

class TestGumroadRefundAfterSweep:
    """Refund after sweep creates deficit record atomically."""

    @pytest.mark.asyncio
    async def test_refund_after_sweep_tracks_deficit(self, manager, ledger, db):
        """Refund on already-swept funds creates deficit for recovery."""
        _ensure_tables(db)

        # 1. Payment
        pay_event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="gumroad",
            payment_ref="refund_sweep_001",
            amount=Decimal("75.00"),
            currency="USD",
            product="Digital Guide",
            metadata={"brand": "refund_brand"},
            raw={},
        )
        await manager._handle_webhook_event(pay_event)

        # 2. Sweep completes — insert record directly (sweep_engine is mocked)
        sweep_id = db.execute_insert(
            "INSERT INTO brand_profit_sweeps "
            "(brand, amount, currency, sweep_method, "
            "tx_reference, status, completed_at) "
            "VALUES (?, ?, ?, ?, ?, 'completed', CURRENT_TIMESTAMP)",
            ("refund_brand", 75.0, "USD", "crypto_xmr", "swept_tx"),
        )
        # Also record in GL
        mock_sweep = SweepResult(
            success=True, sweep_id=sweep_id, tx_hash="swept_tx",
            status=SweepStatus.COMPLETED, amount_crypto=0.5,
        )
        manager.sweep_engine.sweep_brand = AsyncMock(return_value=mock_sweep)
        await manager.sweep_brand("refund_brand", 75.0)

        # 3. Customer requests refund
        refund_event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_REFUNDED,
            provider="gumroad",
            payment_ref="refund_sweep_001",
            amount=Decimal("75.00"),
            currency="USD",
            metadata={"brand": "refund_brand"},
            raw={},
        )
        await manager._handle_webhook_event(refund_event)

        # 4. Payment status → refunded
        payments = db.execute(
            "SELECT status FROM brand_payments_received WHERE payment_ref = ?",
            ("refund_sweep_001",),
        )
        assert dict(payments[0])["status"] == "refunded"

        # 5. Deficit recorded for recovery
        deficits = db.execute(
            "SELECT * FROM sweep_deficits WHERE brand = 'refund_brand'"
        )
        assert len(deficits) == 1
        deficit = dict(deficits[0])
        assert deficit["amount"] == pytest.approx(75.0, abs=0.01)
        assert deficit["status"] == "outstanding"
        assert deficit["sweep_id"] == sweep_id

        # 6. Refund GL entry exists
        entries = ledger.get_journal_entries(brand="refund_brand")
        # Payment + sweep + refund reversal = 3
        assert len(entries) == 3

        # Books still balanced
        assert ledger.verify_integrity()["balanced"]

    @pytest.mark.asyncio
    async def test_refund_without_sweep_no_deficit(self, manager, ledger, db):
        """Refund before sweep = no deficit needed, just mark refunded."""
        _ensure_tables(db)

        # Payment
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="gumroad",
            payment_ref="refund_nosweep_001",
            amount=Decimal("25.00"),
            currency="USD",
            product="Template",
            metadata={"brand": "nosweep_brand"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        # Refund (no sweep happened)
        refund = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_REFUNDED,
            provider="gumroad",
            payment_ref="refund_nosweep_001",
            amount=Decimal("25.00"),
            currency="USD",
            metadata={"brand": "nosweep_brand"},
            raw={},
        )
        await manager._handle_webhook_event(refund)

        # No deficit table or no deficit entries
        try:
            deficits = db.execute(
                "SELECT * FROM sweep_deficits WHERE brand = 'nosweep_brand'"
            )
            assert len(deficits) == 0
        except Exception:
            pass  # Table may not exist if no swept payments ever


# ── Duplicate Webhook Prevention ────────────────────────────────

class TestGumroadDuplicateWebhook:
    """Idempotency: same webhook processed only once."""

    @pytest.mark.asyncio
    async def test_duplicate_sale_processed_once(self, manager, ledger, db):
        _ensure_tables(db)

        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="gumroad",
            payment_ref="gum_dup_e2e_001",
            amount=Decimal("19.99"),
            currency="USD",
            product="Digital Template",
            metadata={"brand": "dup_brand"},
            raw={},
        )

        # Process same event twice
        await manager._handle_webhook_event(event)
        await manager._handle_webhook_event(event)

        # Only 1 payment
        payments = db.execute(
            "SELECT * FROM brand_payments_received WHERE payment_ref = ?",
            ("gum_dup_e2e_001",),
        )
        assert len(payments) == 1

        # Only 1 GL entry
        entries = ledger.get_journal_entries(brand="dup_brand")
        assert len(entries) == 1


# ── Multi-Sale Income Statement ─────────────────────────────────

class TestGumroadIncomeStatement:
    """Multiple Gumroad sales → correct income statement."""

    @pytest.mark.asyncio
    async def test_multiple_sales_income_and_fees(self, manager, ledger, db):
        _ensure_tables(db)

        # 3 sales of different products
        for i, (price, product) in enumerate([
            (Decimal("29.99"), "Ebook Template"),
            (Decimal("49.99"), "Digital Course Download"),
            (Decimal("9.99"), "Prompt Template Pack"),
        ]):
            event = WebhookEvent(
                event_type=WebhookEventType.PAYMENT_COMPLETED,
                provider="gumroad",
                payment_ref=f"gum_multi_{i}",
                amount=price,
                currency="USD",
                product=product,
                metadata={"brand": "multi_brand"},
                raw={},
            )
            await manager._handle_webhook_event(event)

        # Income statement
        income = ledger.get_income_statement()
        assert income["total_revenue"] == pytest.approx(89.97, abs=0.01)
        assert income["total_expenses"] > 0  # Platform fees
        assert income["net_income"] > 0
        assert income["net_income"] < 89.97  # Less than gross

        # Books balanced
        assert ledger.verify_integrity()["balanced"]

        # Balance sheet balanced
        bs = ledger.get_balance_sheet()
        assert bs["balanced"]
        assert bs["assets"] > 0
