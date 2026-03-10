"""Tests for bootstrap funding — anonymous prepaid card + AI crowdfunding."""

import os
import tempfile
from pathlib import Path

import pytest

from monai.business.bootstrap import (
    BOOTSTRAP_CATEGORIES,
    INFRASTRUCTURE_CATEGORIES,
    NO_LLC_PLATFORMS,
    BootstrapWallet,
)
from monai.config import BootstrapWalletConfig, Config
from monai.db.database import Database


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(Path(path))
    yield database
    os.unlink(path)


@pytest.fixture
def config():
    cfg = Config()
    cfg.bootstrap_wallet = BootstrapWalletConfig(
        enabled=True,
        method="paysafecard",
        paysafecard_pin="1234567890123456",
        loaded_amount=50.0,
        spend_limit_per_tx=50.0,
        retired=False,
    )
    return cfg


@pytest.fixture
def wallet(config, db):
    return BootstrapWallet(config, db)


class TestPrepaidCardSpending:
    def test_can_spend_valid(self, wallet):
        result = wallet.can_spend_prepaid(10.0, "domain")
        assert result["allowed"] is True
        assert result["remaining_after"] == 40.0

    def test_can_spend_exceeds_limit(self, wallet):
        result = wallet.can_spend_prepaid(100.0, "domain")
        assert result["allowed"] is False
        assert "per-tx limit" in result["reason"]

    def test_can_spend_wrong_category(self, wallet):
        result = wallet.can_spend_prepaid(10.0, "llc_formation")
        assert result["allowed"] is False
        assert "not allowed for prepaid" in result["reason"]

    def test_can_spend_wallet_disabled(self, wallet):
        wallet.config.bootstrap_wallet.enabled = False
        result = wallet.can_spend_prepaid(10.0, "domain")
        assert result["allowed"] is False
        assert "not enabled" in result["reason"]

    def test_can_spend_wallet_retired(self, wallet):
        wallet.config.bootstrap_wallet.retired = True
        result = wallet.can_spend_prepaid(10.0, "domain")
        assert result["allowed"] is False
        assert "retired" in result["reason"]

    def test_spend_prepaid_success(self, wallet):
        result = wallet.spend_prepaid(10.0, "monai.fund domain", "domain", "Namecheap")
        assert "error" not in result
        assert result["source"] == "prepaid_card"
        assert result["remaining"] == 40.0

    def test_spend_prepaid_tracks_balance(self, wallet):
        wallet.spend_prepaid(10.0, "Domain", "domain")
        wallet.spend_prepaid(5.0, "Hosting", "hosting")

        assert wallet.get_prepaid_total_spent() == 15.0
        assert wallet.get_prepaid_remaining() == 35.0

    def test_spend_prepaid_insufficient(self, wallet):
        wallet.spend_prepaid(40.0, "Domain", "domain")

        result = wallet.spend_prepaid(15.0, "Hosting", "hosting")
        assert "error" in result
        assert "Insufficient" in result["error"]

    def test_retire_prepaid(self, wallet):
        wallet.retire_prepaid()
        assert wallet.config.bootstrap_wallet.retired is True

        result = wallet.can_spend_prepaid(1.0, "domain")
        assert result["allowed"] is False


class TestCrowdfunding:
    def test_create_campaign(self, wallet):
        cid = wallet.create_campaign(
            platform="kofi",
            title="Help monAI start its business",
            description="The first AI-funded startup",
            goal_amount=500.0,
            campaign_url="https://ko-fi.com/monai",
        )
        assert cid > 0

        campaign = wallet.get_campaign(cid)
        assert campaign["platform"] == "kofi"
        assert campaign["status"] == "active"
        assert campaign["goal_amount"] == 500.0
        assert campaign["raised_amount"] == 0

    def test_record_contribution(self, wallet):
        cid = wallet.create_campaign("kofi", "Test", "Test campaign", 100.0)
        wallet.record_contribution(cid, 25.0, backer_name="Alice", message="Go AI!")
        wallet.record_contribution(cid, 50.0, backer_name="Bob")

        campaign = wallet.get_campaign(cid)
        assert campaign["raised_amount"] == 75.0
        assert campaign["backer_count"] == 2

        contribs = wallet.get_campaign_contributions(cid)
        assert len(contribs) == 2

    def test_campaign_funded_status(self, wallet):
        cid = wallet.create_campaign("kofi", "Test", "Test", 50.0)
        wallet.record_contribution(cid, 30.0)
        assert wallet.get_campaign(cid)["status"] == "active"

        wallet.record_contribution(cid, 25.0)
        assert wallet.get_campaign(cid)["status"] == "funded"

    def test_crowdfunding_balance(self, wallet):
        cid = wallet.create_campaign("kofi", "Test", "Test", 500.0)
        wallet.record_contribution(cid, 100.0)
        wallet.record_contribution(cid, 150.0)

        assert wallet.get_crowdfunding_total_raised() == 250.0
        assert wallet.get_crowdfunding_available() == 250.0

    def test_spend_crowdfunding(self, wallet):
        cid = wallet.create_campaign("kofi", "Test", "Test", 500.0)
        wallet.record_contribution(cid, 200.0)

        result = wallet.spend_crowdfunding(100.0, "LLC formation", "llc_formation", "IncFile")
        assert "error" not in result
        assert result["remaining"] == 100.0

    def test_spend_crowdfunding_insufficient(self, wallet):
        cid = wallet.create_campaign("kofi", "Test", "Test", 500.0)
        wallet.record_contribution(cid, 50.0)

        result = wallet.spend_crowdfunding(100.0, "LLC formation", "llc_formation")
        assert "error" in result
        assert "Insufficient" in result["error"]

    def test_active_campaigns(self, wallet):
        wallet.create_campaign("kofi", "Ko-fi Campaign", "Test", 200.0)
        wallet.create_campaign("buymeacoffee", "BMC Campaign", "Test", 300.0)

        active = wallet.get_active_campaigns()
        assert len(active) == 2

    def test_contribution_recorded_as_bootstrap_tx(self, wallet):
        cid = wallet.create_campaign("kofi", "Test", "Test", 500.0)
        wallet.record_contribution(cid, 42.0, backer_name="Charlie")

        txs = wallet.get_all_transactions()
        cf_txs = [t for t in txs if t["source"] == "crowdfunding" and t["amount"] > 0]
        assert len(cf_txs) == 1
        assert cf_txs[0]["amount"] == 42.0


class TestFundingPhase:
    def test_pre_bootstrap(self, db):
        config = Config()  # Wallet not enabled
        w = BootstrapWallet(config, db)
        assert w.get_funding_phase() == "pre_bootstrap"

    def test_prepaid_active(self, wallet):
        assert wallet.get_funding_phase() == "prepaid_active"

    def test_crowdfunding_phase(self, wallet):
        wallet.config.bootstrap_wallet.retired = True
        cid = wallet.create_campaign("kofi", "Test", "Test", 500.0)
        wallet.record_contribution(cid, 10.0)
        assert wallet.get_funding_phase() == "crowdfunding"

    def test_self_sustaining(self, wallet, db):
        # Create a corporate entity with bank account
        from monai.business.corporate import CorporateManager
        corp = CorporateManager(db)
        eid = corp.create_entity("LLC", "llc_us", "US-WY")
        corp.update_entity_bank(eid, "Mercury", "****1234")

        assert wallet.get_funding_phase() == "self_sustaining"


class TestBootstrapSummary:
    def test_summary_structure(self, wallet):
        summary = wallet.get_bootstrap_summary()

        assert "phase" in summary
        assert "prepaid_card" in summary
        assert "crowdfunding" in summary
        assert "total_bootstrap_funds" in summary

        assert summary["prepaid_card"]["loaded"] == 50.0
        assert summary["prepaid_card"]["enabled"] is True

    def test_summary_with_spending(self, wallet):
        wallet.spend_prepaid(15.0, "Domain + hosting", "domain")
        cid = wallet.create_campaign("kofi", "Test", "Test", 500.0)
        wallet.record_contribution(cid, 100.0)

        summary = wallet.get_bootstrap_summary()
        assert summary["prepaid_card"]["spent"] == 15.0
        assert summary["crowdfunding"]["total_raised"] == 100.0
        assert summary["total_bootstrap_spent"] == 15.0


class TestPlatformConfigs:
    def test_kofi_no_llc_required(self):
        assert NO_LLC_PLATFORMS["kofi"]["requires_llc"] is False
        assert NO_LLC_PLATFORMS["kofi"]["fee_pct"] == 0.0

    def test_all_platforms_have_required_fields(self):
        for name, platform in NO_LLC_PLATFORMS.items():
            assert "name" in platform
            assert "url" in platform
            assert "fee_pct" in platform
            assert "requires_llc" in platform
            assert platform["requires_llc"] is False

    def test_categories_no_overlap(self):
        assert not BOOTSTRAP_CATEGORIES & INFRASTRUCTURE_CATEGORIES
