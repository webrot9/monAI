"""Tests for monai.business.kofi — Ko-fi campaign automation."""

import pytest
from unittest.mock import MagicMock, patch

from monai.business.bootstrap import BootstrapWallet
from monai.business.kofi import KofiCampaignManager, CAMPAIGN_CONTENT


class TestKofiCampaignManager:
    @pytest.fixture
    def bootstrap(self, config, db):
        return BootstrapWallet(config, db)

    @pytest.fixture
    def kofi(self, config, db, mock_llm, bootstrap):
        return KofiCampaignManager(config, db, mock_llm, bootstrap_wallet=bootstrap)

    def test_init_creates_schema(self, kofi, db):
        """Schema should be created on init."""
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kofi_campaigns'"
        )
        assert len(rows) == 1

    def test_plan_no_campaign(self, kofi):
        """Plan should include full setup when no campaign exists."""
        plan = kofi.plan()
        assert len(plan) == 5
        assert "Register" in plan[0]

    def test_plan_existing_campaign(self, kofi, db, bootstrap):
        """Plan should be sync-only when campaign exists."""
        # Create a campaign
        campaign_id = bootstrap.create_campaign(
            platform="kofi",
            title="Test Campaign",
            description="Test",
            goal_amount=500.0,
            campaign_url="https://ko-fi.com/test",
        )
        db.execute_insert(
            "INSERT INTO kofi_campaigns (campaign_id, kofi_page_url, kofi_username, status) "
            "VALUES (?, 'https://ko-fi.com/test', 'test', 'live')",
            (campaign_id,),
        )
        plan = kofi.plan()
        assert len(plan) == 2
        assert "Sync" in plan[0]

    def test_get_active_campaign_none(self, kofi):
        """No active campaign returns None."""
        assert kofi._get_active_campaign() is None

    def test_get_active_campaign_exists(self, kofi, db, bootstrap):
        """Returns active campaign when it exists."""
        campaign_id = bootstrap.create_campaign(
            platform="kofi",
            title="Test",
            description="Test",
            campaign_url="https://ko-fi.com/monai",
        )
        db.execute_insert(
            "INSERT INTO kofi_campaigns (campaign_id, kofi_page_url, kofi_username, status) "
            "VALUES (?, 'https://ko-fi.com/monai', 'monai', 'live')",
            (campaign_id,),
        )
        result = kofi._get_active_campaign()
        assert result is not None
        assert result["kofi_username"] == "monai"
        assert result["status"] == "live"

    def test_campaign_content_template(self):
        """Campaign content template has all required fields."""
        assert CAMPAIGN_CONTENT["title"]
        assert CAMPAIGN_CONTENT["description"]
        assert CAMPAIGN_CONTENT["goal_amount"] == 500.0
        assert len(CAMPAIGN_CONTENT["tiers"]) == 3
        for tier in CAMPAIGN_CONTENT["tiers"]:
            assert "amount" in tier
            assert "name" in tier
            assert "description" in tier

    def test_get_campaign_status_no_campaign(self, kofi):
        """Status returns no_campaign when none exists."""
        status = kofi.get_campaign_status()
        assert status["status"] == "no_campaign"

    def test_get_campaign_status_with_campaign(self, kofi, db, bootstrap):
        """Status returns full progress info."""
        campaign_id = bootstrap.create_campaign(
            platform="kofi",
            title="Test",
            description="Test",
            goal_amount=500.0,
            campaign_url="https://ko-fi.com/monai",
        )
        # Add some contributions
        bootstrap.record_contribution(campaign_id, 50.0, "Supporter1")
        bootstrap.record_contribution(campaign_id, 100.0, "Supporter2")

        db.execute_insert(
            "INSERT INTO kofi_campaigns (campaign_id, kofi_page_url, kofi_username, status) "
            "VALUES (?, 'https://ko-fi.com/monai', 'monai', 'live')",
            (campaign_id,),
        )

        status = kofi.get_campaign_status()
        assert status["status"] == "live"
        assert status["raised"] == 150.0
        assert status["goal"] == 500.0
        assert status["backers"] == 2
        assert status["progress_pct"] == 30.0
        assert status["kofi_url"] == "https://ko-fi.com/monai"
