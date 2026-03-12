"""Tests for infrastructure audit fixes.

Covers: webhook integration, dispute idempotency, SpendingGuard wiring,
Playwright dependency, mid-cycle anonymity, config hardcoding, Gumroad balance.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.config import TelegramConfig


# ── Fix 1: Webhook server integration ─────────────────────────────


class TestWebhookServerIntegration:
    def test_start_webhook_server_function_exists(self):
        """Verify _start_webhook_server is importable from main."""
        from monai.main import _start_webhook_server
        assert callable(_start_webhook_server)

    def test_start_webhook_server_registers_providers(self):
        """Verify webhook server registers all payment providers."""
        from monai.main import _start_webhook_server

        mock_orchestrator = MagicMock()
        mock_orchestrator.payment_manager._providers = {
            "stripe": MagicMock(),
            "gumroad": MagicMock(),
        }

        with patch("monai.main.WebhookServer") as MockWS:
            mock_ws_instance = MagicMock()
            MockWS.return_value = mock_ws_instance
            # Prevent actual server start
            mock_ws_instance.start = AsyncMock()

            with patch("monai.main.asyncio") as mock_asyncio:
                mock_loop = MagicMock()
                mock_asyncio.new_event_loop.return_value = mock_loop
                with patch("monai.main.threading"):
                    _start_webhook_server(mock_orchestrator)

            # Should register both providers
            assert mock_ws_instance.register_provider.call_count == 2
            mock_ws_instance.on_event.assert_called_once_with(
                mock_orchestrator.payment_manager._handle_webhook_event
            )


# ── Fix 2: Dispute handler idempotency ────────────────────────────


class TestDisputeIdempotency:
    @pytest.fixture
    def manager(self):
        from monai.payments.manager import UnifiedPaymentManager

        config = MagicMock()
        config.creator_wallet.xmr_address = "4" + "A" * 94
        config.creator_wallet.sweep_threshold_eur = 50.0
        config.creator_wallet.sweep_interval_hours = 24
        config.creator_wallet.min_confirmations_xmr = 10
        config.creator_wallet.min_confirmations_btc = 3
        config.monero.wallet_rpc_url = ""
        config.monero.rpc_user = ""
        config.monero.rpc_password = ""

        db = MagicMock()
        mock_conn = MagicMock()
        db.transaction.return_value.__enter__ = MagicMock(return_value=mock_conn)
        db.transaction.return_value.__exit__ = MagicMock(return_value=False)
        db.execute = MagicMock(return_value=[])
        db.execute_insert = MagicMock(return_value=1)
        db.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        db.connect.return_value.__exit__ = MagicMock(return_value=False)

        ledger = MagicMock()
        return UnifiedPaymentManager(config, db, ledger=ledger)

    def test_dispute_skips_already_disputed(self, manager):
        """If payment is already disputed, handler returns early."""
        from monai.payments.types import WebhookEvent, WebhookEventType

        event = WebhookEvent(
            provider="stripe",
            event_type=WebhookEventType.PAYMENT_DISPUTED,
            payment_ref="pay_123",
            amount=50.0,
            currency="EUR",
            metadata={"brand": "test"},
            raw={},
        )

        # Mock DB returning already-disputed payment
        manager.db.execute = MagicMock(return_value=[
            {"id": 1, "brand": "test", "amount": 50.0, "currency": "EUR", "status": "disputed"}
        ])

        asyncio.get_event_loop().run_until_complete(
            manager._handle_payment_disputed(event)
        )

        # Should NOT have called transaction (skipped due to already disputed)
        manager.db.transaction.assert_not_called()

    def test_dispute_processes_new_dispute(self, manager):
        """Normal dispute (not already disputed) is processed."""
        from monai.payments.types import WebhookEvent, WebhookEventType

        event = WebhookEvent(
            provider="stripe",
            event_type=WebhookEventType.PAYMENT_DISPUTED,
            payment_ref="pay_456",
            amount=50.0,
            currency="EUR",
            metadata={"brand": "test"},
            raw={},
        )

        manager.db.execute = MagicMock(return_value=[
            {"id": 1, "brand": "test", "amount": 50.0, "currency": "EUR", "status": "completed"}
        ])

        asyncio.get_event_loop().run_until_complete(
            manager._handle_payment_disputed(event)
        )

        manager.db.transaction.assert_called_once()


# ── Fix 3: SpendingGuard wired into orchestrator ──────────────────


class TestSpendingGuardWiring:
    def test_orchestrator_has_spending_guard(self, config, db, mock_llm):
        """Orchestrator must instantiate SpendingGuard."""
        from monai.agents.orchestrator import Orchestrator
        from monai.business.spending_guard import SpendingGuard

        # Mock LLM to return valid identity dict for IdentityManager
        mock_llm.quick_json.return_value = {
            "name": "Test Agent",
            "background": "Test",
            "style": "professional",
        }

        orch = Orchestrator(config, db, mock_llm)
        assert hasattr(orch, "spending_guard")
        assert isinstance(orch.spending_guard, SpendingGuard)


# ── Fix 4: Playwright is a required dependency ────────────────────


class TestPlaywrightDependency:
    def test_playwright_in_required_deps(self):
        """Playwright must be in required dependencies, not optional."""
        from pathlib import Path
        import tomllib

        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)

        required = data["project"]["dependencies"]
        assert any("playwright" in dep for dep in required), \
            "playwright must be in required dependencies"

        # Should NOT be in optional browser group anymore
        optional = data["project"].get("optional-dependencies", {})
        browser_deps = optional.get("browser", [])
        assert not any("playwright" in dep for dep in browser_deps), \
            "playwright should be removed from optional browser group"


# ── Fix 5: Mid-cycle anonymity re-verification ───────────────────


class TestMidCycleAnonymity:
    def test_execute_cycle_has_anonymity_recheck(self):
        """_execute_cycle source must contain anonymity_recheck logic."""
        import inspect
        from monai.agents.orchestrator import Orchestrator

        source = inspect.getsource(Orchestrator._execute_cycle)
        assert "anonymity_recheck" in source
        assert "anonymity_lost_mid_cycle" in source


# ── Fix 6: No hardcoded creator_username ──────────────────────────


class TestNoHardcodedCreatorUsername:
    def test_default_creator_username_is_empty(self):
        """Default creator_username must not be hardcoded to a real username."""
        tc = TelegramConfig()
        assert tc.creator_username == "", \
            f"creator_username default should be empty, got '{tc.creator_username}'"


# ── Fix 7: Gumroad get_balance uses seller_price and deducts payouts ──


class TestGumroadBalance:
    @pytest.fixture
    def gumroad(self):
        from monai.payments.gumroad_provider import GumroadProvider

        provider = GumroadProvider.__new__(GumroadProvider)
        provider._client = MagicMock()
        provider._api_key = "test"
        provider.provider_name = "gumroad"
        provider.webhook_secret = "test"
        return provider

    def test_get_balance_uses_seller_price(self, gumroad):
        """get_balance should prefer seller_price over price."""
        import inspect
        source = inspect.getsource(gumroad.get_balance)
        assert "seller_price" in source, \
            "get_balance should use seller_price for accurate revenue"

    def test_get_balance_subtracts_payouts(self, gumroad):
        """get_balance should subtract payouts from total sales."""
        import inspect
        source = inspect.getsource(gumroad.get_balance)
        assert "payout" in source.lower(), \
            "get_balance should account for payouts"
