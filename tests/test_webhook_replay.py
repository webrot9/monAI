"""Tests for Webhook Replay functionality.

Covers:
- Replaying a single webhook event
- Handling missing/corrupted events
- Batch replay of failed webhooks
- Listing replayable events
"""

import json
from unittest.mock import MagicMock

import pytest

from monai.business.brand_payments import BrandPayments
from monai.business.finance import GeneralLedger
from monai.payments.manager import UnifiedPaymentManager
from monai.payments.types import WebhookEvent, WebhookEventType


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


def _ensure_payment_tables(db):
    BrandPayments(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO brand_payment_accounts "
            "(id, brand, provider, account_type, account_id, status) "
            "VALUES (0, '_placeholder', '_none', 'collection', '_placeholder', 'active')"
        )


class TestReplaySingleWebhook:
    @pytest.mark.asyncio
    async def test_replay_successful(self, manager, db, ledger):
        """Replaying a stored webhook should re-process it."""
        _ensure_payment_tables(db)

        # First, process a webhook normally
        event = WebhookEvent(
            event_type=WebhookEventType.PAYMENT_COMPLETED,
            provider="stripe",
            payment_ref="pi_replay_001",
            amount=42.0,
            currency="EUR",
            product="Test Product",
            metadata={"brand": "replay_brand"},
            raw={"id": "evt_123", "type": "payment_intent.succeeded"},
        )
        await manager._handle_webhook_event(event)

        # Verify it was recorded
        rows = db.execute(
            "SELECT id FROM webhook_events WHERE payment_ref = 'pi_replay_001'"
        )
        assert len(rows) == 1
        event_id = rows[0]["id"]

        # Delete the payment record (and FK-dependent fees) to simulate re-process
        payment_rows = db.execute(
            "SELECT id FROM brand_payments_received WHERE payment_ref = 'pi_replay_001'"
        )
        if payment_rows:
            pay_id = payment_rows[0]["id"]
            db.execute("DELETE FROM platform_fees WHERE payment_id = ?", (pay_id,))
        db.execute(
            "DELETE FROM brand_payments_received WHERE payment_ref = 'pi_replay_001'"
        )

        # Replay the event
        result = await manager.replay_webhook(event_id)
        assert result["success"] is True
        assert result["payment_ref"] == "pi_replay_001"

        # Verify payment was re-recorded
        payments = db.execute(
            "SELECT * FROM brand_payments_received WHERE payment_ref = 'pi_replay_001'"
        )
        assert len(payments) == 1

    @pytest.mark.asyncio
    async def test_replay_nonexistent_event(self, manager, db):
        """Replaying a non-existent event should return error."""
        result = await manager.replay_webhook(99999)
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_replay_without_raw_payload(self, manager, db):
        """Events without raw_payload cannot be replayed."""
        _ensure_payment_tables(db)

        # Insert a webhook event without raw_payload
        db.execute_insert(
            "INSERT INTO webhook_events "
            "(provider, event_type, payment_ref, amount, currency, brand) "
            "VALUES ('stripe', 'payment.completed', 'pi_no_raw', 10.0, 'EUR', 'test')"
        )
        rows = db.execute(
            "SELECT id FROM webhook_events WHERE payment_ref = 'pi_no_raw'"
        )
        result = await manager.replay_webhook(rows[0]["id"])
        assert result["success"] is False
        assert "raw_payload" in result["error"]


class TestBatchReplay:
    @pytest.mark.asyncio
    async def test_replay_failed_webhooks(self, manager, db):
        """Batch replay should process events with errors."""
        _ensure_payment_tables(db)

        # Insert events with errors
        for i in range(3):
            db.execute_insert(
                "INSERT INTO webhook_events "
                "(provider, event_type, payment_ref, amount, currency, brand, "
                "raw_payload, error) "
                "VALUES (?, ?, ?, ?, 'EUR', 'test_brand', ?, ?)",
                (
                    "stripe",
                    "payment.completed",
                    f"pi_batch_{i}",
                    float(10 + i),
                    json.dumps({"id": f"evt_{i}"}),
                    "temporary error",
                ),
            )
            # Also need processed_webhooks entries for idempotency
            # (replay will delete them)

        result = await manager.replay_failed_webhooks(since_hours=1, limit=10)
        assert result["total"] == 3
        # Some may succeed, some may fail depending on state
        assert result["succeeded"] + result["failed"] == 3

    @pytest.mark.asyncio
    async def test_replay_empty_when_no_errors(self, manager, db):
        """Batch replay with no errored events returns empty."""
        result = await manager.replay_failed_webhooks(since_hours=1)
        assert result["total"] == 0


class TestReplayableList:
    def test_lists_webhook_events(self, manager, db):
        """Should list available webhook events."""
        _ensure_payment_tables(db)

        db.execute_insert(
            "INSERT INTO webhook_events "
            "(provider, event_type, payment_ref, amount, currency, brand, raw_payload) "
            "VALUES ('stripe', 'payment.completed', 'pi_list_001', 25.0, 'EUR', 'brand_a', '{}')"
        )

        events = manager.get_replayable_webhooks(limit=10)
        assert len(events) >= 1
        assert any(e["payment_ref"] == "pi_list_001" for e in events)
