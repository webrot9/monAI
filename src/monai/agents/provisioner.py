"""Self-provisioning agent — acquires everything monAI needs to operate.

Registers on platforms, creates accounts, gets API keys, registers domains,
sets up email, and acquires any tools/services needed. Fully autonomous.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.agents.base import BaseAgent
from monai.agents.executor import AutonomousExecutor
from monai.agents.identity import IdentityManager
from monai.agents.playbooks import get_playbook
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

# Maximum retries per provisioning task before giving up
MAX_RETRIES = 2


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

    def plan(self) -> list[str]:
        """Determine what needs to be provisioned."""
        identity = self.identity.get_identity()
        accounts = self.identity.get_all_accounts()
        resources_cost = self.identity.get_monthly_resource_costs()

        context = (
            f"Identity: {json.dumps(identity, default=str)}\n"
            f"Existing accounts: {json.dumps([{'platform': a['platform'], 'type': a['type']} for a in accounts], default=str)}\n"
            f"Monthly resource costs: ${resources_cost:.2f}\n"
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
        """Run provisioning cycle — set up everything needed."""
        self.log_action("run_start", "Starting provisioning cycle")

        steps = self.plan()
        results = {}

        for step in steps:
            result = self._execute_provisioning(step)
            results[step] = result

            # If the step failed, log analysis for future improvement
            if isinstance(result, dict) and result.get("status") == "failed":
                self._analyze_failure(step, result)

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    async def register_on_platform(self, platform: str) -> dict[str, Any]:
        """Register monAI on a platform (Upwork, Fiverr, etc.)."""
        if self.identity.has_account(platform):
            return {"status": "already_registered", "platform": platform}

        identity = self.identity.get_identity()
        password = self.identity.generate_password()

        # Build a rich task description with playbook knowledge
        task = self._build_registration_task(platform, identity, password)

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

    def _build_registration_task(self, platform: str, identity: dict, password: str) -> str:
        """Build a detailed task description using playbook knowledge."""
        playbook = get_playbook(platform)

        # Base task info
        parts = [
            f"Register a new account on {platform}.",
            f"Identity details:",
            f"  - Name: {identity.get('name', 'Nexify Digital')}",
            f"  - Username: {identity.get('preferred_username', 'nexifydigital')}",
            f"  - Password: {password}",
            f"  - Description: {identity.get('description', 'AI-powered digital services')}",
        ]

        if playbook:
            # Add specific URL
            if playbook.get("signup_url"):
                parts.append(f"\nSTART HERE: Navigate to {playbook['signup_url']}")
            elif playbook.get("note"):
                parts.append(f"\nIMPORTANT: {playbook['note']}")

            # Add step-by-step instructions
            if playbook.get("steps"):
                parts.append("\nFOLLOW THESE STEPS:")
                for i, step in enumerate(playbook["steps"], 1):
                    parts.append(f"  {i}. {step}")

            # Add error recovery guidance
            if playbook.get("error_recovery"):
                parts.append("\nIF YOU ENCOUNTER ERRORS:")
                for error, fix in playbook["error_recovery"].items():
                    parts.append(f"  - '{error}': {fix}")
        else:
            # Generic fallback — at least provide basic guidance
            parts.extend([
                f"\nSTEPS:",
                f"1. First use create_temp_email() to get an email for signup",
                f"2. Navigate to {platform}'s registration/signup page",
                f"3. Use read_page() to understand the form",
                f"4. Use fill_form() to fill in all fields",
                f"5. Submit the form",
                f"6. Check for verification requirements (email/phone)",
                f"7. Complete verification if needed",
                f"8. Take a screenshot for proof",
            ])

        parts.append(
            "\nWhen done, call done() with a summary of what was created "
            "(username, email used, etc.)."
        )
        return "\n".join(parts)

    async def setup_email(self) -> dict[str, Any]:
        """Set up an email account for the agent."""
        if self.identity.has_account("email"):
            return {"status": "already_exists"}

        identity = self.identity.get_identity()
        playbook = get_playbook("protonmail")

        task_parts = [
            "Create a ProtonMail email account for business use.",
            f"Preferred username: {identity.get('preferred_username', 'nexifydigital')}",
        ]

        if playbook:
            task_parts.append(f"\nSTART HERE: Navigate to {playbook.get('signup_url', '')}")
            if playbook.get("steps"):
                task_parts.append("\nFOLLOW THESE STEPS:")
                for i, step in enumerate(playbook["steps"], 1):
                    task_parts.append(f"  {i}. {step}")

        task_parts.append(
            "\nALTERNATIVE: If ProtonMail signup is blocked or requires phone, "
            "use create_temp_email() to get an instant disposable email via API. "
            "This is faster and doesn't require browser signup."
        )
        task_parts.append(
            "\nWhen done, call done() with the email address that was created."
        )

        task = "\n".join(task_parts)
        result = await self.executor.execute_task(task, json.dumps(identity, default=str))
        return result

    async def register_domain(self, domain: str, registrar: str = "namecheap") -> dict[str, Any]:
        """Register a domain name."""
        playbook = get_playbook(registrar)

        task_parts = [
            f"Register the domain '{domain}' on {registrar}.",
        ]

        if playbook:
            search_url = playbook.get("search_url", "")
            if search_url:
                task_parts.append(f"\nSTART HERE: Navigate to {search_url}{domain}")
            if playbook.get("steps"):
                task_parts.append("\nFOLLOW THESE STEPS:")
                for i, step in enumerate(playbook["steps"], 1):
                    # Replace {domain} placeholder in steps
                    task_parts.append(f"  {i}. {step.replace('{domain}', domain)}")
            if playbook.get("error_recovery"):
                task_parts.append("\nIF YOU ENCOUNTER ERRORS:")
                for error, fix in playbook["error_recovery"].items():
                    task_parts.append(f"  - '{error}': {fix}")
        else:
            task_parts.extend([
                f"1. Navigate to {registrar}'s website",
                f"2. Search for the domain '{domain}'",
                f"3. If available, add to cart and purchase",
                f"4. Take screenshots of each step",
            ])

        task_parts.append(
            "\nWhen done, call done() with confirmation that the domain was registered."
        )

        task = "\n".join(task_parts)
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
            "Steps:\n"
            "1. Use create_temp_email() to get an email for signup\n"
            "2. Go to their developer portal / API signup page\n"
            "3. Register an account if needed\n"
            "4. Navigate to the API keys section\n"
            "5. Create a new API key\n"
            "6. Call done() with the API key value\n"
        )

        identity = self.identity.get_identity()
        result = await self.executor.execute_task(task, json.dumps(identity, default=str))
        return result

    def _execute_provisioning(self, step: str) -> dict[str, Any]:
        """Execute a provisioning step with retry logic."""
        for attempt in range(MAX_RETRIES + 1):
            result = self._execute_single(step)

            if isinstance(result, dict) and result.get("status") in ("completed", "already_registered", "already_exists", "already_have"):
                return result

            if attempt < MAX_RETRIES:
                logger.info(f"Provisioning step failed (attempt {attempt + 1}/{MAX_RETRIES + 1}), retrying: {step[:80]}")
            else:
                logger.warning(f"Provisioning step failed after {MAX_RETRIES + 1} attempts: {step[:80]}")

        return result

    def _execute_single(self, step: str) -> dict[str, Any]:
        """Execute a single provisioning step."""
        if "register" in step.lower() and "platform" in step.lower():
            platform = self.think(
                f"Extract just the platform name from this step: '{step}'. "
                "Reply with just the platform name, lowercase."
            ).strip().lower()
            return self._run_async(self.register_on_platform(platform))
        elif "email" in step.lower():
            return self._run_async(self.setup_email())
        elif "domain" in step.lower():
            domain_name = self.think(
                f"Extract just the domain name from this step: '{step}'. "
                "Reply with only the domain name (e.g. 'example.com'). "
                "If no specific domain is mentioned, generate one that's "
                "professional and available (e.g. 'nexifydigital.com')."
            ).strip().strip("'\"").lower()
            return self._run_async(self.register_domain(domain_name))
        elif "api" in step.lower():
            return self._run_async(self.acquire_api_key(step))
        else:
            return self._run_async(
                self.executor.execute_task(step, json.dumps(self.identity.get_identity(), default=str))
            )

    def _analyze_failure(self, step: str, result: dict) -> None:
        """Analyze why a provisioning step failed and log insights."""
        reason = result.get("reason", "unknown")
        history = result.get("history", [])

        # Categorize failure
        if "circuit breaker" in reason.lower():
            category = "too_many_errors"
        elif "max_steps" in result.get("status", ""):
            category = "ran_out_of_steps"
        elif "timeout" in result.get("status", ""):
            category = "timeout"
        else:
            category = "task_failed"

        # Extract common error patterns from history
        error_types = set()
        for action in history:
            r = action.get("result", "")
            if "BLOCKED" in r:
                error_types.add("blocked_by_guardrails")
            if "Timeout" in r:
                error_types.add("page_timeout")
            if "proxy" in r.lower():
                error_types.add("proxy_issues")
            if "captcha" in r.lower():
                error_types.add("captcha_required")

        analysis = {
            "step": step,
            "category": category,
            "reason": reason,
            "error_types": list(error_types),
            "steps_taken": result.get("steps", 0),
        }

        logger.warning(
            f"PROVISIONING FAILURE ANALYSIS: {step[:60]} — "
            f"category={category}, errors={error_types}, "
            f"steps={result.get('steps', 0)}"
        )
        self.log_action("failure_analysis", json.dumps(analysis, default=str)[:500])
