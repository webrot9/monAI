"""Self-provisioning agent — acquires everything monAI needs to operate.

Registers on platforms, creates accounts, gets API keys, registers domains,
sets up email, and acquires any tools/services needed. Fully autonomous.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.agents.asset_aware import AssetManager
from monai.agents.base import BaseAgent
from monai.agents.constraint_planner import ConstraintPlanner
from monai.agents.executor import AutonomousExecutor
from monai.agents.identity import IdentityManager
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)


class Provisioner(BaseAgent):
    """Handles all account creation, API key acquisition, and infrastructure setup."""

    name = "provisioner"
    description = (
        "Self-provisioning agent that registers on platforms, creates accounts, "
        "acquires API keys, registers domains, and sets up any infrastructure "
        "that monAI needs to operate."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.identity = IdentityManager(config, db, llm)
        self.executor = AutonomousExecutor(config, db, llm)
        self.constraint_planner = ConstraintPlanner(db, llm)

    def plan(self) -> list[str]:
        """Determine what needs to be provisioned."""
        identity = self.identity.get_identity()
        accounts = self.identity.get_all_accounts()
        resources_cost = self.identity.get_monthly_resource_costs()

        # Include real asset inventory so the LLM knows what actually exists
        try:
            asset_inventory = AssetManager(self.db).get_inventory().to_context()
        except Exception:
            asset_inventory = ""

        context = (
            f"Identity: {json.dumps(identity, default=str)}\n"
            f"Existing accounts: {json.dumps([{'platform': a['platform'], 'type': a['type']} for a in accounts], default=str)}\n"
            f"Monthly resource costs: ${resources_cost:.2f}\n"
            f"\n{asset_inventory}\n"
        )

        plan = self.think_json(
            "What infrastructure do I need to provision to start making money? "
            "Consider: email account, freelance platform accounts (Upwork, Fiverr, Freelancer), "
            "social media, domain registration, payment processing, marketplace accounts, "
            "and any other tools/services needed. "
            "Only suggest things I don't already have. "
            "Return: {\"steps\": [{\"action\": str, \"platform\": str, \"priority\": int, "
            "\"reason\": str, \"estimated_cost\": float}]}",
            context=context,
        )
        steps = plan.get("steps", [])
        self.log_action("plan", f"Identified {len(steps)} provisioning steps")
        return [s["action"] for s in sorted(steps, key=lambda x: x.get("priority", 99))]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Run provisioning cycle with constraint-aware planning.

        Uses the ConstraintPlanner to build a dependency graph, then executes
        steps in topological order — prerequisites first, dependents after.
        """
        self.log_action("run_start", "Starting provisioning cycle")

        goals = self.plan()
        graph = self.constraint_planner.plan(goals)

        self.log_action("constraint_plan", graph.summary()[:500])

        # Execute in dependency order
        results = graph_results = {}
        max_rounds = len(graph.steps) * 2  # safety cap

        for _ in range(max_rounds):
            ready = graph.get_ready_steps()
            if not ready:
                break

            for step in ready:
                step_result = self._execute_provisioning(step.action)
                results[step.action] = step_result

                if isinstance(step_result, dict) and step_result.get("status") in (
                    "completed", "already_registered", "already_exists", "already_have",
                ):
                    graph.mark_completed(step.id)
                elif isinstance(step_result, dict) and step_result.get("status") in (
                    "failed", "blocked", "skipped",
                ):
                    graph.mark_failed(
                        step.id,
                        step_result.get("reason", step_result.get("status", "unknown")),
                    )
                else:
                    # Assume success if not explicitly failed
                    graph.mark_completed(step.id)

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    async def register_on_platform(self, platform: str) -> dict[str, Any]:
        """Register monAI on a platform (Upwork, Fiverr, etc.)."""
        if self.identity.has_account(platform):
            return {"status": "already_registered", "platform": platform}

        identity = self.identity.get_identity()
        password = self.identity.generate_password()

        task = (
            f"Register a new account on {platform}. "
            f"Use these details:\n"
            f"- Name/Company: {identity.get('name', 'monAI')}\n"
            f"- Username: {identity.get('preferred_username', 'monai')}\n"
            f"- Password: {password}\n"
            f"- Description: {identity.get('description', 'AI-powered digital services')}\n"
            f"Go to the {platform} registration page, fill in the form, and complete signup. "
            f"Take a screenshot after registration for verification."
        )

        result = await self.executor.execute_task(task, json.dumps(identity, default=str))

        if result.get("status") == "completed":
            self.identity.store_account(
                platform=platform,
                identifier=identity.get("preferred_username", "monai"),
                credentials={"password": password},
                metadata={"registration_result": result},
            )
            self.log_action("register", f"Registered on {platform}")

        return result

    async def setup_email(self) -> dict[str, Any]:
        """Set up an email account for the agent."""
        if self.identity.has_account("email"):
            return {"status": "already_exists"}

        identity = self.identity.get_identity()
        task = (
            "Create a free email account for business use. "
            "Options: Gmail, Outlook, ProtonMail. "
            f"Preferred username: {identity.get('preferred_username', 'monai')}\n"
            "Complete the full registration process."
        )

        result = await self.executor.execute_task(task, json.dumps(identity, default=str))
        return result

    async def register_domain(self, domain: str, registrar: str = "namecheap") -> dict[str, Any]:
        """Register a domain name — only after validating availability."""
        # Validate domain before attempting registration
        try:
            from monai.agents.name_validator import NameValidator
            validator = NameValidator(self.config, self.db, self.llm)
            check = validator.check_domain_whois(domain)
            validator.close()

            if check.available is False:
                self.log_action(
                    "register_domain_blocked",
                    f"Domain '{domain}' is already taken: {check.details}",
                )
                # Ask LLM to suggest alternatives
                alternatives = self.think_json(
                    f"The domain '{domain}' is already taken. "
                    "Suggest 5 alternative domain names that are similar but likely available. "
                    "Try variations: different TLDs, prefixes, suffixes. "
                    "Return: {\"alternatives\": [str]}",
                )
                alt_list = alternatives.get("alternatives", [])
                return {
                    "status": "domain_taken",
                    "domain": domain,
                    "details": check.details,
                    "alternatives": alt_list,
                }
        except Exception as e:
            logger.warning(f"Domain validation failed ({e}), proceeding with registration attempt")

        task = (
            f"Register the domain '{domain}' on {registrar}. "
            "Navigate to the registrar, search for the domain, "
            "add to cart, and complete purchase. "
            "Take screenshots of each step."
        )
        identity = self.identity.get_identity()
        result = await self.executor.execute_task(task, json.dumps(identity, default=str))

        if result.get("status") == "completed":
            self.identity.store_domain(domain, registrar)
            self.log_action("register_domain", domain)

        return result

    async def acquire_api_key(self, service: str) -> dict[str, Any]:
        """Get an API key for a service."""
        existing = self.identity.get_api_key(service)
        if existing:
            return {"status": "already_have", "service": service}

        task = (
            f"Get an API key for {service}. "
            "Go to their developer portal, register if needed, "
            "create a new API key, and copy it. "
            "Return the API key in the final result."
        )

        identity = self.identity.get_identity()
        result = await self.executor.execute_task(task, json.dumps(identity, default=str))
        return result

    def _execute_provisioning(self, step: str) -> dict[str, Any]:
        """Execute a provisioning step (sync wrapper for async operations)."""
        # Pre-check: verify assets exist before attempting registration
        missing = AssetManager(self.db).get_missing_prerequisites(step)
        if missing:
            logger.warning(f"Cannot execute '{step}': missing {missing}")
            return {"status": "blocked", "missing_prerequisites": missing}

        if "register" in step.lower() and "platform" in step.lower():
            platform = self.think(
                f"Extract just the platform name from this step: '{step}'. "
                "Reply with just the platform name, lowercase."
            ).strip().lower()
            return self._run_async(self.register_on_platform(platform))
        elif "email" in step.lower():
            return self._run_async(self.setup_email())
        elif "domain" in step.lower():
            # Extract or generate a domain, then validate before registering
            domain_name = self.think(
                f"Extract just the domain name from this step: '{step}'. "
                "Reply with only the domain name (e.g. 'example.com'). "
                "If no specific domain is mentioned, generate a unique, "
                "professional name (e.g. 'nexifydigital.com')."
            ).strip().strip("'\"").lower()

            # Validate and find an available domain
            try:
                from monai.agents.name_validator import NameValidator
                validator = NameValidator(self.config, self.db, self.llm)
                check = validator.check_domain(domain_name)
                if check.available is False:
                    # Domain taken — generate and validate a new one
                    identity, validation = validator.generate_and_validate(
                        domain_tlds=[".com", ".io", ".co", ".dev"],
                    )
                    domain_name = identity.get("validated_domain", domain_name)
                    logger.info(f"Original domain taken, using validated: {domain_name}")
                validator.close()
            except Exception as e:
                logger.warning(f"Domain validation failed ({e}), using original: {domain_name}")

            return self._run_async(self.register_domain(domain_name))
        elif "api" in step.lower():
            return self._run_async(self.acquire_api_key(step))
        else:
            return self._run_async(
                self.executor.execute_task(step, json.dumps(self.identity.get_identity(), default=str))
            )
