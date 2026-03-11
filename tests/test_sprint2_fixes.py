"""Tests for Sprint 2 high-priority bug fixes.

Covers:
- Reconciliation filters (only webhook-sourced GL entries)
- Currency-aware sweepable balance
- Refund-after-sweep deficit tracking
- Rate limiter memory protection
- Sweep deficit deduction
"""

import time
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from monai.business.brand_payments import BrandPayments
from monai.business.finance import GeneralLedger
from monai.business.reconciliation import ReconciliationEngine
from monai.payments.manager import UnifiedPaymentManager
from monai.payments.types import (
    SweepResult, SweepStatus,
    WebhookEvent, WebhookEventType,
)
from monai.payments.webhook_server import _RateLimiter


# ── Fixtures ──────────────────────────────────────────────────

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


@pytest.fixture
def brand_payments(db):
    return BrandPayments(db)


def _ensure_payment_tables(db):
    BrandPayments(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO brand_payment_accounts "
            "(id, brand, provider, account_type, account_id, status) "
            "VALUES (0, '_placeholder', '_none', 'collection', '_placeholder', 'active')"
        )


# ── Reconciliation ────────────────────────────────────────────

class TestReconciliationFilter:
    def test_excludes_sweep_gl_entries(self, db, ledger):
        """Reconciliation should only match webhook-sourced GL entries."""
        _ensure_payment_tables(db)

        # Record a webhook payment
        ledger.record_revenue(
            amount=100.0,
            revenue_account="4900",
            cash_account="1010",
            description="Payment via stripe",
            reference="pi_recon_001",
            source="webhook",
            brand="test_brand",
        )

        # Record a sweep (should NOT appear in reconciliation)
        ledger.record_sweep(
            amount=50.0,
            from_account="1050",
            description="Sweep to creator",
            reference="sweep_tx_001",
            source="sweep_engine",
            brand="test_brand",
        )

        # Create matching webhook event
        with db.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT, event_type TEXT, payment_ref TEXT,
                    amount REAL, currency TEXT, brand TEXT,
                    status TEXT DEFAULT 'processed', raw_payload TEXT, error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute(
                "INSERT INTO webhook_events (provider, event_type, payment_ref, amount, currency, brand) "
                "VALUES ('stripe', 'payment.completed', 'pi_recon_001', 100.0, 'EUR', 'test_brand')"
            )

        recon = ReconciliationEngine(db)
        result = recon.run_reconciliation()

        # Should match the webhook payment
        assert result.matched == 1
        # Sweep entry should NOT be in unmatched_gl
        assert len(result.unmatched_gl) == 0


# ── Currency-Aware Sweepable ──────────────────────────────────

class TestCurrencyAwareSweepable:
    def test_sweepable_by_currency(self, brand_payments, db):
        """Sweepable balance should be trackable per currency."""
        # Add placeholder account for FK
        with db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO brand_payment_accounts "
                "(id, brand, provider, account_type, account_id, status) "
                "VALUES (0, '_placeholder', '_none', 'collection', '_placeholder', 'active')"
            )

        brand_payments.record_payment("brand_a", 0, 100.0, currency="EUR")
        brand_payments.record_payment("brand_a", 0, 50.0, currency="USD")
        brand_payments.record_payment("brand_a", 0, 30.0, currency="EUR")

        # Total sweepable (all currencies)
        assert brand_payments.get_sweepable_balance("brand_a") == 180.0

        # Per-currency sweepable
        assert brand_payments.get_sweepable_balance("brand_a", currency="EUR") == 130.0
        assert brand_payments.get_sweepable_balance("brand_a", currency="USD") == 50.0

        # Per-currency breakdown
        by_currency = brand_payments.get_sweepable_by_currency("brand_a")
        assert by_currency["EUR"] == 130.0
        assert by_currency["USD"] == 50.0

    def test_sweepable_subtracts_sweeps(self, brand_payments, db):
        """Sweeps reduce the sweepable balance."""
        with db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO brand_payment_accounts "
                "(id, brand, provider, account_type, account_id, status) "
                "VALUES (0, '_placeholder', '_none', 'collection', '_placeholder', 'active')"
            )

        brand_payments.record_payment("brand_b", 0, 200.0, currency="EUR")
        brand_payments.initiate_sweep("brand_b", 0, 0, 80.0)
        brand_payments.complete_sweep(1, "tx_hash")

        assert brand_payments.get_sweepable_balance("brand_b") == 120.0


# ── Refund After Sweep ────────────────────────────────────────

class TestRefundAfterSweep:
    @pytest.mark.asyncio
    async def test_deficit_tracked_on_refund_after_sweep(self, manager, db, ledger):
        """Refund after sweep should create a deficit record."""
        _ensure_payment_tables(db)

        # 1. Record a payment
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_deficit_001",
            amount=100.0,
            currency="EUR",
            product="Test",
            metadata={"brand": "deficit_brand"},
            raw={},
        )
        await manager._handle_webhook_event(event)

        # 2. Simulate a completed sweep
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO brand_profit_sweeps "
                "(brand, from_account_id, to_account_id, amount, sweep_method, "
                "status, completed_at) "
                "VALUES ('deficit_brand', 0, 0, 100.0, 'crypto_xmr', "
                "'completed', datetime('now'))"
            )

        # 3. Refund the payment
        refund_event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_REFUNDED,
            provider="stripe",
            payment_ref="pi_deficit_001",
            amount=100.0,
            currency="EUR",
            metadata={"brand": "deficit_brand"},
            raw={},
        )
        await manager._handle_webhook_event(refund_event)

        # 4. Verify deficit was tracked
        deficits = db.execute(
            "SELECT * FROM sweep_deficits WHERE brand = 'deficit_brand'"
        )
        assert len(deficits) == 1
        assert deficits[0]["amount"] == 100.0
        assert deficits[0]["status"] == "outstanding"
        assert deficits[0]["payment_ref"] == "pi_deficit_001"


# ── Rate Limiter ──────────────────────────────────────────────

class TestRateLimiter:
    def test_allows_within_limit(self):
        limiter = _RateLimiter(max_per_second=5, max_per_minute=100)
        for _ in range(5):
            assert limiter.is_allowed("1.2.3.4") is True

    def test_blocks_over_second_limit(self):
        limiter = _RateLimiter(max_per_second=3, max_per_minute=100)
        for _ in range(3):
            assert limiter.is_allowed("1.2.3.4") is True
        assert limiter.is_allowed("1.2.3.4") is False

    def test_different_ips_independent(self):
        limiter = _RateLimiter(max_per_second=2, max_per_minute=100)
        assert limiter.is_allowed("1.1.1.1") is True
        assert limiter.is_allowed("1.1.1.1") is True
        assert limiter.is_allowed("1.1.1.1") is False

        # Different IP should still be allowed
        assert limiter.is_allowed("2.2.2.2") is True

    def test_memory_protection(self):
        """Should not OOM from many unique IPs."""
        limiter = _RateLimiter(max_per_second=5, max_per_minute=100)
        limiter.MAX_TRACKED_IPS = 100  # Lower for test speed

        # Generate more IPs than the cap
        for i in range(150):
            limiter.is_allowed(f"10.0.{i // 256}.{i % 256}")

        # Should not have more than MAX_TRACKED_IPS entries
        assert len(limiter._second_counts) <= 101  # 100 + 1 after clear
