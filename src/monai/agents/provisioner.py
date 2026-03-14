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
from monai.agents.email_verifier import EmailVerifier
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
        self.email_verifier = EmailVerifier(config, db)

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

        # Ensure we have an email before attempting registration
        email_account = self.identity.get_account("email")
        if not email_account:
            return {"status": "blocked", "reason": "No email account available"}
        email = email_account["identifier"]

        identity = self.identity.get_identity()
        password = self.identity.generate_password()

        task = (
            f"Register a new account on {platform}. "
            f"Use these details:\n"
            f"- Email: {email}\n"
            f"- Name/Company: {identity.get('name', 'monAI')}\n"
            f"- Username: {identity.get('preferred_username', 'monai')}\n"
            f"- Password: {password}\n"
            f"- Description: {identity.get('description', 'AI-powered digital services')}\n"
            f"Go to the {platform} registration page, fill in the form, and complete signup. "
            f"IMPORTANT: Use ONLY the credentials above. Do NOT invent or fabricate any "
            f"email addresses, passwords, or other credentials.\n"
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
        """Set up a real email account via Mailslurp API.

        Fully autonomous:
        1. If no Mailslurp API key → self-provision one via browser
           (sign up on mailslurp.com using a disposable mail.tm email)
        2. Create a persistent inbox via Mailslurp API
        3. Store credentials for all future operations

        No manual config needed. The only key the creator provides is OpenAI.
        """
        if self.identity.has_account("email"):
            return {"status": "already_exists"}

        # Step 1: Self-provision Mailslurp API key if missing
        if not self.config.comms.mailslurp_api_key:
            logger.info("No Mailslurp API key — self-provisioning via browser")
            provision_result = await self._provision_mailslurp_key()
            if provision_result.get("status") != "completed":
                return {
                    "status": "failed",
                    "reason": f"Mailslurp self-provisioning failed: "
                              f"{provision_result.get('error', 'unknown')}",
                }

        # Step 2: Create inbox via API
        identity = self.identity.get_identity()
        inbox_name = identity.get("name", "monAI")

        result = self.email_verifier.create_mailslurp_inbox(name=inbox_name)
        if result.get("status") != "created":
            logger.error(f"Mailslurp inbox creation failed: {result}")
            return {
                "status": "failed",
                "reason": result.get("error", "Mailslurp API error"),
            }

        self.identity.store_account(
            platform="email",
            identifier=result["address"],
            credentials={"inbox_id": result["inbox_id"]},
            metadata={
                "type": "mailslurp",
                "inbox_id": result["inbox_id"],
                "provider": "mailslurp",
            },
        )
        logger.info(f"Email provisioned via Mailslurp: {result['address']}")
        return {"status": "completed", "email": result["address"]}

    async def _provision_mailslurp_key(self) -> dict[str, Any]:
        """Self-provision a Mailslurp API key autonomously.

        Uses a disposable mail.tm email ONLY for the Mailslurp signup.
        After getting the API key, the temp email can expire — doesn't matter.
        """
        # Create throwaway email for Mailslurp signup
        temp = self.email_verifier.create_temp_email()
        if temp.get("status") != "created":
            return {"status": "failed",
                    "error": f"Cannot create bootstrap email: {temp.get('error')}"}

        temp_email = temp["address"]
        temp_password = temp["password"]
        logger.info(f"Bootstrap temp email created: {temp_email}")

        # Sign up on Mailslurp via browser automation
        import secrets
        ms_password = secrets.token_urlsafe(20)

        signup_task = (
            f"Go to https://app.mailslurp.com/sign-up/ and create a free account.\n"
            f"Use these credentials:\n"
            f"- Email: {temp_email}\n"
            f"- Password: {ms_password}\n"
            f"Complete the signup form and submit it.\n"
            f"IMPORTANT: Use ONLY the credentials above. Do NOT invent any.\n"
            f"After signup, you may need to verify the email. "
            f"Report whether signup succeeded via done()."
        )
        signup_result = await self.executor.execute_task(signup_task)
        if signup_result.get("status") != "completed":
            return {"status": "failed",
                    "error": f"Mailslurp signup failed: {signup_result}"}

        # Check temp email for verification (if needed)
        verification = self.email_verifier.wait_for_verification(
            email_address=temp_email,
            platform="mailslurp",
            imap_password=temp_password,
            timeout=60,
            poll_interval=5,
        )
        if verification.get("status") == "found":
            link = verification.get("verification_value", "")
            if link and verification.get("verification_type") == "link":
                await self.executor.execute_task(
                    f"Navigate to this verification link and confirm: {link}\n"
                    f"Click any 'Confirm' or 'Verify' buttons on the page.\n"
                    f"Return the result via done()."
                )

        # Extract API key from Mailslurp dashboard
        extract_task = (
            f"Go to https://app.mailslurp.com/\n"
            f"If not logged in, log in with:\n"
            f"- Email: {temp_email}\n"
            f"- Password: {ms_password}\n"
            f"Navigate to the API key section (usually in account settings or "
            f"displayed on the dashboard).\n"
            f"Copy the API key and return it via done() in this exact format:\n"
            f"API_KEY: <the key here>\n"
            f"IMPORTANT: Return the ACTUAL API key from the page. Do NOT fabricate one."
        )
        extract_result = await self.executor.execute_task(extract_task)
        if extract_result.get("status") != "completed":
            return {"status": "failed",
                    "error": f"API key extraction failed: {extract_result}"}

        # Parse API key from executor response
        result_text = json.dumps(extract_result, default=str)
        import re
        api_key_match = re.search(
            r'API_KEY:\s*([a-f0-9-]{30,})', result_text, re.IGNORECASE)
        if not api_key_match:
            # Try UUID format
            api_key_match = re.search(
                r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
                result_text, re.IGNORECASE,
            )
        if not api_key_match:
            return {"status": "failed",
                    "error": "Could not parse API key from Mailslurp dashboard"}

        api_key = api_key_match.group(1)

        # Store in config (encrypted)
        self.config.comms.mailslurp_api_key = api_key
        self.config.save()
        logger.info("Mailslurp API key self-provisioned and saved to config")

        # Also store the Mailslurp account for reference
        self.identity.store_account(
            platform="mailslurp",
            identifier=temp_email,
            credentials={"password": ms_password, "api_key": api_key},
            metadata={"type": "service_account", "provider": "mailslurp"},
        )

        return {"status": "completed", "api_key_length": len(api_key)}

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
