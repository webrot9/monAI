"""Tests for APIProvisioner — autonomous payment provider account creation.

Covers:
- Schema initialization (brand_api_keys table)
- Plan generation (detecting unprovisioned brands/providers)
- Key storage and retrieval (encrypted)
- Key rotation (marking old keys, storing new)
- Provider dispatching
- Webhook URL building
- Brand email resolution
- Result key parsing (multiple formats)
- BTCPay provisioning (API-based)
- provision_all orchestration
- Determine needed providers
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from monai.agents.api_provisioner import (
    API_PROVISIONER_SCHEMA,
    PROVIDER_CONFIG,
    APIProvisioner,
)
from monai.config import Config


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def provisioner(config, db, mock_llm):
    """APIProvisioner with mocked dependencies."""
    mock_llm.quick_json.return_value = {
        "name": "TestBrand Digital",
        "tagline": "AI-powered services",
        "description": "Digital services company",
        "preferred_username": "testbrand",
        "business_type": "digital_services",
    }
    with patch("monai.agents.api_provisioner.EmailVerifier") as mock_ev, \
         patch("monai.agents.api_provisioner.IdentityManager") as mock_im:
        mock_ev_instance = MagicMock()
        mock_ev.return_value = mock_ev_instance
        mock_ev_instance.create_temp_email.return_value = {
            "status": "created",
            "address": "temp@mail.tm",
            "password": "pass123",
            "domain": "mail.tm",
        }
        mock_ev_instance.wait_for_verification.return_value = {
            "status": "found",
            "verification_type": "link",
            "verification_value": "https://example.com/verify",
        }

        mock_im_instance = MagicMock()
        mock_im.return_value = mock_im_instance
        mock_im_instance.get_identity.return_value = {
            "name": "TestBrand",
            "email": "test@testbrand.com",
            "country": "US",
        }
        mock_im_instance.generate_password.return_value = "SecureP@ss123!"
        mock_im_instance.generate_email_alias.return_value = "alias123@testbrand.com"
        mock_im_instance.get_account.return_value = None

        prov = APIProvisioner(config, db, mock_llm)
        # Replace the mocks (since __init__ calls super which creates its own)
        prov.email_verifier = mock_ev_instance
        prov.identity = mock_im_instance

        # Ensure identities table exists (normally created by IdentityManager)
        with db.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS identities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    identifier TEXT NOT NULL,
                    credentials TEXT,
                    status TEXT DEFAULT 'active',
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

        yield prov


@pytest.fixture
def provisioner_with_pm(provisioner):
    """Provisioner with a mock payment manager attached."""
    pm = MagicMock()
    provisioner.payment_manager = pm
    return provisioner


def _seed_brand(db, brand="testbrand"):
    """Insert a brand into brand_payment_accounts so plan() finds it."""
    db.execute_insert(
        "INSERT OR IGNORE INTO brand_payment_accounts "
        "(brand, provider, account_type, account_id, status) "
        "VALUES (?, 'manual', 'collection', 'acc-1', 'active')",
        (brand,),
    )


# ── Schema ───────────────────────────────────────────────────────────


class TestSchema:
    def test_brand_api_keys_table_created(self, provisioner, db):
        """Schema should create brand_api_keys table."""
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='brand_api_keys'"
        )
        assert len(rows) == 1

    def test_unique_constraint(self, provisioner, db):
        """brand+provider+key_type+status should be unique."""
        db.execute_insert(
            "INSERT INTO brand_api_keys (brand, provider, key_type, key_value, status) "
            "VALUES ('b1', 'stripe', 'secret', 'enc_val', 'active')",
        )
        # Same combo should fail or replace (INSERT OR REPLACE used in prod code)
        info = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='brand_api_keys'"
        )
        assert "UNIQUE" in info[0]["sql"]


# ── Plan Generation ──────────────────────────────────────────────────


class TestPlan:
    def test_plan_identifies_unprovisioned_providers(self, provisioner, db):
        """Plan should list providers that haven't been provisioned yet."""
        _seed_brand(db, "acme")
        steps = provisioner.plan()
        # Should suggest all providers from PROVIDER_CONFIG for 'acme'
        provider_names = [s.split(":")[0].replace("provision_", "") for s in steps]
        for provider in PROVIDER_CONFIG:
            assert provider in provider_names

    def test_plan_skips_already_provisioned(self, provisioner, db):
        """Providers with active keys should not appear in plan."""
        _seed_brand(db, "acme")
        # Simulate Stripe already provisioned
        db.execute_insert(
            "INSERT INTO brand_api_keys (brand, provider, key_type, key_value, status) "
            "VALUES ('acme', 'stripe', 'secret', 'enc_val', 'active')",
        )
        steps = provisioner.plan()
        stripe_steps = [s for s in steps if "stripe" in s and "acme" in s]
        assert len(stripe_steps) == 0

    def test_plan_empty_with_no_brands(self, provisioner):
        """No active brands = no provisioning steps."""
        steps = provisioner.plan()
        assert steps == []


# ── Key Storage & Retrieval ──────────────────────────────────────────


class TestKeyStorage:
    def test_store_and_retrieve_keys(self, provisioner, db):
        """Stored keys should be retrievable via get_active_keys."""
        with patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            provisioner._store_provider_keys(
                brand="mybrand",
                provider="stripe",
                keys={"publishable": "pk_test_123", "secret": "sk_test_456"},
                webhook_secret="whsec_789",
                account_email="me@brand.com",
            )

        active = provisioner._get_active_keys("mybrand", "stripe")
        assert len(active) == 2
        key_types = {k["key_type"] for k in active}
        assert key_types == {"publishable", "secret"}

    def test_get_decrypted_key(self, provisioner, db):
        """get_decrypted_key should decrypt the stored value."""
        with patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            provisioner._store_provider_keys(
                brand="mybrand",
                provider="stripe",
                keys={"secret": "sk_test_real"},
            )

        with patch("monai.agents.api_provisioner.decrypt_value", side_effect=lambda v: v.replace("ENC:", "")):
            result = provisioner.get_decrypted_key("mybrand", "stripe", "secret")
            assert result == "sk_test_real"

    def test_get_decrypted_key_not_found(self, provisioner):
        """Missing key should return None."""
        result = provisioner.get_decrypted_key("nobody", "stripe", "secret")
        assert result is None

    def test_get_webhook_secret(self, provisioner, db):
        """Webhook secret should be retrievable and decrypted."""
        with patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            provisioner._store_provider_keys(
                brand="mybrand",
                provider="stripe",
                keys={"secret": "sk_test"},
                webhook_secret="whsec_test",
            )

        with patch("monai.agents.api_provisioner.decrypt_value", side_effect=lambda v: v.replace("ENC:", "")):
            secret = provisioner.get_webhook_secret("mybrand", "stripe")
            assert secret == "whsec_test"

    def test_webhook_secret_none_when_missing(self, provisioner):
        assert provisioner.get_webhook_secret("nobody", "stripe") is None


# ── Key Rotation ─────────────────────────────────────────────────────


class TestKeyRotation:
    def test_mark_keys_rotated(self, provisioner, db):
        """Rotating should change status from active to rotated."""
        with patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            provisioner._store_provider_keys(
                brand="mybrand", provider="stripe",
                keys={"secret": "sk_old"},
            )

        # Verify active
        assert len(provisioner._get_active_keys("mybrand", "stripe")) == 1

        provisioner._mark_keys_rotated("mybrand", "stripe")

        # Should be gone from active
        assert len(provisioner._get_active_keys("mybrand", "stripe")) == 0

        # But still in DB as rotated
        rows = db.execute(
            "SELECT status FROM brand_api_keys WHERE brand='mybrand' AND provider='stripe'"
        )
        assert rows[0]["status"] == "rotated"

    def test_rotate_keys_no_active_keys(self, provisioner):
        """Rotation with no active keys should return error."""
        result = provisioner.rotate_keys("nobody", "stripe")
        assert result["status"] == "error"
        assert "No active keys" in result["error"]


# ── Webhook URL Building ────────────────────────────────────────────


class TestWebhookURL:
    def test_webhook_url_with_brand_domain(self, provisioner, db):
        """Should use brand's domain for webhook URL."""
        # Create identities table with a domain
        db.execute(
            "CREATE TABLE IF NOT EXISTS identities ("
            "id INTEGER PRIMARY KEY, identifier TEXT, type TEXT, "
            "metadata TEXT, status TEXT DEFAULT 'active', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute_insert(
            "INSERT INTO identities (identifier, type, platform, metadata, status) "
            "VALUES ('mybrand.com', 'domain', 'namecheap', '{\"brand\": \"mybrand\"}', 'active')",
        )

        url = provisioner._build_webhook_url("mybrand", "stripe")
        assert "mybrand.com" in url
        assert "/webhooks/stripe" in url
        assert "brand=mybrand" in url

    def test_webhook_url_fallback_to_placeholder(self, provisioner, db):
        """Without any domain, should use placeholder."""
        # Ensure no identities table
        try:
            db.execute("DROP TABLE IF EXISTS identities")
        except Exception:
            pass
        db.execute(
            "CREATE TABLE IF NOT EXISTS identities ("
            "id INTEGER PRIMARY KEY, identifier TEXT, type TEXT, "
            "metadata TEXT, status TEXT DEFAULT 'active', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )

        # No btcpay config
        provisioner.config = MagicMock()
        provisioner.config.btcpay.server_url = ""

        url = provisioner._build_webhook_url("mybrand", "stripe")
        assert "webhooks.example.com" in url

    def test_webhook_url_brand_slug(self, provisioner, db):
        """Brand with spaces should be slugified."""
        db.execute(
            "CREATE TABLE IF NOT EXISTS identities ("
            "id INTEGER PRIMARY KEY, identifier TEXT, type TEXT, "
            "metadata TEXT, status TEXT DEFAULT 'active', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        provisioner.config = MagicMock()
        provisioner.config.btcpay.server_url = ""

        url = provisioner._build_webhook_url("My Brand Name", "gumroad")
        assert "brand=my-brand-name" in url


# ── Result Key Parsing ───────────────────────────────────────────────


class TestKeyParsing:
    def test_parse_direct_keys(self, provisioner):
        """Should extract keys from direct result dict."""
        result = {"publishable_key": "pk_123", "secret_key": "sk_456"}
        keys = provisioner._parse_keys_from_result(result, ["publishable_key", "secret_key"])
        assert keys["publishable_key"] == "pk_123"
        assert keys["secret_key"] == "sk_456"

    def test_parse_nested_result(self, provisioner):
        """Should extract from nested 'result' field."""
        result = {"result": {"access_token": "at_789"}}
        keys = provisioner._parse_keys_from_result(result, ["access_token"])
        assert keys["access_token"] == "at_789"

    def test_parse_json_string_result(self, provisioner):
        """Should handle JSON string in result field."""
        result = {"result": json.dumps({"api_key": "key_abc"})}
        keys = provisioner._parse_keys_from_result(result, ["api_key"])
        assert keys["api_key"] == "key_abc"

    def test_parse_nested_data_field(self, provisioner):
        """Should extract from result.data nested dict."""
        result = {"data": {"publishable_key": "pk_data"}}
        keys = provisioner._parse_keys_from_result(result, ["publishable_key"])
        assert keys["publishable_key"] == "pk_data"

    def test_parse_output_field(self, provisioner):
        """Should extract from output JSON string."""
        result = {"output": json.dumps({"secret_key": "sk_output"})}
        keys = provisioner._parse_keys_from_result(result, ["secret_key"])
        assert keys["secret_key"] == "sk_output"

    def test_parse_missing_keys(self, provisioner):
        """Missing keys should return empty dict."""
        result = {"foo": "bar"}
        keys = provisioner._parse_keys_from_result(result, ["secret_key"])
        assert keys == {}

    def test_parse_invalid_json_output(self, provisioner):
        """Non-JSON output should not crash."""
        result = {"output": "not json at all"}
        keys = provisioner._parse_keys_from_result(result, ["secret_key"])
        assert keys == {}


# ── Brand Email Resolution ───────────────────────────────────────────


class TestBrandEmail:
    def test_email_from_identity(self, provisioner, db):
        """Should use identity's email alias generator when available."""
        db.execute(
            "CREATE TABLE IF NOT EXISTS identities ("
            "id INTEGER PRIMARY KEY, identifier TEXT, type TEXT, "
            "metadata TEXT, status TEXT DEFAULT 'active', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        identity = {"from_email": "payments@testbrand.com"}
        email = provisioner._get_brand_email("testbrand", identity)
        # Should call generate_email_alias with domain
        provisioner.identity.generate_email_alias.assert_called_with("testbrand.com")

    def test_email_from_brand_domain(self, provisioner, db):
        """Should use payments@ if brand has a domain."""
        db.execute(
            "CREATE TABLE IF NOT EXISTS identities ("
            "id INTEGER PRIMARY KEY, identifier TEXT, type TEXT, "
            "metadata TEXT, status TEXT DEFAULT 'active', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute_insert(
            "INSERT INTO identities (identifier, type, platform, metadata, status) "
            "VALUES ('mybrand.io', 'domain', 'namecheap', '{\"brand\": \"mybrand\"}', 'active')",
        )
        identity = {}
        email = provisioner._get_brand_email("mybrand", identity)
        assert email == "payments@mybrand.io"

    def test_email_existing_account(self, provisioner, db):
        """Should reuse existing email account for the brand."""
        db.execute(
            "CREATE TABLE IF NOT EXISTS identities ("
            "id INTEGER PRIMARY KEY, identifier TEXT, type TEXT, "
            "metadata TEXT, status TEXT DEFAULT 'active', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute_insert(
            "INSERT INTO identities (identifier, type, platform, metadata, status) "
            "VALUES ('old@mybrand.com', 'email', 'gmail', '{\"brand\": \"mybrand\"}', 'active')",
        )
        email = provisioner._get_brand_email("mybrand", {})
        assert email == "old@mybrand.com"


# ── Provider Dispatch ────────────────────────────────────────────────


class TestDispatch:
    def test_dispatch_stripe(self, provisioner):
        """Should route to provision_stripe."""
        with patch.object(provisioner, "provision_stripe", return_value={"status": "provisioned"}) as mock:
            result = provisioner._dispatch_provision("stripe", "brand1")
            mock.assert_called_once()
            assert result["status"] == "provisioned"

    def test_dispatch_gumroad(self, provisioner):
        with patch.object(provisioner, "provision_gumroad", return_value={"status": "provisioned"}) as mock:
            provisioner._dispatch_provision("gumroad", "brand1")
            mock.assert_called_once()

    def test_dispatch_lemonsqueezy(self, provisioner):
        with patch.object(provisioner, "provision_lemonsqueezy", return_value={"status": "provisioned"}) as mock:
            provisioner._dispatch_provision("lemonsqueezy", "brand1")
            mock.assert_called_once()

    def test_dispatch_btcpay(self, provisioner):
        with patch.object(provisioner, "provision_btcpay", return_value={"status": "provisioned"}) as mock:
            provisioner._dispatch_provision("btcpay", "brand1")
            mock.assert_called_once()

    def test_dispatch_unknown_provider(self, provisioner):
        result = provisioner._dispatch_provision("paypal", "brand1")
        assert result["status"] == "error"
        assert "Unknown provider" in result["error"]


# ── Already Provisioned Check ────────────────────────────────────────


class TestAlreadyProvisioned:
    def test_stripe_already_provisioned(self, provisioner, db):
        """Should short-circuit if keys already exist."""
        with patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            provisioner._store_provider_keys(
                brand="acme", provider="stripe", keys={"secret": "sk_123"},
            )
        result = provisioner.provision_stripe("acme", {"name": "ACME"})
        assert result["status"] == "already_provisioned"

    def test_gumroad_already_provisioned(self, provisioner, db):
        with patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            provisioner._store_provider_keys(
                brand="acme", provider="gumroad", keys={"access_token": "at_123"},
            )
        result = provisioner.provision_gumroad("acme", {"name": "ACME"})
        assert result["status"] == "already_provisioned"

    def test_lemonsqueezy_already_provisioned(self, provisioner, db):
        with patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            provisioner._store_provider_keys(
                brand="acme", provider="lemonsqueezy", keys={"api_key": "lsk_123"},
            )
        result = provisioner.provision_lemonsqueezy("acme", {"name": "ACME"})
        assert result["status"] == "already_provisioned"

    def test_btcpay_already_provisioned(self, provisioner, db):
        provisioner.config.btcpay.server_url = "https://btcpay.example.com"
        provisioner.config.btcpay.api_key = "admin-key"
        with patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            provisioner._store_provider_keys(
                brand="acme", provider="btcpay", keys={"api_key": "bk_123"},
            )
        result = provisioner.provision_btcpay("acme")
        assert result["status"] == "already_provisioned"


# ── BTCPay Provisioning (API-based) ─────────────────────────────────


class TestBTCPayProvisioning:
    def test_btcpay_no_server_url(self, provisioner):
        """Should error without btcpay server URL."""
        provisioner.config.btcpay.server_url = ""
        result = provisioner.provision_btcpay("brand1")
        assert result["status"] == "error"
        assert "server URL" in result["error"]

    def test_btcpay_no_api_key(self, provisioner):
        """Should error without btcpay admin API key."""
        provisioner.config.btcpay.server_url = "https://btcpay.example.com"
        provisioner.config.btcpay.api_key = ""
        result = provisioner.provision_btcpay("brand1")
        assert result["status"] == "error"
        assert "API key" in result["error"]

    def test_btcpay_successful_provision(self, provisioner, db):
        """Full BTCPay provision flow via mocked API."""
        provisioner.config.btcpay.server_url = "https://btcpay.example.com"
        provisioner.config.btcpay.api_key = "admin-key-123"

        mock_conn = MagicMock()
        # Store creation response
        store_response = MagicMock()
        store_response.json.return_value = {"id": "store-abc-123"}
        # API key creation response
        apikey_response = MagicMock()
        apikey_response.json.return_value = {"apiKey": "store-api-key-xyz"}
        # Webhook creation response
        webhook_response = MagicMock()
        webhook_response.json.return_value = {"id": "wh-001"}

        mock_conn.post.side_effect = [store_response, apikey_response, webhook_response]

        with patch("monai.agents.api_provisioner.PlatformConnection", return_value=mock_conn), \
             patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            result = provisioner.provision_btcpay("testbrand")

        assert result["status"] == "provisioned"
        assert result["store_id"] == "store-abc-123"
        # Verify keys stored
        active = provisioner._get_active_keys("testbrand", "btcpay")
        assert len(active) == 1

    def test_btcpay_store_creation_fails(self, provisioner, db):
        """Should error if store creation returns no ID."""
        provisioner.config.btcpay.server_url = "https://btcpay.example.com"
        provisioner.config.btcpay.api_key = "admin-key"

        mock_conn = MagicMock()
        store_response = MagicMock()
        store_response.json.return_value = {"error": "Invalid request"}
        mock_conn.post.return_value = store_response

        with patch("monai.agents.api_provisioner.PlatformConnection", return_value=mock_conn):
            result = provisioner.provision_btcpay("brand1")

        assert result["status"] == "error"
        assert result["phase"] == "store_creation"


# ── Provision All ────────────────────────────────────────────────────


class TestProvisionAll:
    def test_provision_all_orchestrates(self, provisioner):
        """provision_all should call individual provision methods."""
        provisioner._determine_needed_providers = MagicMock(
            return_value=["stripe", "btcpay"]
        )
        with patch.object(provisioner, "provision_stripe",
                         return_value={"status": "provisioned"}) as mock_stripe, \
             patch.object(provisioner, "provision_btcpay",
                         return_value={"status": "provisioned"}) as mock_btcpay:
            result = provisioner.provision_all("brand1", {"name": "Brand1"})

        assert "stripe" in result["provisioned"]
        assert "btcpay" in result["provisioned"]
        assert result["failed"] == []
        mock_stripe.assert_called_once()
        mock_btcpay.assert_called_once()

    def test_provision_all_handles_failures(self, provisioner):
        """Failed provisions should be tracked in results."""
        provisioner._determine_needed_providers = MagicMock(
            return_value=["gumroad"]
        )
        with patch.object(provisioner, "provision_gumroad",
                         side_effect=Exception("Network error")):
            result = provisioner.provision_all("brand1", {"name": "Brand1"})

        assert "gumroad" in result["failed"]
        assert result["provisioned"] == []

    def test_provision_all_skips_already_done(self, provisioner):
        """Already-provisioned providers show in already_existed."""
        provisioner._determine_needed_providers = MagicMock(
            return_value=["lemonsqueezy"]
        )
        with patch.object(provisioner, "provision_lemonsqueezy",
                         return_value={"status": "already_provisioned"}):
            result = provisioner.provision_all("brand1", {"name": "Brand1"})

        assert "lemonsqueezy" in result["already_existed"]
        assert result["provisioned"] == []


# ── Determine Needed Providers ───────────────────────────────────────


class TestDetermineProviders:
    def test_excludes_already_configured(self, provisioner, db):
        """Should not suggest providers with active keys."""
        _seed_brand(db, "acme")
        with patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            provisioner._store_provider_keys(
                brand="acme", provider="stripe", keys={"secret": "sk"},
            )

        # Make think_json return all providers
        provisioner.think_json = MagicMock(return_value={
            "providers": ["stripe", "gumroad", "btcpay"],
        })

        result = provisioner._determine_needed_providers("acme", {})
        assert "stripe" not in result
        assert "gumroad" in result

    def test_fallback_to_default_order(self, provisioner, db):
        """If LLM fails, should return default priority order."""
        provisioner.think_json = MagicMock(side_effect=Exception("LLM down"))
        _seed_brand(db, "acme")

        result = provisioner._determine_needed_providers("acme", {})
        # Default: btcpay, stripe, gumroad, lemonsqueezy
        assert result[0] == "btcpay"
        assert len(result) == 4


# ── Run Cycle ────────────────────────────────────────────────────────


class TestRunCycle:
    def test_run_dispatches_planned_steps(self, provisioner, db):
        """run() should execute each planned step."""
        _seed_brand(db, "brand1")
        # Only 1 provider unprovisioned
        with patch("monai.agents.api_provisioner.encrypt_value", side_effect=lambda v: f"ENC:{v}"):
            for p in ["stripe", "gumroad", "lemonsqueezy"]:
                provisioner._store_provider_keys(
                    brand="brand1", provider=p, keys={"secret": "sk"},
                )

        # Only btcpay-like providers remain (if btcpay not configured, none remain)
        with patch.object(provisioner, "_dispatch_provision",
                         return_value={"status": "provisioned"}) as mock_dispatch:
            result = provisioner.run()

        # Dispatch called for remaining steps
        for step, res in result.items():
            assert res["status"] == "provisioned"

    def test_run_handles_dispatch_error(self, provisioner, db):
        """run() should catch and record dispatch errors."""
        _seed_brand(db, "brand1")
        with patch.object(provisioner, "plan", return_value=["provision_stripe:brand1"]), \
             patch.object(provisioner, "_dispatch_provision",
                         side_effect=Exception("Boom")):
            result = provisioner.run()

        assert result["provision_stripe:brand1"]["status"] == "error"
        assert "Boom" in result["provision_stripe:brand1"]["error"]


# ── Provider Config ──────────────────────────────────────────────────


class TestProviderConfig:
    def test_all_providers_have_required_fields(self):
        """Each provider config should have signup_url, dashboard_url, key_types."""
        for name, cfg in PROVIDER_CONFIG.items():
            assert "signup_url" in cfg, f"{name} missing signup_url"
            assert "dashboard_url" in cfg, f"{name} missing dashboard_url"
            assert "key_types" in cfg, f"{name} missing key_types"
            assert "webhook_route" in cfg, f"{name} missing webhook_route"
            assert len(cfg["key_types"]) >= 1, f"{name} has no key_types"

    def test_stripe_config(self):
        assert PROVIDER_CONFIG["stripe"]["key_types"] == ["publishable", "secret"]

    def test_gumroad_config(self):
        assert PROVIDER_CONFIG["gumroad"]["key_types"] == ["access_token"]

    def test_lemonsqueezy_config(self):
        assert PROVIDER_CONFIG["lemonsqueezy"]["key_types"] == ["api_key"]
