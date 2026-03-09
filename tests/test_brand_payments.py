"""Tests for BrandPayments — per-brand anonymous payment collection and sweeping."""

import json

import pytest

from monai.business.brand_payments import (
    BrandPayments,
    COLLECTION_METHODS,
    SWEEP_METHODS,
)
from monai.db.database import Database
from tests.conftest_schema import TEST_SCHEMA


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    with d.connect() as conn:
        conn.executescript(TEST_SCHEMA)
    return d


@pytest.fixture
def bp(db):
    return BrandPayments(db)


# ── Schema ────────────────────────────────────────────────────


class TestSchema:
    def test_creates_tables(self, bp, db):
        for table in ("brand_payment_accounts", "brand_payments_received",
                      "brand_profit_sweeps"):
            rows = db.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            assert len(rows) == 1, f"Table {table} not created"

    def test_collection_methods_defined(self):
        providers = {m["provider"] for m in COLLECTION_METHODS}
        assert "crypto_xmr" in providers
        assert "crypto_btc" in providers
        assert "stripe" in providers

    def test_sweep_methods_defined(self):
        methods = {m["method"] for m in SWEEP_METHODS}
        assert "crypto_xmr" in methods
        assert "crypto_btc_coinjoin" in methods


# ── Account Management ───────────────────────────────────────


class TestAccounts:
    def test_add_collection_account(self, bp):
        acc_id = bp.add_collection_account(
            "micro_saas", "crypto_xmr",
            "46abc123...wallet_address",
            label="Main XMR wallet",
        )
        assert acc_id > 0

    def test_add_sweep_account(self, bp):
        acc_id = bp.add_sweep_account(
            "micro_saas", "crypto_xmr",
            "creator_wallet_address",
            label="Creator XMR",
        )
        assert acc_id > 0

    def test_get_collection_accounts(self, bp):
        bp.add_collection_account("micro_saas", "crypto_xmr", "wallet1")
        bp.add_collection_account("micro_saas", "stripe", "acct_123")

        accounts = bp.get_collection_accounts("micro_saas")
        assert len(accounts) == 2

    def test_get_sweep_accounts(self, bp):
        bp.add_sweep_account("micro_saas", "crypto_xmr", "creator_wallet")

        accounts = bp.get_sweep_accounts("micro_saas")
        assert len(accounts) == 1
        assert accounts[0]["account_type"] == "sweep"

    def test_brand_isolation(self, bp):
        bp.add_collection_account("micro_saas", "crypto_xmr", "wallet_saas")
        bp.add_collection_account("newsletter", "crypto_xmr", "wallet_news")

        saas = bp.get_collection_accounts("micro_saas")
        news = bp.get_collection_accounts("newsletter")
        assert len(saas) == 1
        assert len(news) == 1
        assert saas[0]["account_id"] == "wallet_saas"

    def test_duplicate_account_ignored(self, bp):
        bp.add_collection_account("micro_saas", "crypto_xmr", "wallet1")
        bp.add_collection_account("micro_saas", "crypto_xmr", "wallet1")

        accounts = bp.get_collection_accounts("micro_saas")
        assert len(accounts) == 1

    def test_update_balance(self, bp):
        acc_id = bp.add_collection_account("micro_saas", "crypto_xmr", "wallet1")
        bp.update_account_balance(acc_id, 150.50)

        accounts = bp.get_collection_accounts("micro_saas")
        assert accounts[0]["balance"] == 150.50

    def test_deactivate_account(self, bp):
        acc_id = bp.add_collection_account("micro_saas", "crypto_xmr", "wallet1")
        bp.deactivate_account(acc_id)

        # Closed accounts don't show in active list
        accounts = bp.get_collection_accounts("micro_saas")
        assert len(accounts) == 0


# ── Payment Reception ────────────────────────────────────────


class TestPayments:
    def test_record_payment(self, bp):
        acc_id = bp.add_collection_account("micro_saas", "stripe", "acct_123")
        pay_id = bp.record_payment(
            "micro_saas", acc_id, 49.99,
            product="Pro Plan", customer_email="buyer@test.com",
            payment_ref="ch_abc123",
        )
        assert pay_id > 0

    def test_get_payments(self, bp):
        acc_id = bp.add_collection_account("micro_saas", "stripe", "acct_123")
        bp.record_payment("micro_saas", acc_id, 49.99, product="Plan A")
        bp.record_payment("micro_saas", acc_id, 29.99, product="Plan B")

        payments = bp.get_payments("micro_saas")
        assert len(payments) == 2

    def test_get_payments_brand_isolation(self, bp):
        acc1 = bp.add_collection_account("micro_saas", "stripe", "acct_1")
        acc2 = bp.add_collection_account("newsletter", "stripe", "acct_2")
        bp.record_payment("micro_saas", acc1, 49.99)
        bp.record_payment("newsletter", acc2, 9.99)

        assert len(bp.get_payments("micro_saas")) == 1
        assert len(bp.get_payments("newsletter")) == 1

    def test_get_brand_revenue(self, bp):
        acc_id = bp.add_collection_account("micro_saas", "stripe", "acct_123")
        bp.record_payment("micro_saas", acc_id, 49.99)
        bp.record_payment("micro_saas", acc_id, 29.99)

        rev = bp.get_brand_revenue("micro_saas")
        assert rev["transactions"] == 2
        assert abs(rev["total_revenue"] - 79.98) < 0.01

    def test_get_brand_revenue_empty(self, bp):
        rev = bp.get_brand_revenue("nonexistent")
        assert rev["transactions"] == 0

    def test_refund_payment(self, bp):
        acc_id = bp.add_collection_account("micro_saas", "stripe", "acct_123")
        pay_id = bp.record_payment("micro_saas", acc_id, 49.99)
        result = bp.refund_payment(pay_id)

        assert result["status"] == "refunded"
        payments = bp.get_payments("micro_saas", status="refunded")
        assert len(payments) == 1

    def test_record_payment_with_lead_id(self, bp):
        acc_id = bp.add_collection_account("micro_saas", "stripe", "acct_123")
        pay_id = bp.record_payment(
            "micro_saas", acc_id, 49.99, lead_id=42,
        )
        payments = bp.get_payments("micro_saas")
        assert payments[0]["lead_id"] == 42


# ── Profit Sweeping ──────────────────────────────────────────


class TestSweeping:
    def test_sweepable_balance(self, bp):
        acc_id = bp.add_collection_account("micro_saas", "crypto_xmr", "wallet1")
        bp.record_payment("micro_saas", acc_id, 100.0)
        bp.record_payment("micro_saas", acc_id, 50.0)

        balance = bp.get_sweepable_balance("micro_saas")
        assert balance == 150.0

    def test_sweepable_balance_after_sweep(self, bp):
        coll_id = bp.add_collection_account("micro_saas", "crypto_xmr", "wallet1")
        sweep_id = bp.add_sweep_account("micro_saas", "crypto_xmr", "creator_wallet")
        bp.record_payment("micro_saas", coll_id, 100.0)

        bp.initiate_sweep("micro_saas", coll_id, sweep_id, 60.0)
        bp.complete_sweep(1)

        balance = bp.get_sweepable_balance("micro_saas")
        assert balance == 40.0

    def test_initiate_sweep(self, bp):
        coll_id = bp.add_collection_account("micro_saas", "crypto_xmr", "wallet1")
        sweep_id_acc = bp.add_sweep_account("micro_saas", "crypto_xmr", "creator_wallet")

        sweep_id = bp.initiate_sweep(
            "micro_saas", coll_id, sweep_id_acc, 50.0,
            sweep_method="crypto_xmr",
        )
        assert sweep_id > 0

    def test_complete_sweep(self, bp):
        coll_id = bp.add_collection_account("micro_saas", "crypto_xmr", "wallet1")
        sweep_acc = bp.add_sweep_account("micro_saas", "crypto_xmr", "creator_wallet")

        sweep_id = bp.initiate_sweep("micro_saas", coll_id, sweep_acc, 50.0)
        bp.complete_sweep(sweep_id, tx_reference="tx_abc123")

        history = bp.get_sweep_history("micro_saas")
        assert len(history) == 1
        assert history[0]["status"] == "completed"
        assert history[0]["tx_reference"] == "tx_abc123"

    def test_mark_sweep_mixing(self, bp):
        coll_id = bp.add_collection_account("micro_saas", "crypto_btc", "wallet1")
        sweep_acc = bp.add_sweep_account("micro_saas", "crypto_btc", "creator_wallet")

        sweep_id = bp.initiate_sweep(
            "micro_saas", coll_id, sweep_acc, 50.0,
            sweep_method="crypto_btc_coinjoin",
        )
        bp.mark_sweep_mixing(sweep_id)

        history = bp.get_sweep_history("micro_saas")
        assert history[0]["status"] == "mixing"

    def test_fail_sweep(self, bp):
        coll_id = bp.add_collection_account("micro_saas", "crypto_xmr", "wallet1")
        sweep_acc = bp.add_sweep_account("micro_saas", "crypto_xmr", "creator_wallet")

        sweep_id = bp.initiate_sweep("micro_saas", coll_id, sweep_acc, 50.0)
        bp.fail_sweep(sweep_id, reason="insufficient funds")

        history = bp.get_sweep_history("micro_saas")
        assert history[0]["status"] == "failed"

    def test_get_sweep_history_all_brands(self, bp):
        c1 = bp.add_collection_account("micro_saas", "crypto_xmr", "w1")
        s1 = bp.add_sweep_account("micro_saas", "crypto_xmr", "cw1")
        c2 = bp.add_collection_account("newsletter", "crypto_xmr", "w2")
        s2 = bp.add_sweep_account("newsletter", "crypto_xmr", "cw2")

        bp.initiate_sweep("micro_saas", c1, s1, 50.0)
        bp.initiate_sweep("newsletter", c2, s2, 30.0)

        all_history = bp.get_sweep_history()
        assert len(all_history) == 2


# ── Analytics ─────────────────────────────────────────────────


class TestAnalytics:
    def test_all_brands_revenue(self, bp):
        a1 = bp.add_collection_account("micro_saas", "stripe", "acct_1")
        a2 = bp.add_collection_account("newsletter", "stripe", "acct_2")
        bp.record_payment("micro_saas", a1, 100.0)
        bp.record_payment("newsletter", a2, 50.0)

        revenue = bp.get_all_brands_revenue()
        assert len(revenue) == 2
        brands = {r["brand"]: r for r in revenue}
        assert brands["micro_saas"]["total_revenue"] == 100.0
        assert brands["newsletter"]["total_revenue"] == 50.0

    def test_revenue_by_provider(self, bp):
        a1 = bp.add_collection_account("micro_saas", "stripe", "acct_1")
        a2 = bp.add_collection_account("micro_saas", "crypto_xmr", "wallet_1")
        bp.record_payment("micro_saas", a1, 100.0)
        bp.record_payment("micro_saas", a2, 50.0)

        by_provider = bp.get_revenue_by_provider("micro_saas")
        assert len(by_provider) == 2
        providers = {r["provider"]: r for r in by_provider}
        assert providers["stripe"]["total_revenue"] == 100.0
        assert providers["crypto_xmr"]["total_revenue"] == 50.0

    def test_total_swept(self, bp):
        c = bp.add_collection_account("micro_saas", "crypto_xmr", "w1")
        s = bp.add_sweep_account("micro_saas", "crypto_xmr", "cw1")

        sid1 = bp.initiate_sweep("micro_saas", c, s, 50.0)
        sid2 = bp.initiate_sweep("micro_saas", c, s, 30.0)
        bp.complete_sweep(sid1, "tx1")
        bp.complete_sweep(sid2, "tx2")

        assert bp.get_total_swept("micro_saas") == 80.0
        assert bp.get_total_swept() == 80.0

    def test_total_swept_excludes_pending(self, bp):
        c = bp.add_collection_account("micro_saas", "crypto_xmr", "w1")
        s = bp.add_sweep_account("micro_saas", "crypto_xmr", "cw1")

        sid1 = bp.initiate_sweep("micro_saas", c, s, 50.0)
        bp.initiate_sweep("micro_saas", c, s, 30.0)  # stays pending
        bp.complete_sweep(sid1, "tx1")

        assert bp.get_total_swept("micro_saas") == 50.0

    def test_get_collection_methods(self, bp):
        methods = bp.get_collection_methods()
        assert len(methods) >= 4
        assert methods[0]["provider"] == "crypto_xmr"  # Monero first

    def test_get_sweep_methods(self, bp):
        methods = bp.get_sweep_methods()
        assert len(methods) >= 2
        assert methods[0]["method"] == "crypto_xmr"  # Monero first
