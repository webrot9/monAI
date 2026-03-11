"""Tests for the reconciliation engine — GL vs webhook event matching."""

from __future__ import annotations

import pytest

from monai.business.finance import GeneralLedger
from monai.business.reconciliation import ReconciliationEngine


@pytest.fixture
def ledger(db):
    return GeneralLedger(db)


@pytest.fixture
def engine(db, ledger):
    # Ensure webhook_events table exists (normally created by webhook server)
    # ledger fixture ensures GL tables exist
    from monai.payments.webhook_server import WEBHOOK_LOG_SCHEMA
    with db.connect() as conn:
        conn.executescript(WEBHOOK_LOG_SCHEMA)
    return ReconciliationEngine(db)


def _create_webhook_event(db, payment_ref, amount, provider="stripe",
                           currency="EUR", event_type="payment.completed"):
    """Insert a webhook event into the DB."""
    return db.execute_insert(
        "INSERT INTO webhook_events "
        "(provider, event_type, payment_ref, amount, currency, status) "
        "VALUES (?, ?, ?, ?, ?, 'processed')",
        (provider, event_type, payment_ref, amount, currency),
    )


class TestReconciliationMatching:
    def test_perfect_match(self, engine, ledger, db):
        """GL entry and webhook event with same ref and amount match perfectly."""
        # Create GL entry with reference
        ledger.record_revenue(
            amount=50.0, revenue_account="4000", cash_account="1010",
            description="Sale", reference="pay_abc123", source="webhook",
        )
        # Create matching webhook event
        _create_webhook_event(db, "pay_abc123", 50.0)

        result = engine.run_reconciliation()
        assert result.matched == 1
        assert result.is_clean
        assert result.discrepancy_count == 0

    def test_perfect_match_marks_reconciled(self, engine, ledger, db):
        """Matched GL entries get is_reconciled = 1."""
        ledger.record_revenue(
            amount=100.0, revenue_account="4000", cash_account="1010",
            description="Sale", reference="pay_rec001", source="webhook",
        )
        _create_webhook_event(db, "pay_rec001", 100.0)

        engine.run_reconciliation()

        rows = db.execute(
            "SELECT is_reconciled FROM gl_journal_entries WHERE reference = 'pay_rec001'"
        )
        assert rows[0]["is_reconciled"] == 1

    def test_amount_mismatch(self, engine, ledger, db):
        """Same ref but different amounts flagged as mismatch."""
        ledger.record_revenue(
            amount=50.0, revenue_account="4000", cash_account="1010",
            description="Sale", reference="pay_mismatch", source="webhook",
        )
        _create_webhook_event(db, "pay_mismatch", 55.0)

        result = engine.run_reconciliation()
        assert result.matched == 0
        assert len(result.amount_mismatches) == 1
        assert result.amount_mismatches[0]["difference"] == -5.0
        assert not result.is_clean

    def test_amount_within_tolerance(self, engine, ledger, db):
        """Amounts within €0.01 tolerance still match."""
        ledger.record_revenue(
            amount=99.995, revenue_account="4000", cash_account="1010",
            description="Sale", reference="pay_close", source="webhook",
        )
        _create_webhook_event(db, "pay_close", 100.0)

        result = engine.run_reconciliation()
        # 99.995 vs 100.0 — diff is 0.005 which is < 0.01
        assert result.matched == 1
        assert result.is_clean

    def test_unmatched_gl_entry(self, engine, ledger, db):
        """Webhook-sourced GL entry with no matching webhook flagged."""
        ledger.record_revenue(
            amount=75.0, revenue_account="4000", cash_account="1010",
            description="Webhook entry", reference="webhook_orphan_001",
            source="webhook",
        )

        result = engine.run_reconciliation()
        assert len(result.unmatched_gl) == 1
        assert result.unmatched_gl[0]["payment_ref"] == "webhook_orphan_001"
        assert not result.is_clean

    def test_manual_gl_entry_excluded(self, engine, ledger, db):
        """Manual GL entries are excluded from webhook reconciliation."""
        ledger.record_revenue(
            amount=75.0, revenue_account="4000", cash_account="1010",
            description="Manual entry", reference="manual_001", source="manual",
        )

        result = engine.run_reconciliation()
        # Manual entries should not appear — only webhook/webhook_refund sources
        assert result.total_gl == 0
        assert result.is_clean

    def test_unmatched_webhook_event(self, engine, db):
        """Webhook event with no GL entry flagged."""
        _create_webhook_event(db, "pay_orphan", 200.0, provider="gumroad")

        result = engine.run_reconciliation()
        assert len(result.unmatched_webhooks) == 1
        assert result.unmatched_webhooks[0]["payment_ref"] == "pay_orphan"
        assert result.unmatched_webhooks[0]["provider"] == "gumroad"

    def test_multiple_matches(self, engine, ledger, db):
        """Multiple GL entries and webhooks all match."""
        for i in range(5):
            ref = f"pay_multi_{i}"
            ledger.record_revenue(
                amount=10.0 * (i + 1), revenue_account="4000", cash_account="1010",
                description=f"Sale {i}", reference=ref, source="webhook",
            )
            _create_webhook_event(db, ref, 10.0 * (i + 1))

        result = engine.run_reconciliation()
        assert result.matched == 5
        assert result.is_clean

    def test_gl_without_reference_ignored(self, engine, ledger, db):
        """GL entries without a reference are not reconciled."""
        ledger.record_expense(
            amount=30.0, expense_account="5200", cash_account="1010",
            description="Internal expense", source="bootstrap",
        )

        result = engine.run_reconciliation()
        assert result.total_gl == 0  # No entries with references
        assert result.is_clean

    def test_mixed_results(self, engine, ledger, db):
        """Mix of matched, unmatched GL, unmatched webhooks, and mismatches."""
        # Matched pair
        ledger.record_revenue(
            amount=100.0, revenue_account="4000", cash_account="1010",
            description="Good sale", reference="pay_good", source="webhook",
        )
        _create_webhook_event(db, "pay_good", 100.0)

        # Amount mismatch
        ledger.record_revenue(
            amount=50.0, revenue_account="4000", cash_account="1010",
            description="Fee issue", reference="pay_fee", source="webhook",
        )
        _create_webhook_event(db, "pay_fee", 47.50)

        # Orphan GL (webhook source — should be flagged as unmatched)
        ledger.record_revenue(
            amount=25.0, revenue_account="4000", cash_account="1010",
            description="No webhook", reference="pay_missing_wh", source="webhook",
        )

        # Orphan webhook
        _create_webhook_event(db, "pay_missing_gl", 75.0, provider="kofi")

        result = engine.run_reconciliation()
        assert result.matched == 1
        assert len(result.amount_mismatches) == 1
        assert len(result.unmatched_gl) == 1
        assert len(result.unmatched_webhooks) == 1
        assert result.discrepancy_count == 3


class TestReconciliationPersistence:
    def test_run_recorded_in_db(self, engine, db):
        """Reconciliation run stored in reconciliation_runs table."""
        result = engine.run_reconciliation()
        history = engine.get_run_history()
        assert len(history) == 1
        assert history[0]["id"] == result.run_id
        assert history[0]["status"] == "clean"

    def test_items_recorded(self, engine, ledger, db):
        """Individual reconciliation items stored."""
        ledger.record_revenue(
            amount=50.0, revenue_account="4000", cash_account="1010",
            description="Sale", reference="pay_item_test", source="webhook",
        )
        _create_webhook_event(db, "pay_item_test", 50.0)

        result = engine.run_reconciliation()
        items = engine.get_run_items(result.run_id)
        assert len(items) == 1
        assert items[0]["item_type"] == "matched"
        assert items[0]["payment_ref"] == "pay_item_test"

    def test_multiple_runs_tracked(self, engine, db):
        """Multiple runs create separate records."""
        engine.run_reconciliation()
        engine.run_reconciliation()
        engine.run_reconciliation()

        history = engine.get_run_history()
        assert len(history) == 3

    def test_get_unreconciled_entries(self, engine, ledger, db):
        """Lists GL entries not yet reconciled."""
        ledger.record_revenue(
            amount=50.0, revenue_account="4000", cash_account="1010",
            description="Reconciled", reference="pay_done", source="webhook",
        )
        _create_webhook_event(db, "pay_done", 50.0)

        ledger.record_revenue(
            amount=75.0, revenue_account="4000", cash_account="1010",
            description="Not reconciled", reference="pay_pending", source="webhook",
        )

        # Run reconciliation (only pay_done matches)
        engine.run_reconciliation()

        unreconciled = engine.get_unreconciled_gl_entries()
        assert len(unreconciled) == 1
        assert unreconciled[0]["reference"] == "pay_pending"


class TestReconciliationReporting:
    def test_clean_telegram_report(self, engine, ledger, db):
        """Clean reconciliation produces concise report."""
        ledger.record_revenue(
            amount=100.0, revenue_account="4000", cash_account="1010",
            description="Sale", reference="pay_clean", source="webhook",
        )
        _create_webhook_event(db, "pay_clean", 100.0)

        result = engine.run_reconciliation()
        msg = engine.format_telegram_report(result)

        assert "CLEAN" in msg
        assert "Matched: 1" in msg

    def test_discrepancy_telegram_report(self, engine, ledger, db):
        """Discrepancy report includes mismatch details."""
        ledger.record_revenue(
            amount=100.0, revenue_account="4000", cash_account="1010",
            description="Sale", reference="pay_disc", source="webhook",
        )
        _create_webhook_event(db, "pay_disc", 95.0)

        result = engine.run_reconciliation()
        msg = engine.format_telegram_report(result)

        assert "DISCREPANCIES" in msg
        assert "pay_disc" in msg
        assert "Amount Mismatches" in msg
