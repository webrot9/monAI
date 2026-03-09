"""Tests for the Phone Provisioner agent."""

from unittest.mock import MagicMock

import pytest

from monai.config import Config
from monai.db.database import Database
from monai.agents.phone_provisioner import PhoneProvisioner


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.quick.return_value = "test"
    llm.chat_json.return_value = {}
    return llm


class TestPhoneProvisioner:
    def test_schema_created(self, config, db, mock_llm):
        pp = PhoneProvisioner(config, db, mock_llm)
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='virtual_phones'"
        )
        assert len(rows) == 1

    def test_get_number_no_inventory(self, config, db, mock_llm):
        pp = PhoneProvisioner(config, db, mock_llm)
        result = pp.get_number("upwork", "test_agent")
        assert result["status"] == "pending_api_integration"
        assert result["platform"] == "upwork"

    def test_get_number_reuses_existing(self, config, db, mock_llm):
        pp = PhoneProvisioner(config, db, mock_llm)
        # Insert an unused number
        db.execute_insert(
            "INSERT INTO virtual_phones (provider, phone_number, status) "
            "VALUES ('smspool', '+1234567890', 'active')"
        )

        result = pp.get_number("upwork", "test_agent")
        assert result["status"] == "reused"
        assert result["phone_number"] == "+1234567890"

        # Verify it's now marked as used
        rows = db.execute("SELECT * FROM virtual_phones WHERE id = ?", (result["phone_id"],))
        assert rows[0]["used_for_platform"] == "upwork"
        assert rows[0]["used_by_agent"] == "test_agent"

    def test_check_verification_not_found(self, config, db, mock_llm):
        pp = PhoneProvisioner(config, db, mock_llm)
        result = pp.check_verification(999)
        assert result["status"] == "not_found"

    def test_check_verification_waiting(self, config, db, mock_llm):
        pp = PhoneProvisioner(config, db, mock_llm)
        phone_id = db.execute_insert(
            "INSERT INTO virtual_phones (provider, phone_number, status) "
            "VALUES ('smspool', '+1234567890', 'active')"
        )
        result = pp.check_verification(phone_id)
        assert result["status"] == "waiting"

    def test_check_verification_received(self, config, db, mock_llm):
        pp = PhoneProvisioner(config, db, mock_llm)
        phone_id = db.execute_insert(
            "INSERT INTO virtual_phones (provider, phone_number, status, verification_code) "
            "VALUES ('smspool', '+1234567890', 'active', '123456')"
        )
        result = pp.check_verification(phone_id)
        assert result["status"] == "received"
        assert result["code"] == "123456"

    def test_release_number(self, config, db, mock_llm):
        pp = PhoneProvisioner(config, db, mock_llm)
        phone_id = db.execute_insert(
            "INSERT INTO virtual_phones (provider, phone_number, status) "
            "VALUES ('smspool', '+1234567890', 'active')"
        )
        pp.release_number(phone_id)

        rows = db.execute("SELECT status FROM virtual_phones WHERE id = ?", (phone_id,))
        assert rows[0]["status"] == "released"

    def test_inventory_empty(self, config, db, mock_llm):
        pp = PhoneProvisioner(config, db, mock_llm)
        inv = pp.get_inventory()
        assert inv == {}

    def test_inventory_with_data(self, config, db, mock_llm):
        pp = PhoneProvisioner(config, db, mock_llm)
        db.execute_insert(
            "INSERT INTO virtual_phones (provider, phone_number, status) "
            "VALUES ('smspool', '+1', 'active')"
        )
        db.execute_insert(
            "INSERT INTO virtual_phones (provider, phone_number, status) "
            "VALUES ('smspool', '+2', 'active')"
        )
        db.execute_insert(
            "INSERT INTO virtual_phones (provider, phone_number, status) "
            "VALUES ('smspool', '+3', 'released')"
        )

        inv = pp.get_inventory()
        assert inv["active"] == 2
        assert inv["released"] == 1

    def test_costs_empty(self, config, db, mock_llm):
        pp = PhoneProvisioner(config, db, mock_llm)
        costs = pp.get_costs()
        assert costs["total_numbers"] == 0
