"""Tests for monai.agents.identity."""

import json

import pytest

from monai.agents.identity import IdentityManager


class TestIdentityManager:
    @pytest.fixture
    def identity(self, config, db, mock_llm):
        mock_llm.quick_json.return_value = {
            "name": "TestCorp Digital",
            "tagline": "AI-powered services",
            "description": "Digital services company",
            "preferred_username": "testcorp",
            "business_type": "digital_services",
        }
        return IdentityManager(config, db, mock_llm)

    def test_auto_creates_identity(self, identity):
        info = identity.get_identity()
        assert info["identifier"]  # Has a name (set by validator or fallback)

    def test_store_and_get_account(self, identity):
        aid = identity.store_account(
            "upwork", "testuser@upwork",
            credentials={"password": "secret"},
            metadata={"profile_url": "https://upwork.com/testuser"},
        )
        assert aid >= 1

        account = identity.get_account("upwork")
        assert account is not None
        assert account["identifier"] == "testuser@upwork"
        assert account["credentials"]["password"] == "secret"

    def test_get_nonexistent_account(self, identity):
        assert identity.get_account("nonexistent_platform") is None

    def test_has_account(self, identity):
        assert identity.has_account("fiverr") is False
        identity.store_account("fiverr", "test@fiverr")
        assert identity.has_account("fiverr") is True

    def test_store_api_key(self, identity):
        identity.store_api_key("stripe", "stripe_live_key", "sk_live_xxx", cost_monthly=0.0)
        key = identity.get_api_key("stripe")
        assert key == "sk_live_xxx"

    def test_get_missing_api_key(self, identity):
        assert identity.get_api_key("nonexistent") is None

    def test_store_domain(self, identity):
        did = identity.store_domain("example.ai", "namecheap", metadata={"price": 12.99})
        assert did >= 1

    def test_get_all_accounts(self, identity):
        identity.store_account("upwork", "u1")
        identity.store_account("fiverr", "f1")
        accounts = identity.get_all_accounts()
        # +1 for the auto-created agent_identity
        assert len(accounts) >= 3

    def test_monthly_resource_costs(self, identity):
        identity.store_api_key("svc1", "key1", "val1", cost_monthly=9.99)
        identity.store_api_key("svc2", "key2", "val2", cost_monthly=5.00)
        assert identity.get_monthly_resource_costs() == 14.99

    def test_generate_password(self, identity):
        p1 = identity.generate_password()
        p2 = identity.generate_password()
        assert len(p1) == 20
        assert p1 != p2

    def test_generate_email_alias(self, identity):
        alias = identity.generate_email_alias("example.com")
        assert "@example.com" in alias
        # Username is randomized for anti-correlation — just check it's non-empty
        assert len(alias.split("@")[0]) > 0
