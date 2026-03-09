"""Tests for LLC provisioner agent."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.agents.llc_provisioner import (
    LLCProvisioner,
    JURISDICTION_CONFIGS,
    REGISTERED_AGENTS,
    BANK_OPTIONS,
)


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.llc.enabled = True
    config.llc.entity_name = "Test Ventures LLC"
    config.llc.entity_type = "llc_us"
    config.llc.jurisdiction = "US-WY"
    config.llc.contractor_alias = "Test Consulting"
    config.llc.contractor_service = "Management consulting"
    config.llc.contractor_rate_type = "percentage"
    config.llc.contractor_rate_percentage = 90.0
    config.llc.contractor_rate_amount = 0
    config.llc.contractor_payment_method = "bank_transfer"
    config.data_dir = Path(tempfile.mkdtemp())
    config.privacy.proxy_type = "none"
    config.llm.model = "gpt-4o"
    config.llm.model_mini = "gpt-4o-mini"
    config.llm.api_key = "test"
    config.llm.max_tokens = 4096
    config.llm.temperature = 0.7
    return config


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.connect.return_value.__enter__ = MagicMock()
    db.connect.return_value.__exit__ = MagicMock()
    db.execute = MagicMock(return_value=[])
    db.execute_insert = MagicMock(return_value=1)
    return db


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.chat.return_value = '{"name": "Alpine Ventures LLC"}'
    return llm


@pytest.fixture
def provisioner(mock_config, mock_db, mock_llm):
    with patch("monai.agents.llc_provisioner.AutonomousExecutor"), \
         patch("monai.agents.llc_provisioner.IdentityManager"):
        prov = LLCProvisioner(mock_config, mock_db, mock_llm)
    return prov


class TestJurisdictionConfig:
    def test_wyoming_config(self):
        wy = JURISDICTION_CONFIGS["US-WY"]
        assert wy["name"] == "Wyoming"
        assert wy["member_disclosure"] is False
        assert wy["filing_fee"] == 100

    def test_new_mexico_config(self):
        nm = JURISDICTION_CONFIGS["US-NM"]
        assert nm["annual_fee"] == 0
        assert nm["registered_agent_required"] is False

    def test_delaware_config(self):
        de = JURISDICTION_CONFIGS["US-DE"]
        assert de["filing_fee"] == 90


class TestRegisteredAgents:
    def test_incfile_config(self):
        agent = REGISTERED_AGENTS["incfile"]
        assert agent["online_signup"] is True
        assert agent["includes_formation"] is True
        assert agent["cost_yearly"] == 0

    def test_all_agents_have_url(self):
        for name, agent in REGISTERED_AGENTS.items():
            assert "url" in agent, f"Agent {name} missing url"
            assert agent["url"].startswith("https://")


class TestBankOptions:
    def test_mercury_config(self):
        mercury = BANK_OPTIONS["mercury"]
        assert mercury["monthly_fee"] == 0
        assert mercury["accepts_llc"] is True
        assert mercury["stripe_compatible"] is True


class TestLLCProvisioner:
    def test_init_creates_schema(self, provisioner, mock_db):
        # Schema creation happens in __init__
        mock_db.connect.return_value.__enter__.return_value.executescript.assert_called()

    def test_init_steps(self, provisioner, mock_db):
        provisioner._init_steps("Test LLC")
        # Should insert 8 steps
        assert mock_db.execute.call_count >= 8

    def test_get_provision_status_not_configured(self, provisioner, mock_config):
        mock_config.llc.entity_name = ""
        status = provisioner.get_provision_status()
        assert status["status"] == "not_configured"

    def test_get_provision_status_not_started(self, provisioner, mock_db):
        mock_db.execute.return_value = []
        status = provisioner.get_provision_status()
        assert status["status"] == "not_started"

    def test_get_provision_status_in_progress(self, provisioner, mock_db):
        mock_db.execute.return_value = [
            {"step_name": "check_name_availability", "status": "completed",
             "step_order": 1, "attempts": 1, "max_attempts": 3},
            {"step_name": "register_agent_account", "status": "completed",
             "step_order": 2, "attempts": 1, "max_attempts": 3},
            {"step_name": "file_llc_formation", "status": "failed",
             "step_order": 3, "attempts": 1, "max_attempts": 3},
            {"step_name": "apply_ein", "status": "pending",
             "step_order": 4, "attempts": 0, "max_attempts": 3},
            {"step_name": "open_bank_account", "status": "pending",
             "step_order": 5, "attempts": 0, "max_attempts": 3},
            {"step_name": "connect_stripe", "status": "pending",
             "step_order": 6, "attempts": 0, "max_attempts": 3},
            {"step_name": "setup_contractor", "status": "pending",
             "step_order": 7, "attempts": 0, "max_attempts": 3},
            {"step_name": "assign_brands", "status": "pending",
             "step_order": 8, "attempts": 0, "max_attempts": 3},
        ]

        status = provisioner.get_provision_status()
        assert status["completed"] == 2
        assert status["failed"] == 1
        assert status["progress_pct"] == 25
        assert status["current_step"] == "file_llc_formation"


class TestExtractEIN:
    def test_extract_ein_valid(self):
        text = "Your EIN is 12-3456789. Please save this."
        assert LLCProvisioner._extract_ein(text) == "12-3456789"

    def test_extract_ein_in_context(self):
        text = "Confirmation: EIN 87-1234567 has been assigned to Test LLC"
        assert LLCProvisioner._extract_ein(text) == "87-1234567"

    def test_extract_ein_not_found(self):
        text = "Your application is being processed"
        assert LLCProvisioner._extract_ein(text) == ""

    def test_extract_ein_multiple_takes_first(self):
        text = "Old EIN: 11-1111111. New EIN: 22-2222222."
        assert LLCProvisioner._extract_ein(text) == "11-1111111"


class TestStepExecution:
    @pytest.mark.asyncio
    async def test_step_setup_contractor(self, provisioner, mock_db):
        """Contractor setup doesn't need browser — it's pure DB."""
        provisioner.corporate.get_primary_entity = MagicMock(
            return_value={"id": 1, "name": "Test LLC"}
        )
        provisioner.corporate.get_active_contractor = MagicMock(return_value=None)
        provisioner.corporate.create_contractor = MagicMock(return_value=42)

        result = await provisioner._step_setup_contractor(
            "Test LLC", "US-WY", "incfile"
        )

        assert result["status"] == "completed"
        assert result["contractor_id"] == 42
        assert result["alias"] == "Test Consulting"

    @pytest.mark.asyncio
    async def test_step_setup_contractor_already_exists(self, provisioner):
        provisioner.corporate.get_primary_entity = MagicMock(
            return_value={"id": 1, "name": "Test LLC"}
        )
        provisioner.corporate.get_active_contractor = MagicMock(
            return_value={"id": 1, "alias": "Existing"}
        )

        result = await provisioner._step_setup_contractor(
            "Test LLC", "US-WY", "incfile"
        )

        assert result["status"] == "completed"
        assert result["contractor"] == "Existing"

    @pytest.mark.asyncio
    async def test_step_assign_brands(self, provisioner, mock_db):
        provisioner.corporate.get_primary_entity = MagicMock(
            return_value={"id": 1, "name": "Test LLC"}
        )
        provisioner.corporate.get_brand_entity = MagicMock(return_value=None)
        provisioner.corporate.assign_brand = MagicMock()

        mock_db.execute.return_value = [
            {"name": "newsletter_saas"},
            {"name": "micro_tools"},
        ]

        result = await provisioner._step_assign_brands(
            "Test LLC", "US-WY", "incfile"
        )

        assert result["status"] == "completed"
        assert len(result["assigned_brands"]) == 2
        assert "newsletter_saas" in result["assigned_brands"]

    @pytest.mark.asyncio
    async def test_step_assign_brands_no_entity(self, provisioner):
        provisioner.corporate.get_primary_entity = MagicMock(return_value=None)

        result = await provisioner._step_assign_brands(
            "Test LLC", "US-WY", "incfile"
        )

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_step_check_name_available(self, provisioner):
        provisioner.executor.execute_task = AsyncMock(return_value={
            "status": "completed",
            "result": "The name 'Test Ventures LLC' is available",
        })

        result = await provisioner._step_check_name(
            "Test Ventures LLC", "US-WY", "incfile"
        )

        assert result["status"] == "completed"
        assert result["name_available"] is True

    @pytest.mark.asyncio
    async def test_step_check_name_taken(self, provisioner):
        provisioner.executor.execute_task = AsyncMock(return_value={
            "status": "completed",
            "result": "Name is taken. Already registered.",
        })

        result = await provisioner._step_check_name(
            "Test Ventures LLC", "US-WY", "incfile"
        )

        assert result["status"] == "name_taken"
        assert "suggested_name" in result

    @pytest.mark.asyncio
    async def test_step_apply_ein_success(self, provisioner):
        provisioner.corporate.get_primary_entity = MagicMock(
            return_value={
                "id": 1, "name": "Test LLC",
                "formation_date": "2026-03-01",
            }
        )
        provisioner.identity.get_identity = MagicMock(
            return_value={"name": "Test Owner"}
        )
        provisioner.executor.execute_task = AsyncMock(return_value={
            "status": "completed",
            "result": "Your EIN is 98-7654321. Save this confirmation.",
        })

        result = await provisioner._step_apply_ein(
            "Test LLC", "US-WY", "incfile"
        )

        assert result["status"] == "completed"
        assert result["ein"] == "98-7654321"


class TestRunPipeline:
    def test_run_skips_completed_steps(self, provisioner, mock_db):
        """Verify completed steps are skipped on re-run."""
        mock_db.execute.return_value = [
            {"step_name": "check_name_availability", "status": "completed",
             "step_order": 1, "attempts": 1, "max_attempts": 3},
            {"step_name": "register_agent_account", "status": "completed",
             "step_order": 2, "attempts": 1, "max_attempts": 3},
            {"step_name": "file_llc_formation", "status": "pending",
             "step_order": 3, "attempts": 0, "max_attempts": 3},
        ]

        # Mock the executor for the remaining step
        provisioner.executor.execute_task = AsyncMock(return_value={
            "status": "completed", "result": "Filed",
        })
        provisioner.corporate.create_entity = MagicMock(return_value=1)

        provisioner.identity.get_account = MagicMock(return_value={
            "identifier": "test@test.com",
            "credentials": '{"password": "test"}',
        })

        result = provisioner.run()

        # First two steps should be marked completed without execution
        assert result["steps"]["check_name_availability"]["status"] == "completed"
        assert result["steps"]["register_agent_account"]["status"] == "completed"

    def test_run_stops_on_max_attempts(self, provisioner, mock_db):
        mock_db.execute.return_value = [
            {"step_name": "check_name_availability", "status": "failed",
             "step_order": 1, "attempts": 3, "max_attempts": 3,
             "error": "Network error"},
        ]

        result = provisioner.run()
        step = result["steps"]["check_name_availability"]
        assert step["status"] == "max_attempts_reached"
