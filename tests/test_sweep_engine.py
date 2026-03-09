"""Tests for the Sweep Engine — automated profit transfers to creator."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.payments.sweep_engine import SweepEngine
from monai.payments.types import SweepStatus


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.creator_wallet.xmr_address = "4" + "A" * 94
    config.creator_wallet.sweep_threshold_eur = 50.0
    config.creator_wallet.sweep_interval_hours = 24
    config.creator_wallet.min_confirmations_xmr = 10
    config.creator_wallet.min_confirmations_btc = 3
    config.monero.wallet_rpc_url = "http://127.0.0.1:18082"
    config.monero.rpc_user = ""
    config.monero.rpc_password = ""
    config.monero.proxy_url = ""
    return config


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.connect.return_value.__enter__ = MagicMock()
    db.connect.return_value.__exit__ = MagicMock()
    return db


class TestSweepEngine:
    def test_get_sweep_summary_no_wallet(self, mock_config, mock_db):
        mock_config.creator_wallet.xmr_address = ""
        engine = SweepEngine(mock_config, mock_db)
        summary = engine.get_sweep_summary()
        assert summary["creator_xmr_address"] == "NOT SET"

    def test_get_sweep_summary_with_wallet(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)

        # Mock brand_payments methods
        engine.brand_payments.get_total_swept = MagicMock(return_value=150.0)
        engine.brand_payments.get_sweep_history = MagicMock(return_value=[])

        summary = engine.get_sweep_summary()
        assert summary["total_swept_eur"] == 150.0
        assert summary["sweep_threshold_eur"] == 50.0
        assert summary["sweep_interval_hours"] == 24
        assert "4AAA" in summary["creator_xmr_address"]

    @pytest.mark.asyncio
    async def test_sweep_cycle_no_wallet(self, mock_config, mock_db):
        mock_config.creator_wallet.xmr_address = ""
        engine = SweepEngine(mock_config, mock_db)

        # Mock health check
        engine.monero.health_check = AsyncMock(return_value=True)

        results = await engine.run_sweep_cycle()
        assert results == []  # No sweeps because no wallet

    @pytest.mark.asyncio
    async def test_sweep_cycle_wallet_offline(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine.monero.health_check = AsyncMock(return_value=False)

        results = await engine.run_sweep_cycle()
        assert results == []

    @pytest.mark.asyncio
    async def test_sweep_brand_no_wallet(self, mock_config, mock_db):
        mock_config.creator_wallet.xmr_address = ""
        engine = SweepEngine(mock_config, mock_db)

        result = await engine.sweep_brand("test_brand")
        assert result.success is False
        assert "No creator XMR address" in result.error

    @pytest.mark.asyncio
    async def test_sweep_brand_nothing_to_sweep(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine.brand_payments.get_sweepable_balance = MagicMock(return_value=0.0)

        result = await engine.sweep_brand("test_brand")
        assert result.success is False
        assert "Nothing to sweep" in result.error

    @pytest.mark.asyncio
    async def test_sweep_brand_no_crypto_account(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine.brand_payments.get_sweepable_balance = MagicMock(return_value=100.0)
        engine.brand_payments.get_collection_accounts = MagicMock(return_value=[])

        result = await engine.sweep_brand("test_brand")
        assert result.success is False
        assert "No crypto account" in result.error

    def test_find_sweep_source_prefers_xmr(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine.brand_payments.get_collection_accounts = MagicMock(return_value=[
            {"id": 1, "provider": "crypto_btc", "account_id": "bc1..."},
            {"id": 2, "provider": "crypto_xmr", "account_id": "4AAA..."},
            {"id": 3, "provider": "stripe", "account_id": "acct_123"},
        ])

        source = engine._find_sweep_source("test")
        assert source["provider"] == "crypto_xmr"
        assert source["id"] == 2

    def test_find_sweep_source_falls_back_to_btc(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine.brand_payments.get_collection_accounts = MagicMock(return_value=[
            {"id": 1, "provider": "crypto_btc", "account_id": "bc1..."},
            {"id": 3, "provider": "stripe", "account_id": "acct_123"},
        ])

        source = engine._find_sweep_source("test")
        assert source["provider"] == "crypto_btc"

    def test_find_sweep_source_none_for_fiat_only(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine.brand_payments.get_collection_accounts = MagicMock(return_value=[
            {"id": 3, "provider": "stripe", "account_id": "acct_123"},
        ])

        source = engine._find_sweep_source("test")
        assert source is None

    def test_ensure_sweep_destination_creates_new(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine.brand_payments.get_sweep_accounts = MagicMock(return_value=[])
        engine.brand_payments.add_sweep_account = MagicMock(return_value=99)

        dest = engine._ensure_sweep_destination("brand", "4" + "X" * 94)
        assert dest["id"] == 99
        assert dest["provider"] == "crypto_xmr"

    def test_ensure_sweep_destination_reuses_existing(self, mock_config, mock_db):
        addr = "4" + "X" * 94
        engine = SweepEngine(mock_config, mock_db)
        engine.brand_payments.get_sweep_accounts = MagicMock(return_value=[
            {"id": 42, "account_id": addr, "provider": "crypto_xmr"},
        ])

        dest = engine._ensure_sweep_destination("brand", addr)
        assert dest["id"] == 42
