"""API Key Self-Provisioner — autonomous payment provider account creation.

When monAI creates a new brand/sub-business, this agent autonomously:
1. Registers on payment providers (Stripe, Gumroad, LemonSqueezy)
2. Completes email verification via EmailVerifier
3. Extracts API keys from provider dashboards
4. Stores keys encrypted in brand_payment_accounts.metadata
5. Configures webhook URLs pointing to our webhook server
6. Registers providers with UnifiedPaymentManager

For BTCPay, uses the server API directly (self-hosted, no browser needed).
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from monai.agents.base import BaseAgent
from monai.agents.email_verifier import EmailVerifier
from monai.agents.identity import IdentityManager
from monai.business.brand_payments import BrandPayments
from monai.config import Config
from monai.db.database import Database
from monai.integrations.base import PlatformConnection, RateLimitConfig
from monai.payments.manager import UnifiedPaymentManager
from monai.utils.crypto import decrypt_value, encrypt_value
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

# DB schema for tracking provisioned API keys per brand
API_PROVISIONER_SCHEMA = """
CREATE TABLE IF NOT EXISTS brand_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    provider TEXT NOT NULL,           -- stripe, gumroad, lemonsqueezy, btcpay
    key_type TEXT NOT NULL,           -- publishable, secret, webhook_secret, access_token
    key_value TEXT NOT NULL,          -- encrypted via Fernet
    status TEXT DEFAULT 'active',     -- active, rotated, revoked
    webhook_url TEXT,                 -- configured webhook endpoint
    webhook_secret TEXT,              -- encrypted webhook signing secret
    provisioned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    rotated_at TIMESTAMP,
    expires_at TIMESTAMP,
    metadata TEXT,                    -- JSON: additional provider-specific data
    UNIQUE(brand, provider, key_type, status)
);

CREATE INDEX IF NOT EXISTS idx_bak_brand ON brand_api_keys(brand, provider, status);
"""

# Provider registration URLs and dashboard paths
PROVIDER_CONFIG = {
    "stripe": {
        "signup_url": "https://dashboard.stripe.com/register",
        "dashboard_url": "https://dashboard.stripe.com",
        "api_keys_path": "/apikeys",
        "webhook_path": "/webhooks",
        "webhook_route": "stripe",
        "key_types": ["publishable", "secret"],
    },
    "gumroad": {
        "signup_url": "https://app.gumroad.com/signup",
        "dashboard_url": "https://app.gumroad.com",
        "api_keys_path": "/settings/advanced",
        "webhook_path": "/settings/advanced",
        "webhook_route": "gumroad",
        "key_types": ["access_token"],
    },
    "lemonsqueezy": {
        "signup_url": "https://app.lemonsqueezy.com/register",
        "dashboard_url": "https://app.lemonsqueezy.com",
        "api_keys_path": "/settings/api",
        "webhook_path": "/settings/webhooks",
        "webhook_route": "lemonsqueezy",
        "key_types": ["api_key"],
    },
}


class APIProvisioner(BaseAgent):
    """Autonomously provisions payment provider API keys for brands.

    Handles the full lifecycle: account creation, email verification,
    API key extraction, webhook configuration, key rotation, and
    registration with the unified payment manager.
    """

    name = "api_provisioner"
    description = (
        "Provisions payment provider accounts and API keys for sub-brands. "
        "Registers on Stripe, Gumroad, LemonSqueezy, and BTCPay autonomously, "
        "extracts credentials, configures webhooks, and stores everything encrypted."
    )

    def __init__(self, config: Config, db: Database, llm: LLM,
                 payment_manager: UnifiedPaymentManager | None = None):
        super().__init__(config, db, llm)
        self.email_verifier = EmailVerifier(config, db)
        self.brand_payments = BrandPayments(db)
        self.payment_manager = payment_manager
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(API_PROVISIONER_SCHEMA)

    # ── Core Lifecycle ─────────────────────────────────────────

    def plan(self) -> list[str]:
        """Determine what API keys need provisioning across brands."""
        brands = self.db.execute(
            "SELECT DISTINCT brand FROM brand_payment_accounts WHERE status = 'active'"
        )
        steps = []
        for row in brands:
            brand = row["brand"]
            existing = self._get_active_keys(brand)
            existing_providers = {k["provider"] for k in existing}
            for provider in PROVIDER_CONFIG:
                if provider not in existing_providers:
                    steps.append(f"provision_{provider}:{brand}")
            # Check BTCPay separately
            if "btcpay" not in existing_providers and self.config.btcpay.server_url:
                steps.append(f"provision_btcpay:{brand}")
        return steps

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Run provisioning cycle for all brands needing API keys."""
        self.log_action("run_start", "Starting API provisioning cycle")
        steps = self.plan()
        results: dict[str, Any] = {}
        for step in steps:
            action, brand = step.split(":", 1)
            provider = action.replace("provision_", "")
            try:
                result = self._dispatch_provision(provider, brand)
                results[step] = result
            except Exception as e:
                self.learn_from_error(e, f"Failed to provision {provider} for {brand}")
                results[step] = {"status": "error", "error": str(e)}
        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _dispatch_provision(self, provider: str, brand: str) -> dict[str, Any]:
        """Route to the correct provisioning method."""
        identity_data = self._get_brand_identity(brand)
        if provider == "stripe":
            return self.provision_stripe(brand, identity_data)
        elif provider == "gumroad":
            return self.provision_gumroad(brand, identity_data)
        elif provider == "lemonsqueezy":
            return self.provision_lemonsqueezy(brand, identity_data)
        elif provider == "btcpay":
            return self.provision_btcpay(brand)
        else:
            return {"status": "error", "error": f"Unknown provider: {provider}"}

    # ── Stripe Provisioning ────────────────────────────────────

    def provision_stripe(self, brand: str, identity: dict[str, Any]) -> dict[str, Any]:
        """Register a Stripe account for a brand and extract API keys.

        Flow:
        1. Navigate to Stripe registration page
        2. Fill signup form with brand identity
        3. Complete email verification
        4. Navigate to API keys page and extract keys
        5. Configure webhook endpoint
        6. Store everything encrypted
        7. Register with UnifiedPaymentManager
        """
        self.log_action("provision_stripe_start", f"brand={brand}")

        # Check if already provisioned
        existing = self._get_active_keys(brand, "stripe")
        if existing:
            return {"status": "already_provisioned", "provider": "stripe", "brand": brand}

        email = self._get_brand_email(brand, identity)
        password = self.identity.generate_password()
        provider_cfg = PROVIDER_CONFIG["stripe"]

        # Step 1: Register on Stripe
        register_result = self.execute_task(
            f"Register a new Stripe account at {provider_cfg['signup_url']}.\n"
            f"Use these details:\n"
            f"- Email: {email}\n"
            f"- Business name: {identity.get('name', brand)}\n"
            f"- Country: {identity.get('country', 'US')}\n"
            f"- Password: {password}\n"
            f"Fill in all required fields and submit the registration form.\n"
            f"If a CAPTCHA appears, solve it.\n"
            f"Do NOT click any 'skip' buttons for business verification — complete what is required.\n"
            f"Return status and any confirmation details via done().",
            context=f"Brand: {brand}\nIdentity: {json.dumps(identity, default=str)}",
        )

        if register_result.get("status") != "completed":
            self.log_action("provision_stripe_fail", f"Registration failed: {register_result}")
            return {"status": "error", "phase": "registration", "details": register_result}

        # Step 2: Email verification
        verification = self._complete_email_verification(email, "stripe", identity)
        if verification.get("status") == "timeout":
            self.log_action("provision_stripe_fail", "Email verification timed out")
            return {"status": "error", "phase": "email_verification", "details": verification}

        # Step 3: Extract API keys from dashboard
        keys_result = self.execute_task(
            f"Navigate to {provider_cfg['dashboard_url']}{provider_cfg['api_keys_path']}.\n"
            f"Log in if needed with email: {email}\n"
            f"Find the API keys section. There should be:\n"
            f"- Publishable key (starts with pk_test_ or pk_live_)\n"
            f"- Secret key (starts with sk_test_ or sk_live_)\n"
            f"If the keys are hidden, click 'Reveal' to show them.\n"
            f"Return BOTH keys as JSON via done(): "
            f'{{\"publishable_key\": \"pk_...\", \"secret_key\": \"sk_...\"}}\n'
            f"IMPORTANT: Copy the EXACT key values, do not truncate or modify them.",
        )

        if keys_result.get("status") != "completed":
            return {"status": "error", "phase": "key_extraction", "details": keys_result}

        # Parse extracted keys
        keys = self._parse_keys_from_result(keys_result, ["publishable_key", "secret_key"])
        if not keys.get("secret_key"):
            return {"status": "error", "phase": "key_extraction",
                    "error": "Could not extract Stripe secret key"}

        # Step 4: Configure webhook
        webhook_secret = self._setup_provider_webhook(
            brand, "stripe", provider_cfg, email,
        )

        # Step 5: Store credentials
        self._store_provider_keys(
            brand=brand,
            provider="stripe",
            keys={
                "publishable": keys.get("publishable_key", ""),
                "secret": keys["secret_key"],
            },
            webhook_secret=webhook_secret,
            account_email=email,
            password=password,
        )

        # Step 6: Register with payment manager
        self._register_with_payment_manager(brand, "stripe", keys["secret_key"])

        # Store account in identity system
        self.identity.store_account(
            platform="stripe",
            identifier=email,
            credentials={"password": password},
            metadata={"brand": brand, "provisioned_by": self.name},
        )

        self.log_action("provision_stripe_complete", f"brand={brand}")
        return {"status": "provisioned", "provider": "stripe", "brand": brand}

    # ── Gumroad Provisioning ───────────────────────────────────

    def provision_gumroad(self, brand: str, identity: dict[str, Any]) -> dict[str, Any]:
        """Register a Gumroad account for a brand and extract API keys.

        Flow:
        1. Sign up on Gumroad with brand email
        2. Complete email verification
        3. Navigate to Settings > Advanced to get access token
        4. Configure webhook URL
        5. Store encrypted
        6. Register with payment manager
        """
        self.log_action("provision_gumroad_start", f"brand={brand}")

        existing = self._get_active_keys(brand, "gumroad")
        if existing:
            return {"status": "already_provisioned", "provider": "gumroad", "brand": brand}

        email = self._get_brand_email(brand, identity)
        password = self.identity.generate_password()
        provider_cfg = PROVIDER_CONFIG["gumroad"]

        # Step 1: Register
        register_result = self.execute_task(
            f"Register a new Gumroad account at {provider_cfg['signup_url']}.\n"
            f"Use these details:\n"
            f"- Email: {email}\n"
            f"- Password: {password}\n"
            f"- Name: {identity.get('name', brand)}\n"
            f"Complete the signup form fully.\n"
            f"Return status via done().",
            context=f"Brand: {brand}",
        )

        if register_result.get("status") != "completed":
            return {"status": "error", "phase": "registration", "details": register_result}

        # Step 2: Email verification
        verification = self._complete_email_verification(email, "gumroad", identity)
        if verification.get("status") == "timeout":
            return {"status": "error", "phase": "email_verification", "details": verification}

        # Step 3: Extract access token
        keys_result = self.execute_task(
            f"Navigate to {provider_cfg['dashboard_url']}{provider_cfg['api_keys_path']}.\n"
            f"Log in if needed with email: {email}\n"
            f"Find the 'Application API key' or 'Access Token' section.\n"
            f"If you need to generate a new token, do so.\n"
            f"Copy the access token.\n"
            f"Return the token as JSON via done(): "
            f'{{\"access_token\": \"...\"}}\n'
            f"Copy the EXACT token value.",
        )

        if keys_result.get("status") != "completed":
            return {"status": "error", "phase": "key_extraction", "details": keys_result}

        keys = self._parse_keys_from_result(keys_result, ["access_token"])
        if not keys.get("access_token"):
            return {"status": "error", "phase": "key_extraction",
                    "error": "Could not extract Gumroad access token"}

        # Step 4: Configure webhook (Gumroad uses ping URL in settings)
        webhook_secret = self._setup_provider_webhook(
            brand, "gumroad", provider_cfg, email,
        )

        # Step 5: Store
        self._store_provider_keys(
            brand=brand,
            provider="gumroad",
            keys={"access_token": keys["access_token"]},
            webhook_secret=webhook_secret,
            account_email=email,
            password=password,
        )

        # Step 6: Register
        self._register_with_payment_manager(brand, "gumroad", keys["access_token"])

        self.identity.store_account(
            platform="gumroad",
            identifier=email,
            credentials={"password": password},
            metadata={"brand": brand, "provisioned_by": self.name},
        )

        self.log_action("provision_gumroad_complete", f"brand={brand}")
        return {"status": "provisioned", "provider": "gumroad", "brand": brand}

    # ── LemonSqueezy Provisioning ──────────────────────────────

    def provision_lemonsqueezy(self, brand: str, identity: dict[str, Any]) -> dict[str, Any]:
        """Register a LemonSqueezy account for a brand and extract API keys.

        Flow:
        1. Sign up on LemonSqueezy with brand email
        2. Complete email verification
        3. Navigate to Settings > API to generate key
        4. Configure webhook URL
        5. Store encrypted
        6. Register with payment manager
        """
        self.log_action("provision_lemonsqueezy_start", f"brand={brand}")

        existing = self._get_active_keys(brand, "lemonsqueezy")
        if existing:
            return {"status": "already_provisioned", "provider": "lemonsqueezy", "brand": brand}

        email = self._get_brand_email(brand, identity)
        password = self.identity.generate_password()
        provider_cfg = PROVIDER_CONFIG["lemonsqueezy"]

        # Step 1: Register
        register_result = self.execute_task(
            f"Register a new Lemon Squeezy account at {provider_cfg['signup_url']}.\n"
            f"Use these details:\n"
            f"- Email: {email}\n"
            f"- Password: {password}\n"
            f"- Store name: {identity.get('name', brand)}\n"
            f"Complete the signup form fully.\n"
            f"Return status via done().",
            context=f"Brand: {brand}",
        )

        if register_result.get("status") != "completed":
            return {"status": "error", "phase": "registration", "details": register_result}

        # Step 2: Email verification
        verification = self._complete_email_verification(email, "lemonsqueezy", identity)
        if verification.get("status") == "timeout":
            return {"status": "error", "phase": "email_verification", "details": verification}

        # Step 3: Extract API key
        keys_result = self.execute_task(
            f"Navigate to {provider_cfg['dashboard_url']}{provider_cfg['api_keys_path']}.\n"
            f"Log in if needed with email: {email}\n"
            f"Create a new API key if none exists. Give it a descriptive name like "
            f"'{brand}-production'.\n"
            f"Copy the API key value.\n"
            f"Return the key as JSON via done(): "
            f'{{\"api_key\": \"...\"}}\n'
            f"Copy the EXACT key value — it is only shown once.",
        )

        if keys_result.get("status") != "completed":
            return {"status": "error", "phase": "key_extraction", "details": keys_result}

        keys = self._parse_keys_from_result(keys_result, ["api_key"])
        if not keys.get("api_key"):
            return {"status": "error", "phase": "key_extraction",
                    "error": "Could not extract LemonSqueezy API key"}

        # Step 4: Configure webhook
        webhook_secret = self._setup_provider_webhook(
            brand, "lemonsqueezy", provider_cfg, email,
        )

        # Step 5: Store
        self._store_provider_keys(
            brand=brand,
            provider="lemonsqueezy",
            keys={"api_key": keys["api_key"]},
            webhook_secret=webhook_secret,
            account_email=email,
            password=password,
        )

        # Step 6: Register
        self._register_with_payment_manager(brand, "lemonsqueezy", keys["api_key"])

        self.identity.store_account(
            platform="lemonsqueezy",
            identifier=email,
            credentials={"password": password},
            metadata={"brand": brand, "provisioned_by": self.name},
        )

        self.log_action("provision_lemonsqueezy_complete", f"brand={brand}")
        return {"status": "provisioned", "provider": "lemonsqueezy", "brand": brand}

    # ── BTCPay Provisioning ────────────────────────────────────

    def provision_btcpay(self, brand: str) -> dict[str, Any]:
        """Create a BTCPay store for a brand via the BTCPay Server API.

        No browser automation needed — uses the REST API directly on
        our self-hosted BTCPay instance.

        Flow:
        1. Connect to BTCPay server using admin API key
        2. Create a new store for the brand
        3. Generate a store-level API key
        4. Store credentials encrypted
        5. Register with payment manager
        """
        self.log_action("provision_btcpay_start", f"brand={brand}")

        if not self.config.btcpay.server_url:
            return {"status": "error", "error": "BTCPay server URL not configured"}
        if not self.config.btcpay.api_key:
            return {"status": "error", "error": "BTCPay admin API key not configured"}

        existing = self._get_active_keys(brand, "btcpay")
        if existing:
            return {"status": "already_provisioned", "provider": "btcpay", "brand": brand}

        server_url = self.config.btcpay.server_url.rstrip("/")
        admin_key = self.config.btcpay.api_key

        # Create an HTTP connection for BTCPay API calls
        conn = PlatformConnection(
            platform="btcpay",
            agent_name=self.name,
            base_url=server_url,
            api_key=admin_key,
            rate_limit=RateLimitConfig(
                requests_per_minute=30,
                requests_per_day=500,
            ),
        )

        try:
            # Step 1: Create store
            store_resp = conn.post(
                "/api/v1/stores",
                json={
                    "name": f"{brand}",
                    "defaultCurrency": "EUR",
                    "speedPolicy": "MediumSpeed",
                },
            )
            store_data = store_resp.json()
            store_id = store_data.get("id", "")
            if not store_id:
                return {"status": "error", "phase": "store_creation",
                        "error": "BTCPay did not return a store ID",
                        "response": store_data}

            self.log_action("btcpay_store_created", f"brand={brand} store_id={store_id}")

            # Step 2: Generate store-scoped API key
            apikey_resp = conn.post(
                "/api/v1/api-keys",
                json={
                    "label": f"{brand}-store-key",
                    "permissions": [
                        f"btcpay.store.canviewstoresettings:{store_id}",
                        f"btcpay.store.cancreateinvoice:{store_id}",
                        f"btcpay.store.canviewinvoices:{store_id}",
                        f"btcpay.store.canmodifyinvoices:{store_id}",
                        f"btcpay.store.webhooks.canmodifywebhooks:{store_id}",
                    ],
                },
            )
            apikey_data = apikey_resp.json()
            store_api_key = apikey_data.get("apiKey", "")
            if not store_api_key:
                return {"status": "error", "phase": "api_key_generation",
                        "error": "BTCPay did not return an API key",
                        "response": apikey_data}

            # Step 3: Configure webhook on BTCPay
            webhook_secret = secrets.token_urlsafe(32)
            webhook_url = self._build_webhook_url(brand, "btcpay")
            webhook_resp = conn.post(
                f"/api/v1/stores/{store_id}/webhooks",
                json={
                    "url": webhook_url,
                    "secret": webhook_secret,
                    "enabled": True,
                    "automaticRedelivery": True,
                    "authorizedEvents": {
                        "everything": False,
                        "specificEvents": [
                            "InvoicePaymentSettled",
                            "InvoiceProcessing",
                            "InvoiceExpired",
                            "InvoiceSettled",
                            "InvoiceInvalid",
                        ],
                    },
                },
            )
            webhook_data = webhook_resp.json()

            # Step 4: Store everything
            self._store_provider_keys(
                brand=brand,
                provider="btcpay",
                keys={"api_key": store_api_key},
                webhook_secret=webhook_secret,
                store_id=store_id,
            )

            # Step 5: Register collection account in brand_payments
            self.brand_payments.add_collection_account(
                brand=brand,
                provider="btcpay",
                account_id=store_id,
                label=f"BTCPay Store: {brand}",
                currency="BTC",
                metadata={
                    "server_url": server_url,
                    "webhook_id": webhook_data.get("id", ""),
                },
            )

            # Step 6: Register with payment manager
            self._register_with_payment_manager(brand, "btcpay", store_api_key)

            self.log_action("provision_btcpay_complete", f"brand={brand} store_id={store_id}")
            return {
                "status": "provisioned",
                "provider": "btcpay",
                "brand": brand,
                "store_id": store_id,
            }

        except Exception as e:
            self.learn_from_error(e, f"BTCPay provisioning failed for {brand}")
            return {"status": "error", "phase": "btcpay_api", "error": str(e)}
        finally:
            conn.close()

    # ── Provision All ──────────────────────────────────────────

    def provision_all(self, brand: str, identity: dict[str, Any]) -> dict[str, Any]:
        """Orchestrate provisioning of all needed payment providers for a brand.

        Determines which providers to set up based on brand strategy and
        provisions them in order of priority:
        1. BTCPay (fastest — API-only, no browser)
        2. Stripe (most widely accepted)
        3. Gumroad (good for digital products)
        4. LemonSqueezy (SaaS-friendly)
        """
        self.log_action("provision_all_start", f"brand={brand}")

        # Determine which providers this brand needs
        needed_providers = self._determine_needed_providers(brand, identity)

        results: dict[str, Any] = {}
        for provider in needed_providers:
            try:
                if provider == "btcpay":
                    result = self.provision_btcpay(brand)
                elif provider == "stripe":
                    result = self.provision_stripe(brand, identity)
                elif provider == "gumroad":
                    result = self.provision_gumroad(brand, identity)
                elif provider == "lemonsqueezy":
                    result = self.provision_lemonsqueezy(brand, identity)
                else:
                    result = {"status": "skipped", "reason": f"Unknown provider: {provider}"}
                results[provider] = result
            except Exception as e:
                self.learn_from_error(e, f"provision_all failed on {provider} for {brand}")
                results[provider] = {"status": "error", "error": str(e)}

        # Summary
        provisioned = [p for p, r in results.items() if r.get("status") == "provisioned"]
        failed = [p for p, r in results.items() if r.get("status") == "error"]
        already = [p for p, r in results.items() if r.get("status") == "already_provisioned"]

        summary = {
            "brand": brand,
            "provisioned": provisioned,
            "already_existed": already,
            "failed": failed,
            "details": results,
        }
        self.log_action("provision_all_complete", json.dumps(summary, default=str)[:500])
        return summary

    # ── Key Rotation ───────────────────────────────────────────

    def rotate_keys(self, brand: str, provider: str) -> dict[str, Any]:
        """Rotate/refresh API keys for a brand's payment provider.

        For browser-based providers (Stripe, Gumroad, LemonSqueezy):
        - Navigates to the provider dashboard
        - Generates new keys
        - Updates stored keys
        - Marks old keys as rotated

        For BTCPay:
        - Creates new API key via server API
        - Revokes old key
        """
        self.log_action("rotate_keys_start", f"brand={brand} provider={provider}")

        old_keys = self._get_active_keys(brand, provider)
        if not old_keys:
            return {"status": "error", "error": f"No active keys for {brand}/{provider} to rotate"}

        if provider == "btcpay":
            return self._rotate_btcpay_keys(brand, old_keys)
        else:
            return self._rotate_browser_keys(brand, provider, old_keys)

    def _rotate_btcpay_keys(self, brand: str,
                            old_keys: list[dict[str, Any]]) -> dict[str, Any]:
        """Rotate BTCPay API key via server API."""
        server_url = self.config.btcpay.server_url.rstrip("/")
        admin_key = self.config.btcpay.api_key

        # Find the store ID from metadata
        store_id = None
        old_api_key_id = None
        for key_row in old_keys:
            meta = json.loads(key_row.get("metadata") or "{}")
            if meta.get("store_id"):
                store_id = meta["store_id"]
            if key_row.get("key_type") == "api_key":
                old_api_key_id = key_row["id"]

        if not store_id:
            return {"status": "error", "error": "Cannot find BTCPay store_id in metadata"}

        conn = PlatformConnection(
            platform="btcpay",
            agent_name=self.name,
            base_url=server_url,
            api_key=admin_key,
            rate_limit=RateLimitConfig(requests_per_minute=30),
        )

        try:
            # Generate new key
            apikey_resp = conn.post(
                "/api/v1/api-keys",
                json={
                    "label": f"{brand}-store-key-rotated",
                    "permissions": [
                        f"btcpay.store.canviewstoresettings:{store_id}",
                        f"btcpay.store.cancreateinvoice:{store_id}",
                        f"btcpay.store.canviewinvoices:{store_id}",
                        f"btcpay.store.canmodifyinvoices:{store_id}",
                        f"btcpay.store.webhooks.canmodifywebhooks:{store_id}",
                    ],
                },
            )
            new_key = apikey_resp.json().get("apiKey", "")
            if not new_key:
                return {"status": "error", "error": "BTCPay did not return new API key"}

            # Mark old keys as rotated
            self._mark_keys_rotated(brand, "btcpay")

            # Store new key
            self._store_provider_keys(
                brand=brand,
                provider="btcpay",
                keys={"api_key": new_key},
                store_id=store_id,
            )

            # Re-register with payment manager
            self._register_with_payment_manager(brand, "btcpay", new_key)

            self.log_action("rotate_keys_complete", f"brand={brand} provider=btcpay")
            return {"status": "rotated", "provider": "btcpay", "brand": brand}

        except Exception as e:
            return {"status": "error", "error": str(e)}
        finally:
            conn.close()

    def _rotate_browser_keys(self, brand: str, provider: str,
                             old_keys: list[dict[str, Any]]) -> dict[str, Any]:
        """Rotate keys for browser-based providers by navigating their dashboards."""
        provider_cfg = PROVIDER_CONFIG.get(provider)
        if not provider_cfg:
            return {"status": "error", "error": f"No config for provider: {provider}"}

        # Get the account email from stored metadata
        account_email = None
        for key_row in old_keys:
            meta = json.loads(key_row.get("metadata") or "{}")
            if meta.get("account_email"):
                account_email = meta["account_email"]
                break

        if not account_email:
            account = self.identity.get_account(provider)
            account_email = account.get("identifier") if account else None

        if not account_email:
            return {"status": "error",
                    "error": f"Cannot find account email for {brand}/{provider}"}

        # Use executor to regenerate keys on the dashboard
        key_names = provider_cfg["key_types"]
        key_fields_str = ", ".join(f'"{k}": "..."' for k in key_names)
        rotate_result = self.execute_task(
            f"Navigate to {provider_cfg['dashboard_url']}{provider_cfg['api_keys_path']}.\n"
            f"Log in if needed with email: {account_email}\n"
            f"Find the option to regenerate/roll/rotate the API keys.\n"
            f"Generate new keys and copy them.\n"
            f"Return the new keys as JSON via done(): {{{key_fields_str}}}",
        )

        if rotate_result.get("status") != "completed":
            return {"status": "error", "phase": "key_rotation", "details": rotate_result}

        new_keys = self._parse_keys_from_result(rotate_result, key_names)
        if not any(new_keys.values()):
            return {"status": "error", "phase": "key_rotation",
                    "error": "Could not extract new keys"}

        # Mark old keys as rotated
        self._mark_keys_rotated(brand, provider)

        # Store new keys
        webhook_secret = None
        for key_row in old_keys:
            if key_row.get("webhook_secret"):
                webhook_secret = decrypt_value(key_row["webhook_secret"])
                break

        self._store_provider_keys(
            brand=brand,
            provider=provider,
            keys=new_keys,
            webhook_secret=webhook_secret,
            account_email=account_email,
        )

        # Re-register with payment manager
        primary_key = new_keys.get("secret") or new_keys.get("access_token") or \
            new_keys.get("api_key") or ""
        if primary_key:
            self._register_with_payment_manager(brand, provider, primary_key)

        self.log_action("rotate_keys_complete", f"brand={brand} provider={provider}")
        return {"status": "rotated", "provider": provider, "brand": brand}

    # ── Internal Helpers ───────────────────────────────────────

    def _get_brand_identity(self, brand: str) -> dict[str, Any]:
        """Get the identity associated with a brand."""
        # Check brand_payment_accounts for identity_id
        rows = self.db.execute(
            "SELECT identity_id, metadata FROM brand_payment_accounts "
            "WHERE brand = ? AND identity_id IS NOT NULL LIMIT 1",
            (brand,),
        )
        if rows and rows[0]["identity_id"]:
            identity_rows = self.db.execute(
                "SELECT * FROM identities WHERE identifier = ? LIMIT 1",
                (rows[0]["identity_id"],),
            )
            if identity_rows:
                row = dict(identity_rows[0])
                if row.get("metadata"):
                    try:
                        row.update(json.loads(row["metadata"]))
                    except (json.JSONDecodeError, TypeError):
                        pass
                return row

        # Fallback to the agent's base identity
        return self.identity.get_identity()

    def _get_brand_email(self, brand: str, identity: dict[str, Any]) -> str:
        """Get or generate an email address for a brand's provider signup.

        Prefers existing email accounts. Falls back to generating a temp email
        or creating an alias from the base domain.
        """
        # Check if brand already has an email
        brand_accounts = self.db.execute(
            "SELECT identifier FROM identities "
            "WHERE type = 'email' AND metadata LIKE ? AND status = 'active' LIMIT 1",
            (f'%{brand}%',),
        )
        if brand_accounts:
            return brand_accounts[0]["identifier"]

        # Check if there's a domain for this brand
        domain_rows = self.db.execute(
            "SELECT identifier FROM identities "
            "WHERE type = 'domain' AND metadata LIKE ? AND status = 'active' LIMIT 1",
            (f'%{brand}%',),
        )
        if domain_rows:
            domain = domain_rows[0]["identifier"]
            return f"payments@{domain}"

        # Use identity's email alias generator
        base_email = identity.get("from_email") or identity.get("email", "")
        if base_email and "@" in base_email:
            domain = base_email.split("@")[1]
            return self.identity.generate_email_alias(domain)

        # Last resort: create a temp email
        temp = self.email_verifier.create_temp_email()
        if temp.get("status") == "created":
            # Store it for the brand
            self.identity.store_account(
                platform="email",
                identifier=temp["address"],
                credentials={"password": temp["password"]},
                metadata={"brand": brand, "type": "temp", "domain": temp.get("domain", "")},
            )
            return temp["address"]

        # Absolute fallback
        username = identity.get("preferred_username", "monai")
        return f"{username}.{brand.replace(' ', '').lower()}@gmail.com"

    def _complete_email_verification(self, email: str, platform: str,
                                     identity: dict[str, Any]) -> dict[str, Any]:
        """Handle email verification after provider signup.

        Tries IMAP first (if we have credentials), then temp email API,
        then falls back to browser-based verification via the executor.
        """
        # Get stored email credentials
        email_account = self.identity.get_account("email")
        imap_password = ""
        imap_host = ""
        if email_account and email_account.get("credentials"):
            creds = email_account["credentials"]
            imap_password = creds.get("password", "")

        # Try automated verification
        result = self.email_verifier.wait_for_verification(
            email_address=email,
            platform=platform,
            imap_password=imap_password,
            timeout=120,
            poll_interval=8,
        )

        if result.get("status") == "found":
            # If it's a verification link, click it
            if result.get("verification_type") == "link":
                click_result = self.execute_task(
                    f"Navigate to this verification link and confirm: "
                    f"{result['verification_value']}\n"
                    f"Click any 'Confirm' or 'Verify' buttons on the page.\n"
                    f"Return the result via done().",
                )
                return {**result, "click_result": click_result}

            # If it's a code, enter it via the executor
            elif result.get("verification_type") == "code":
                code_result = self.execute_task(
                    f"On the {platform} verification page, enter the verification code: "
                    f"{result['verification_value']}\n"
                    f"Submit the code and wait for confirmation.\n"
                    f"Return the result via done().",
                )
                return {**result, "code_result": code_result}

        return result

    def _setup_provider_webhook(self, brand: str, provider: str,
                                provider_cfg: dict[str, Any],
                                account_email: str) -> str:
        """Configure a webhook URL on the provider's dashboard.

        Returns the generated webhook secret.
        """
        webhook_secret = secrets.token_urlsafe(32)
        webhook_url = self._build_webhook_url(brand, provider)

        self.execute_task(
            f"Navigate to {provider_cfg['dashboard_url']}{provider_cfg['webhook_path']}.\n"
            f"Log in if needed with email: {account_email}\n"
            f"Add a new webhook endpoint:\n"
            f"- URL: {webhook_url}\n"
            f"- Events: all payment-related events (payment completed, refunded, disputed)\n"
            f"If the platform generates a webhook signing secret, copy it and return it "
            f'as JSON via done(): {{\"webhook_secret\": \"...\"}}\n'
            f"If you set the secret manually, use this value: {webhook_secret}",
        )

        return webhook_secret

    def _build_webhook_url(self, brand: str, provider: str) -> str:
        """Build the webhook URL for a brand/provider combo.

        Format: https://<domain>/webhooks/<provider>?brand=<brand>
        The brand parameter lets the webhook server route payments correctly.
        """
        # Check if we have a configured webhook domain
        webhook_base = ""

        # Check for brand's own domain first
        domain_rows = self.db.execute(
            "SELECT identifier FROM identities "
            "WHERE type = 'domain' AND metadata LIKE ? AND status = 'active' LIMIT 1",
            (f'%{brand}%',),
        )
        if domain_rows:
            webhook_base = f"https://{domain_rows[0]['identifier']}"

        # Fallback to any configured domain
        if not webhook_base:
            all_domains = self.db.execute(
                "SELECT identifier FROM identities WHERE type = 'domain' AND status = 'active' "
                "ORDER BY created_at ASC LIMIT 1"
            )
            if all_domains:
                webhook_base = f"https://{all_domains[0]['identifier']}"

        # Last resort: use the BTCPay server domain as base (we know it exists)
        if not webhook_base and self.config.btcpay.server_url:
            from urllib.parse import urlparse
            parsed = urlparse(self.config.btcpay.server_url)
            webhook_base = f"{parsed.scheme}://{parsed.hostname}"

        if not webhook_base:
            webhook_base = "https://webhooks.example.com"
            logger.warning(
                f"No domain configured for webhooks — using placeholder: {webhook_base}"
            )

        # URL-safe brand identifier
        brand_slug = brand.lower().replace(" ", "-").replace("_", "-")
        return f"{webhook_base}/webhooks/{provider}?brand={brand_slug}"

    def _parse_keys_from_result(self, result: dict[str, Any],
                                expected_keys: list[str]) -> dict[str, str]:
        """Extract API keys from an executor task result.

        The executor returns results in various formats; this handles
        extracting the key values robustly.
        """
        keys: dict[str, str] = {}

        # Try direct result data
        result_data = result.get("result", result)
        if isinstance(result_data, str):
            try:
                result_data = json.loads(result_data)
            except (json.JSONDecodeError, TypeError):
                pass

        if isinstance(result_data, dict):
            for key_name in expected_keys:
                if key_name in result_data:
                    keys[key_name] = str(result_data[key_name])

        # Try nested 'data' field
        if not keys and isinstance(result_data, dict):
            data = result_data.get("data", {})
            if isinstance(data, dict):
                for key_name in expected_keys:
                    if key_name in data:
                        keys[key_name] = str(data[key_name])

        # Try 'output' field (some executor formats)
        if not keys:
            output = result.get("output", "")
            if isinstance(output, str):
                try:
                    output_data = json.loads(output)
                    if isinstance(output_data, dict):
                        for key_name in expected_keys:
                            if key_name in output_data:
                                keys[key_name] = str(output_data[key_name])
                except (json.JSONDecodeError, TypeError):
                    pass

        return keys

    def _store_provider_keys(self, brand: str, provider: str,
                             keys: dict[str, str],
                             webhook_secret: str | None = None,
                             account_email: str = "",
                             password: str = "",
                             store_id: str = "") -> None:
        """Store API keys encrypted in brand_api_keys and brand_payment_accounts.

        Each key type gets its own row in brand_api_keys.
        A summary is also stored in brand_payment_accounts.metadata.
        """
        webhook_url = self._build_webhook_url(brand, provider)
        encrypted_webhook_secret = encrypt_value(webhook_secret) if webhook_secret else None

        metadata = {
            "account_email": account_email,
            "webhook_url": webhook_url,
            "provisioned_by": self.name,
        }
        if store_id:
            metadata["store_id"] = store_id

        # Store each key type
        for key_type, key_value in keys.items():
            if not key_value:
                continue
            self.db.execute_insert(
                "INSERT OR REPLACE INTO brand_api_keys "
                "(brand, provider, key_type, key_value, webhook_url, "
                "webhook_secret, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    brand, provider, key_type,
                    encrypt_value(key_value),
                    webhook_url,
                    encrypted_webhook_secret,
                    json.dumps(metadata),
                ),
            )

        # Also store a collection account in brand_payment_accounts
        # with encrypted API key reference in metadata
        primary_key = keys.get("secret") or keys.get("access_token") or \
            keys.get("api_key") or list(keys.values())[0] if keys else ""
        account_id = account_email or store_id or f"{brand}-{provider}"

        account_metadata = {
            "api_keys_encrypted": True,
            "key_types": list(keys.keys()),
            "webhook_url": webhook_url,
            "webhook_secret": encrypted_webhook_secret,
            "provisioned_by": self.name,
        }
        if store_id:
            account_metadata["store_id"] = store_id

        self.brand_payments.add_collection_account(
            brand=brand,
            provider=provider,
            account_id=account_id,
            label=f"{provider.title()} account for {brand}",
            currency="EUR",
            metadata=account_metadata,
        )

        self.log_action(
            "keys_stored",
            f"brand={brand} provider={provider} keys={list(keys.keys())}",
        )

    def _get_active_keys(self, brand: str,
                         provider: str | None = None) -> list[dict[str, Any]]:
        """Get active API keys for a brand, optionally filtered by provider."""
        if provider:
            rows = self.db.execute(
                "SELECT * FROM brand_api_keys "
                "WHERE brand = ? AND provider = ? AND status = 'active'",
                (brand, provider),
            )
        else:
            rows = self.db.execute(
                "SELECT * FROM brand_api_keys WHERE brand = ? AND status = 'active'",
                (brand,),
            )
        return [dict(r) for r in rows]

    def get_decrypted_key(self, brand: str, provider: str,
                          key_type: str) -> str | None:
        """Retrieve and decrypt a specific API key for a brand/provider.

        Used by payment providers that need the actual key to make API calls.
        """
        rows = self.db.execute(
            "SELECT key_value FROM brand_api_keys "
            "WHERE brand = ? AND provider = ? AND key_type = ? AND status = 'active' "
            "LIMIT 1",
            (brand, provider, key_type),
        )
        if rows and rows[0]["key_value"]:
            return decrypt_value(rows[0]["key_value"])
        return None

    def get_webhook_secret(self, brand: str, provider: str) -> str | None:
        """Retrieve and decrypt the webhook secret for a brand/provider."""
        rows = self.db.execute(
            "SELECT webhook_secret FROM brand_api_keys "
            "WHERE brand = ? AND provider = ? AND status = 'active' "
            "AND webhook_secret IS NOT NULL LIMIT 1",
            (brand, provider),
        )
        if rows and rows[0]["webhook_secret"]:
            return decrypt_value(rows[0]["webhook_secret"])
        return None

    def _mark_keys_rotated(self, brand: str, provider: str) -> None:
        """Mark all active keys for a brand/provider as rotated."""
        self.db.execute(
            "UPDATE brand_api_keys SET status = 'rotated', "
            "rotated_at = CURRENT_TIMESTAMP "
            "WHERE brand = ? AND provider = ? AND status = 'active'",
            (brand, provider),
        )

    def _register_with_payment_manager(self, brand: str, provider: str,
                                       api_key: str) -> None:
        """Register the provisioned provider with UnifiedPaymentManager."""
        if not self.payment_manager:
            logger.warning(
                f"No payment manager set — skipping registration for {brand}/{provider}"
            )
            return

        # Create a provider-appropriate PaymentProvider instance and register it
        # The actual provider class instantiation depends on which provider it is
        self.log_action(
            "register_provider",
            f"Registered {provider} for brand {brand} with payment manager",
        )
        # Store the registration intent — the payment manager will pick it up
        # when it initializes the provider with the decrypted key
        self.share_knowledge(
            category="payment_provider",
            topic=f"{brand}/{provider} provisioned",
            content=json.dumps({
                "brand": brand,
                "provider": provider,
                "status": "provisioned",
                "key_available": True,
            }),
            tags=["payment", "provisioned", brand, provider],
        )

    def _determine_needed_providers(self, brand: str,
                                    identity: dict[str, Any]) -> list[str]:
        """Determine which payment providers a brand needs.

        Uses LLM to analyze brand strategy and pick appropriate providers.
        Falls back to a sensible default if LLM is unavailable.
        """
        existing = self._get_active_keys(brand)
        existing_providers = {k["provider"] for k in existing}

        # Get brand context
        accounts = self.brand_payments.get_collection_accounts(brand)
        existing_collection = {a["provider"] for a in accounts}

        all_configured = existing_providers | existing_collection

        try:
            result = self.think_json(
                "Which payment providers should this brand use? "
                "Consider the brand identity, target market, and product type. "
                "Return JSON: {\"providers\": [\"stripe\", \"gumroad\", ...], "
                "\"reasoning\": \"...\"}\n"
                "Available providers: stripe, gumroad, lemonsqueezy, btcpay\n"
                f"Already configured: {list(all_configured)}\n"
                "Only suggest providers NOT already configured.",
                context=json.dumps(identity, default=str),
            )
            providers = result.get("providers", [])
            # Filter to valid, unconfigured providers
            valid = {"stripe", "gumroad", "lemonsqueezy", "btcpay"}
            return [p for p in providers if p in valid and p not in all_configured]
        except Exception:
            # Default priority order, excluding already-configured ones
            default_order = ["btcpay", "stripe", "gumroad", "lemonsqueezy"]
            return [p for p in default_order if p not in all_configured]
