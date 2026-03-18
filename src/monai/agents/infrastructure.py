"""Infrastructure management — provisioning, identity, LLC, API keys."""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.agents.api_provisioner import APIProvisioner
from monai.agents.asset_aware import AssetManager
from monai.agents.identity import IdentityManager
from monai.agents.llc_provisioner import LLCProvisioner
from monai.agents.phone_provisioner import PhoneProvisioner
from monai.agents.provisioner import Provisioner
from monai.business.bootstrap import BootstrapWallet
from monai.business.corporate import CorporateManager
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM, BudgetExceededError, get_cost_tracker
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)


class InfrastructureManager:
    """Handles all provisioning: identity, LLC, API keys, payment providers, bootstrap."""

    # Map strategy types to the payment providers they need
    STRATEGY_PAYMENT_PROVIDERS: dict[str, list[str]] = {
        "digital_products": ["gumroad"],
    }

    TOR_BLOCKED_PROVIDERS: set[str] = {"stripe", "gumroad", "lemonsqueezy"}

    def __init__(
        self, config: Config, db: Database, llm: LLM,
        *, identity: IdentityManager, payment_manager: Any,
        ledger: Any, bootstrap_wallet: BootstrapWallet,
        telegram: Any, audit: Any,
    ):
        self.config = config
        self.db = db
        self.llm = llm
        self.identity = identity
        self.provisioner = Provisioner(config, db, llm)
        self.llc_provisioner = LLCProvisioner(config, db, llm)
        self.phone_provisioner = PhoneProvisioner(config, db, llm)
        self.api_provisioner = APIProvisioner(
            config, db, llm, payment_manager=payment_manager,
        )
        self.corporate = CorporateManager(db)
        self.bootstrap_wallet = bootstrap_wallet
        self.payment_manager = payment_manager
        self.telegram = telegram
        self.audit = audit
        self._ensure_llc_setup()

    def _ensure_llc_setup(self) -> None:
        """Auto-provision LLC entity and contractor from config if not in DB."""
        if not self.config.llc.enabled:
            return
        entity = self.corporate.get_primary_entity()
        if not entity:
            entity_id = self.corporate.create_entity(
                name=self.config.llc.entity_name or "Holdings LLC",
                entity_type=self.config.llc.entity_type,
                jurisdiction=self.config.llc.jurisdiction,
            )
            entity = self.corporate.get_entity(entity_id)
        if entity and not self.corporate.get_active_contractor(entity["id"]):
            if self.config.llc.contractor_alias:
                self.corporate.create_contractor(
                    alias=self.config.llc.contractor_alias,
                    entity_id=entity["id"],
                    service_description=self.config.llc.contractor_service,
                    rate_type=self.config.llc.contractor_rate_type,
                    rate_amount=self.config.llc.contractor_rate_amount,
                    rate_percentage=self.config.llc.contractor_rate_percentage,
                    payment_method=self.config.llc.contractor_payment_method,
                )

    def ensure_infrastructure(
        self, cycle: int, *, log_action: Any, learn: Any, notify_creator: Any,
        kofi_manager: Any | None = None,
    ) -> dict[str, Any]:
        """Check and provision any missing infrastructure."""
        identity = self.identity.get_identity()
        inventory = AssetManager(self.db).get_inventory()
        logger.info(f"Asset inventory:\n{inventory.summary()}")

        needs = []
        if not inventory.has_email:
            needs.append("email")
        if not identity:
            needs.append("identity")
        if not self.telegram.has_token:
            needs.append("telegram_bot")

        result: dict[str, Any] = {}

        if needs:
            log_action("provisioning", f"Need to set up: {needs}")
            tracker = get_cost_tracker()
            calls_before = tracker.cycle_calls
            max_provisioning_calls = int(tracker.max_cycle_calls * 0.4)
            saved_limit = tracker.max_cycle_calls
            tracker.max_cycle_calls = calls_before + max_provisioning_calls
            try:
                result = self.provisioner.run(needs=needs)
            except BudgetExceededError:
                logger.warning("Provisioning hit budget cap")
                result = {"status": "budget_capped"}
            finally:
                tracker.max_cycle_calls = saved_limit

        # Bootstrap funding
        bootstrap_phase = self.bootstrap_wallet.get_funding_phase()
        result["bootstrap_phase"] = bootstrap_phase

        if bootstrap_phase == "pre_bootstrap":
            if self.config.privacy.proxy_type != "none":
                log_action("bootstrap", "Ko-fi blocks Tor — skipping campaign setup")
            elif kofi_manager:
                log_action("bootstrap", "Starting Ko-fi campaign.")
                try:
                    kofi_result = kofi_manager.run()
                    if kofi_result.get("status") == "live":
                        notify_creator(
                            f"Ko-fi campaign is live! {kofi_result.get('kofi_url', '')} — "
                            f"Goal: €500. Share it to get funded!"
                        )
                    result["bootstrap"] = kofi_result
                except Exception as e:
                    logger.error(f"Ko-fi campaign setup failed: {e}")
                    result["bootstrap"] = {"status": "error", "error": str(e)}
        else:
            if cycle % 3 == 0 and kofi_manager:
                try:
                    result["kofi_sync"] = kofi_manager.run()
                except Exception as e:
                    logger.error(f"Ko-fi sync failed: {e}")

        # LLC provisioning
        llc_status = self.llc_provisioner.get_provision_status()
        if (self.config.llc.enabled
                and llc_status.get("status") != "not_configured"
                and llc_status.get("progress_pct", 0) < 100):
            log_action("llc_provisioning",
                        f"LLC at {llc_status.get('progress_pct', 0)}%")
            llc_result = self.llc_provisioner.run()
            result["llc"] = llc_result
            if llc_result.get("status") == "completed":
                notify_creator(
                    f"LLC '{self.config.llc.entity_name}' fully provisioned!"
                )
            else:
                completed = sum(
                    1 for s in llc_result.get("steps", {}).values()
                    if s.get("status") == "completed"
                )
                notify_creator(
                    f"LLC provisioning in progress: {completed}/8 steps done."
                )

        # API key provisioning
        api_prov_result = self.run_api_provisioning(force=(cycle <= 1))
        if api_prov_result.get("provisioned"):
            result["api_keys"] = api_prov_result

        # Ensure strategy payment providers
        self._ensure_strategy_payment_providers(result)

        if not needs and "llc" not in result and "api_keys" not in result:
            accounts = self.identity.get_all_accounts()
            return {"status": "infrastructure_ok", "accounts": len(accounts)}

        return {"provisioned": needs, "result": result}

    def run_api_provisioning(self, force: bool = False, cycle: int = 0) -> dict[str, Any]:
        """Run API key provisioning for brands needing payment provider keys."""
        if not force and cycle > 1 and cycle % 5 != 1:
            return {"status": "skipped", "reason": "not_provisioning_cycle"}

        if self.config.privacy.proxy_type != "none":
            return {"status": "skipped", "reason": "payment_providers_block_tor"}

        try:
            plan = self.api_provisioner.plan()
            if not plan:
                return {"status": "ok", "provisioned": []}
            result = self.api_provisioner.run()
            return result
        except Exception as e:
            logger.error(f"API provisioning failed: {e}")
            return {"status": "error", "error": str(e)}

    def _ensure_strategy_payment_providers(self, result: dict[str, Any]) -> None:
        """Proactively provision payment providers for active strategies."""
        try:
            active_strategies = self.db.execute(
                "SELECT DISTINCT name AS strategy FROM strategies WHERE status = 'active'"
            )
            if not active_strategies:
                return

            is_proxied = self.config.privacy.proxy_type != "none"
            provisioned = []
            failed_providers: set[str] = set()

            for row in active_strategies:
                strategy_name = row["strategy"]
                needed_providers = self.STRATEGY_PAYMENT_PROVIDERS.get(strategy_name, [])
                for provider in needed_providers:
                    if is_proxied and provider in self.TOR_BLOCKED_PROVIDERS:
                        continue
                    if provider in failed_providers:
                        continue
                    existing = self.db.execute(
                        "SELECT 1 FROM brand_api_keys WHERE provider = ? AND status = 'active' LIMIT 1",
                        (provider,),
                    )
                    if not existing:
                        try:
                            prov_result = self.api_provisioner._dispatch_provision(
                                provider, strategy_name,
                            )
                            provisioned.append(f"{provider}:{strategy_name}")
                            if prov_result.get("status") in ("error", "failed"):
                                failed_providers.add(provider)
                        except Exception:
                            failed_providers.add(provider)

            if provisioned:
                result["auto_provisioned_providers"] = provisioned
        except Exception as e:
            logger.warning(f"Strategy payment provider check failed: {e}")

    def verify_stored_assets(self, log_action: Any, learn: Any) -> dict[str, Any]:
        """Verify stored assets actually exist before using them."""
        try:
            from monai.agents.email_verifier import EmailVerifier
            verifier = EmailVerifier(self.config, self.db)
            result = self.identity.verify_stored_assets(verifier)
            if result["suspended"]:
                log_action(
                    "asset_verification",
                    f"SUSPENDED {len(result['suspended'])} dead assets"
                )
                learn(
                    "asset_cleanup", "Dead assets found and suspended",
                    f"Suspended: {result['suspended']}.",
                    rule="Always verify assets before using them",
                    severity="warning",
                )
            return result
        except Exception as e:
            logger.warning(f"Asset verification failed: {e}")
            return {"error": str(e)}

    def get_phone_number(self, platform: str, requesting_agent: str) -> dict[str, Any]:
        """Get a virtual phone number for platform signup."""
        return self.phone_provisioner.get_number(platform, requesting_agent)
