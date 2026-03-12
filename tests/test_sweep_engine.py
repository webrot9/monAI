"""Tests for the Sweep Engine — dual-flow profit transfer system."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.payments.sweep_engine import SweepEngine
from monai.payments.types import SweepStatus


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.creator_wallet.xmr_address = ""  # No crypto by default
    config.creator_wallet.sweep_threshold_eur = 50.0
    config.creator_wallet.sweep_interval_hours = 24
    config.creator_wallet.min_confirmations_xmr = 10
    config.creator_wallet.min_confirmations_btc = 3
    config.monero.wallet_rpc_url = ""  # No Monero by default
    config.monero.rpc_user = ""
    config.monero.rpc_password = ""
    config.monero.proxy_url = ""
    config.llc.enabled = False
    config.retoswap.enabled = False  # No RetoSwap by default
    return config


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.connect.return_value.__enter__ = MagicMock()
    db.connect.return_value.__exit__ = MagicMock()
    db.execute = MagicMock(return_value=[])
    return db


class TestFlowDetection:
    def test_no_flow_configured(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = []
        assert engine.get_active_flow() == "none"

    def test_crypto_flow(self, mock_config, mock_db):
        mock_config.creator_wallet.xmr_address = "4" + "A" * 94
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = []
        assert engine.get_active_flow() == "crypto_xmr"

    def test_llc_flow_takes_priority(self, mock_config, mock_db):
        mock_config.creator_wallet.xmr_address = "4" + "A" * 94  # Both configured
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = [{"id": 1, "name": "LLC"}]
        engine._corporate.get_active_contractor.return_value = {"id": 1, "alias": "Creator"}
        assert engine.get_active_flow() == "llc_contractor"

    def test_llc_without_contractor_falls_to_crypto(self, mock_config, mock_db):
        mock_config.creator_wallet.xmr_address = "4" + "A" * 94
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = [{"id": 1, "name": "LLC"}]
        engine._corporate.get_active_contractor.return_value = None
        assert engine.get_active_flow() == "crypto_xmr"


class TestLLCSweepCycle:
    @pytest.mark.asyncio
    async def test_llc_sweep_cycle(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        entity = {"id": 1, "name": "Holdings LLC"}
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = [entity]
        engine._corporate.get_primary_entity.return_value = entity
        engine._corporate.get_active_contractor.return_value = {
            "id": 1, "alias": "Creator",
        }
        engine._corporate.get_entity_brands.return_value = [
            {"brand": "saas_brand"},
        ]
        engine._corporate.get_brand_entity.return_value = entity
        engine._corporate.get_expense_total.return_value = 0
        engine._corporate.get_recurring_expenses.return_value = []
        engine._corporate.get_overdue_obligations.return_value = []
        engine.brand_payments.get_all_brands_revenue = MagicMock(return_value=[
            {"brand": "saas_brand", "total_revenue": 500.0, "transactions": 10},
        ])
        mock_config.llc.multi_llc = False

        # Mock no existing invoices
        mock_db.execute.return_value = []

        result = await engine.run_sweep_cycle()
        assert result["flow"] == "llc_contractor"
        assert result["status"] == "ok"
        assert result["total_revenue"] == 500.0

    @pytest.mark.asyncio
    async def test_llc_sweep_no_entity(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        entity = {"id": 1, "name": "LLC"}
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = [entity]
        engine._corporate.get_primary_entity.return_value = entity
        engine._corporate.get_active_contractor.return_value = {"id": 1, "alias": "X"}
        engine._corporate.get_entity_brands.return_value = []
        engine._corporate.get_expense_total.return_value = 0
        engine._corporate.get_recurring_expenses.return_value = []
        engine._corporate.get_overdue_obligations.return_value = []
        engine.brand_payments.get_all_brands_revenue = MagicMock(return_value=[])
        mock_config.llc.multi_llc = False
        mock_db.execute.return_value = []

        result = await engine.run_sweep_cycle()
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_llc_sweep_with_expenses(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        entity = {"id": 1, "name": "Holdings LLC"}
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = [entity]
        engine._corporate.get_primary_entity.return_value = entity
        engine._corporate.get_active_contractor.return_value = {"id": 1, "alias": "Creator"}
        engine._corporate.get_brand_entity.return_value = entity
        engine._corporate.get_expense_total.return_value = 1500.0
        engine._corporate.get_recurring_expenses.return_value = [
            {"description": "AWS", "amount": 50.0},
        ]
        engine._corporate.get_overdue_obligations.return_value = []
        engine.brand_payments.get_all_brands_revenue = MagicMock(return_value=[
            {"brand": "brand_a", "total_revenue": 3000.0, "transactions": 20},
        ])
        mock_config.llc.multi_llc = False
        mock_db.execute.return_value = []

        result = await engine.run_sweep_cycle()
        assert result["total_expenses_via_llc"] == 1500.0
        assert result["recurring_expenses"] == 1

    @pytest.mark.asyncio
    async def test_llc_sweep_reports_overdue_tax(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        entity = {"id": 1, "name": "LLC"}
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = [entity]
        engine._corporate.get_primary_entity.return_value = entity
        engine._corporate.get_active_contractor.return_value = {"id": 1, "alias": "X"}
        engine._corporate.get_expense_total.return_value = 0
        engine._corporate.get_recurring_expenses.return_value = []
        engine._corporate.get_overdue_obligations.return_value = [
            {"obligation_type": "form_5472", "due_date": "2025-04-15", "jurisdiction": "US"},
        ]
        engine.brand_payments.get_all_brands_revenue = MagicMock(return_value=[])
        mock_config.llc.multi_llc = False
        mock_db.execute.return_value = []

        result = await engine.run_sweep_cycle()
        assert result["overdue_tax_obligations"] == 1


class TestCryptoSweepCycle:
    @pytest.mark.asyncio
    async def test_crypto_sweep_no_monero(self, mock_config, mock_db):
        mock_config.creator_wallet.xmr_address = "4" + "A" * 94
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = []

        result = await engine.run_sweep_cycle()
        assert result["status"] == "error"
        assert "monero_not_configured" in result.get("reason", "")

    @pytest.mark.asyncio
    async def test_sweep_brand_no_wallet(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = []
        mock_config.creator_wallet.xmr_address = ""

        result = await engine.sweep_brand("test")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_sweep_brand_llc_mode(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = [{"id": 1, "name": "LLC"}]
        engine._corporate.get_active_contractor.return_value = {"id": 1, "alias": "X"}

        result = await engine.sweep_brand("test")
        assert result.success is True
        assert result.metadata.get("flow") == "llc_contractor"


class TestNoFlowConfigured:
    @pytest.mark.asyncio
    async def test_sweep_cycle_no_config(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = []

        result = await engine.run_sweep_cycle()
        assert result["status"] == "skipped"
        assert "no_payout_method" in result["reason"]


class TestSweepSummary:
    def test_summary_no_flow(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = []
        engine.brand_payments.get_total_swept = MagicMock(return_value=0)
        engine.brand_payments.get_sweep_history = MagicMock(return_value=[])

        summary = engine.get_sweep_summary()
        assert summary["active_flow"] == "none"

    def test_summary_llc_flow(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        entity = {"id": 1, "name": "Test LLC"}
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = [entity]
        engine._corporate.get_primary_entity.return_value = entity
        engine._corporate.get_active_contractor.return_value = {"id": 1, "alias": "Creator"}
        engine._corporate.get_total_paid_to_contractor.return_value = 5000.0
        engine._corporate.get_expense_total.return_value = 1200.0
        engine._corporate.get_overdue_obligations.return_value = []
        engine.brand_payments.get_total_swept = MagicMock(return_value=0)
        engine.brand_payments.get_sweep_history = MagicMock(return_value=[])

        summary = engine.get_sweep_summary()
        assert summary["active_flow"] == "llc_contractor"
        assert summary["llc_name"] == "Test LLC"
        assert summary["total_paid_to_contractor"] == 5000.0
        assert summary["total_expenses_via_llc"] == 1200.0
        assert summary["entities_count"] == 1

    def test_summary_crypto_flow(self, mock_config, mock_db):
        mock_config.creator_wallet.xmr_address = "4" + "X" * 94
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = []
        engine.brand_payments.get_total_swept = MagicMock(return_value=2.5)
        engine.brand_payments.get_sweep_history = MagicMock(return_value=[])

        summary = engine.get_sweep_summary()
        assert summary["active_flow"] == "crypto_xmr"
        assert "4XXX" in summary["creator_xmr_address"]


class TestMultiLLCRotation:
    def test_single_llc_uses_primary(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        entity = {"id": 1, "name": "Primary LLC"}
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = [entity]
        engine._corporate.get_primary_entity.return_value = entity
        mock_config.llc.multi_llc = False

        target = engine._get_invoice_target_entity()
        assert target["name"] == "Primary LLC"

    def test_multi_llc_picks_least_invoiced(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        entity_a = {"id": 1, "name": "LLC A"}
        entity_b = {"id": 2, "name": "LLC B"}
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = [entity_a, entity_b]
        engine._corporate.get_active_contractor.return_value = {"id": 1, "alias": "Creator"}
        mock_config.llc.multi_llc = True

        # LLC A has 5 recent invoices, LLC B has 2
        def mock_execute(query, params):
            entity_id = params[0]
            if entity_id == 1:
                return [{"cnt": 5}]
            return [{"cnt": 2}]
        mock_db.execute = mock_execute

        target = engine._get_invoice_target_entity()
        assert target["name"] == "LLC B"

    def test_multi_llc_no_entities(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine._corporate = MagicMock()
        engine._corporate.get_all_entities.return_value = []
        mock_config.llc.multi_llc = True

        assert engine._get_invoice_target_entity() is None


class TestHelpers:
    def test_find_sweep_source_prefers_xmr(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine.brand_payments.get_collection_accounts = MagicMock(return_value=[
            {"id": 1, "provider": "crypto_btc"},
            {"id": 2, "provider": "crypto_xmr"},
        ])
        assert engine._find_sweep_source("test")["provider"] == "crypto_xmr"

    def test_find_sweep_source_none(self, mock_config, mock_db):
        engine = SweepEngine(mock_config, mock_db)
        engine.brand_payments.get_collection_accounts = MagicMock(return_value=[
            {"id": 1, "provider": "stripe"},
        ])
        assert engine._find_sweep_source("test") is None

    def test_ensure_sweep_destination_reuses(self, mock_config, mock_db):
        addr = "4" + "X" * 94
        engine = SweepEngine(mock_config, mock_db)
        engine.brand_payments.get_sweep_accounts = MagicMock(return_value=[
            {"id": 42, "account_id": addr, "provider": "crypto_xmr"},
        ])
        dest = engine._ensure_sweep_destination("brand", addr)
        assert dest["id"] == 42
