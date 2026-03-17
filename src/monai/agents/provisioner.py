"""Self-provisioning agent — acquires everything monAI needs to operate.

Registers on platforms, creates accounts, gets API keys, registers domains,
sets up email, and acquires any tools/services needed. Fully autonomous.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from monai.agents.asset_aware import AssetManager
from monai.agents.base import BaseAgent
from monai.agents.constraint_planner import ConstraintPlanner, ProvisioningStep
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

    # How long a platform provisioning failure stays "blocked" before retry.
    # Escalates: 1hr → 6hr → 24hr on repeated failures.
    _PROVISION_FAIL_TTL_TIERS = [3600, 21600, 86400]

    # Patterns in failure reasons that indicate permanent proxy blockage.
    # These platforms block ALL anonymous access — retrying is pointless.
    _PROXY_BLOCK_PATTERNS = [
        "all proxies blocked",
        "all proxy methods are blocked",
        "refusing to connect without proxy",
    ]

    _PROVISION_FAIL_SCHEMA = """\
    CREATE TABLE IF NOT EXISTS provision_failures (
        action TEXT NOT NULL,
        platform TEXT NOT NULL,
        failed_at REAL NOT NULL,
        fail_count INTEGER DEFAULT 1,
        reason TEXT,
        permanent INTEGER DEFAULT 0,
        PRIMARY KEY (action, platform)
    );
    """

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.identity = IdentityManager(config, db, llm)
        self.executor = AutonomousExecutor(config, db, llm)
        self.constraint_planner = ConstraintPlanner(db, llm)
        self.email_verifier = EmailVerifier(config, db)

        with db.connect() as conn:
            conn.executescript(self._PROVISION_FAIL_SCHEMA)
            # Migration: add 'permanent' column if missing (table created before it existed)
            try:
                conn.execute("SELECT permanent FROM provision_failures LIMIT 1")
            except Exception:
                conn.execute(
                    "ALTER TABLE provision_failures ADD COLUMN permanent INTEGER DEFAULT 0"
                )

    def _is_provision_blocked(self, action: str, platform: str) -> bool:
        """Check if a provisioning action is still blocked from a past failure."""
        rows = self.db.execute(
            "SELECT failed_at, fail_count, permanent FROM provision_failures "
            "WHERE action = ? AND platform = ?",
            (action, platform),
        )
        if not rows:
            return False
        # Permanent blocks (proxy-blocked platforms) never expire
        if rows[0]["permanent"]:
            return True
        failed_at = rows[0]["failed_at"]
        count = rows[0]["fail_count"]
        tier = min(count - 1, len(self._PROVISION_FAIL_TTL_TIERS) - 1)
        ttl = self._PROVISION_FAIL_TTL_TIERS[tier]
        if time.time() - failed_at < ttl:
            return True
        # TTL expired — clean up
        self.db.execute(
            "DELETE FROM provision_failures WHERE action = ? AND platform = ?",
            (action, platform),
        )
        return False

    def _is_proxy_block_failure(self, reason: str) -> bool:
        """Check if a failure reason indicates permanent proxy blockage."""
        reason_lower = reason.lower()
        return any(p in reason_lower for p in self._PROXY_BLOCK_PATTERNS)

    def _record_provision_failure(self, action: str, platform: str,
                                  reason: str = "") -> None:
        """Record a provisioning failure with escalating TTL.

        Proxy-blocked failures are marked permanent — the platform blocks
        all anonymous proxies and retrying is pointless waste.
        """
        now = time.time()
        permanent = 1 if self._is_proxy_block_failure(reason) else 0
        rows = self.db.execute(
            "SELECT fail_count, permanent FROM provision_failures "
            "WHERE action = ? AND platform = ?",
            (action, platform),
        )
        count = (rows[0]["fail_count"] + 1) if rows else 1
        # Once permanent, stays permanent
        if rows and rows[0]["permanent"]:
            permanent = 1
        self.db.execute(
            "INSERT INTO provision_failures (action, platform, failed_at, "
            "fail_count, reason, permanent) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(action, platform) DO UPDATE SET "
            "failed_at = excluded.failed_at, "
            "fail_count = excluded.fail_count, "
            "reason = excluded.reason, "
            "permanent = MAX(provision_failures.permanent, excluded.permanent)",
            (action, platform, now, count, reason[:500], permanent),
        )
        if permanent:
            logger.info(
                f"Provisioning failure #{count} for {action}:{platform} — "
                f"PERMANENTLY blocked (proxy-blocked platform)")
        else:
            tier = min(count - 1, len(self._PROVISION_FAIL_TTL_TIERS) - 1)
            ttl = self._PROVISION_FAIL_TTL_TIERS[tier]
            logger.info(
                f"Provisioning failure #{count} for {action}:{platform} — "
                f"blocked for {ttl}s"
            )

    def _get_failure_context(self) -> str:
        """Build human-readable failure history for LLM context injection."""
        rows = self.db.execute(
            "SELECT action, platform, fail_count, reason, permanent "
            "FROM provision_failures ORDER BY fail_count DESC"
        )
        if not rows:
            return ""
        lines = ["PAST PROVISIONING FAILURES (do NOT retry these — choose alternatives):"]
        for r in rows:
            if r["permanent"]:
                status = "PERMANENTLY BLOCKED — platform blocks all proxies, NEVER retry"
            elif self._is_provision_blocked(r["action"], r["platform"]):
                status = "STILL BLOCKED"
            else:
                status = "TTL expired, may retry"
            lines.append(
                f"  - {r['action']} on {r['platform']}: "
                f"failed {r['fail_count']}x — {r['reason'] or 'unknown'} [{status}]"
            )
        return "\n".join(lines)

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

        # Include failure history so the LLM knows what's been tried and failed
        failure_history = self._get_failure_context()

        context = (
            f"Identity: {json.dumps(identity, default=str)}\n"
            f"Existing accounts: {json.dumps([{'platform': a['platform'], 'type': a['type']} for a in accounts], default=str)}\n"
            f"Monthly resource costs: ${resources_cost:.2f}\n"
            f"\n{asset_inventory}\n"
            f"\n{failure_history}\n"
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
        # Include platform in goal string so the constraint planner
        # can extract it (e.g., "register_on_platform on upwork")
        goals = []
        for s in sorted(steps, key=lambda x: x.get("priority", 99)):
            action = s.get("action", "")
            platform = s.get("platform", "")
            if platform and platform.lower() not in action.lower():
                goals.append(f"{action} on {platform}")
            else:
                goals.append(action)
        return goals

    def run(self, needs: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Run provisioning cycle with constraint-aware planning.

        Uses the ConstraintPlanner to build a dependency graph, then executes
        steps in topological order — prerequisites first, dependents after.

        Args:
            needs: Specific items to provision (e.g. ['telegram_bot', 'email']).
                   When provided, skips the LLM plan() call entirely and uses
                   these as direct goals. This prevents scope creep where the
                   LLM decides to register on 7 platforms when only telegram_bot
                   is needed.
        """
        self.log_action("run_start", "Starting provisioning cycle")

        if needs:
            goals = needs
            self.log_action("plan", f"Using orchestrator needs directly: {goals}")
        else:
            goals = self.plan()
        graph = self.constraint_planner.plan(goals)

        self.log_action("constraint_plan", graph.summary()[:500])

        # Execute in dependency order
        results = graph_results = {}
        failed_action_platforms: set[str] = set()  # Track (action:platform) this cycle
        max_rounds = len(graph.steps) * 2  # safety cap

        for _ in range(max_rounds):
            ready = graph.get_ready_steps()
            if not ready:
                break

            for step in ready:
                platform = step.platform or ""

                # Check persistent failure history (cross-cycle persistence)
                if platform and self._is_provision_blocked(step.action, platform):
                    logger.info(
                        f"Skipping '{step.action}' for {platform} — "
                        f"still blocked from previous failure (persistent)")
                    graph.mark_failed(step.id,
                                      f"Blocked: {step.action} on {platform} "
                                      f"failed recently, waiting for TTL")
                    continue

                # Dedup: skip steps whose (action, platform) already failed THIS cycle.
                # We do NOT skip other platforms — Freelancer CAPTCHA should not
                # block Stripe registration.
                action_platform_key = f"{step.action}:{platform}"
                if action_platform_key in failed_action_platforms:
                    logger.info(
                        f"Skipping '{step.action}' for {platform} — "
                        f"same action+platform already failed this cycle")
                    graph.mark_failed(step.id, f"Skipped: {action_platform_key} already failed")
                    continue

                step_result = self._execute_provisioning(step)
                results[f"{step.action}:{platform}"] = step_result

                if isinstance(step_result, dict) and step_result.get("status") in (
                    "completed", "already_registered", "already_exists", "already_have",
                ):
                    graph.mark_completed(step.id)
                elif isinstance(step_result, dict) and step_result.get("status") in (
                    "failed", "blocked", "skipped",
                ):
                    reason = step_result.get("reason", step_result.get("status", "unknown"))
                    graph.mark_failed(step.id, reason)
                    failed_action_platforms.add(f"{step.action}:{platform}")
                    # Persist failure for cross-cycle learning
                    if platform:
                        self._record_provision_failure(
                            step.action, platform, reason
                        )
                else:
                    # Assume success if not explicitly failed
                    graph.mark_completed(step.id)

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    async def register_on_platform(self, platform: str) -> dict[str, Any]:
        """Register monAI on a platform (Upwork, Fiverr, etc.).

        After browser-based registration, validates that stored credentials
        contain the fields the platform API actually requires. For social
        platforms (LinkedIn, Twitter, Reddit), browser signup alone does NOT
        produce API credentials — those require OAuth app setup.
        """
        if self.identity.has_account(platform):
            # Double-check: existing account might have stale/incomplete creds
            from monai.social.api import get_required_credential_fields
            required = get_required_credential_fields(platform)
            if required:
                existing = self.identity.get_account(platform)
                creds = (existing or {}).get("credentials", {}) or {}
                missing = [f for f in required if not creds.get(f)]
                if missing:
                    logger.warning(
                        "Account for %s exists but missing API fields %s "
                        "— cannot register via browser (needs OAuth setup)",
                        platform, missing,
                    )
                    return {
                        "status": "blocked",
                        "reason": f"{platform} requires API credentials "
                                  f"({', '.join(required)}) that cannot be "
                                  f"obtained via browser signup alone",
                        "missing_fields": missing,
                    }
            return {"status": "already_registered", "platform": platform}

        # Check if this platform requires OAuth/API credentials that
        # browser signup cannot provide. Don't waste LLM calls on
        # registrations that will produce unusable accounts.
        from monai.social.api import get_required_credential_fields
        required = get_required_credential_fields(platform)
        if required:
            # Social platforms with API requirements can't be auto-provisioned
            # via browser form-fill alone — they need OAuth app setup.
            logger.info(
                "Platform %s requires API credentials (%s) — "
                "skipping browser registration (needs manual OAuth setup)",
                platform, ", ".join(required),
            )
            return {
                "status": "blocked",
                "reason": f"{platform} requires API credentials "
                          f"({', '.join(required)}) that cannot be "
                          f"obtained via browser signup alone. "
                          f"Provide credentials manually or via OAuth flow.",
                "missing_fields": list(required),
            }

        # Ensure we have an email before attempting registration
        email_account = self.identity.get_account("email")
        if not email_account:
            return {"status": "blocked", "reason": "No email account available"}
        email = email_account["identifier"]

        # Use the SAME business identity for all platforms — consistency matters.
        # Different names per platform looks suspicious and wastes LLM calls.
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
                metadata={
                    "registration_result": result,
                    "platform_identity": identity,
                },
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

        # Step 2: Verify the API key actually works BEFORE creating an inbox
        key = self.config.comms.mailslurp_api_key
        if not key:
            return {"status": "failed", "reason": "No Mailslurp API key available"}

        key_valid = self.email_verifier.verify_mailslurp_key(key)
        if not key_valid:
            logger.error("Mailslurp API key is invalid — clearing and failing")
            self.config.comms.mailslurp_api_key = ""
            self.config.save()
            return {
                "status": "failed",
                "reason": "Mailslurp API key failed verification (likely hallucinated)",
            }

        # Step 3: Create inbox via API
        identity = self.identity.get_identity()
        inbox_name = identity.get("name", "monAI")

        result = self.email_verifier.create_mailslurp_inbox(name=inbox_name)
        if result.get("status") != "created":
            logger.error(f"Mailslurp inbox creation failed: {result}")
            return {
                "status": "failed",
                "reason": result.get("error", "Mailslurp API error"),
            }

        # Step 4: Verify the inbox actually exists by reading it back
        inbox_verified = self.email_verifier.verify_mailslurp_inbox(
            result["inbox_id"])
        if not inbox_verified:
            logger.error(
                f"Mailslurp inbox {result['inbox_id']} created but "
                f"verification read-back failed — not storing as primary email")
            return {
                "status": "failed",
                "reason": "Inbox creation claimed success but read-back failed",
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
        logger.info(f"Email provisioned via Mailslurp (verified): {result['address']}")
        return {"status": "completed", "email": result["address"]}

    def _provision_telegram_bot(self) -> dict[str, Any]:
        """Create a Telegram bot via BotFather using the executor.

        Requires a Telegram account (virtual phone number needed).
        On success, stores the bot token in TelegramBot state.
        """
        from monai.utils.telegram import TelegramBot

        # Pre-flight: check if we have the prerequisites to acquire a phone number.
        # Without a configured SMS verification service (API key + funds),
        # the executor will burn 15+ LLM steps searching for free phone numbers
        # and then fail anyway.
        sms_api_key = getattr(self.config, 'sms_api_key', '') or ''
        has_payment = bool(self.db.execute(
            "SELECT 1 FROM identities WHERE type = 'payment_method' AND status = 'active' LIMIT 1"
        ))
        if not sms_api_key and not has_payment:
            logger.warning(
                "Skipping Telegram bot provisioning: no SMS verification service "
                "configured and no payment method to acquire one"
            )
            return {
                "status": "failed",
                "reason": (
                    "Cannot create Telegram bot: no SMS verification service configured "
                    "and no payment method to acquire one. Need either sms_api_key in config "
                    "or a payment method to buy a virtual phone number."
                ),
            }

        # Get provisioning task from TelegramBot utility
        telegram = TelegramBot(self.config, self.db)
        if telegram.has_token:
            return {"status": "already_exists"}

        task_info = telegram.get_provisioning_task()
        identity = self.identity.get_identity()

        result = self.execute_task(
            task_info["task"],
            context=json.dumps(identity, default=str),
        )

        if result.get("status") == "completed":
            # Extract bot token from result
            token = result.get("bot_token") or result.get("token", "")
            if token and ":" in token:
                telegram.set_bot_token(token)
                self.log_action("provision_telegram_bot", "Bot token acquired and stored")
                return {"status": "completed", "bot_created": True}
            else:
                self.log_action("provision_telegram_bot",
                                "Task completed but no valid token extracted")
                return {"status": "failed",
                        "reason": "No valid bot token in executor result"}

        return {"status": "failed", "reason": result.get("error", "Executor failed")}

    async def _provision_mailslurp_key(self) -> dict[str, Any]:
        """Self-provision a Mailslurp API key autonomously.

        Uses a disposable mail.tm email ONLY for the Mailslurp signup.
        After getting the API key, the temp email can expire — doesn't matter.

        IMPORTANT: The temp email is NEVER stored as the agent's primary email.
        It is disposable bootstrap infrastructure only.
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

        # VERIFY the API key before trusting it — the executor could have
        # hallucinated the key extraction from the Mailslurp dashboard
        if not self.email_verifier.verify_mailslurp_key(api_key):
            logger.error(
                "Extracted API key failed verification — executor likely "
                "hallucinated the key extraction. NOT storing.")
            return {
                "status": "failed",
                "error": "Extracted Mailslurp API key is invalid (verification failed)",
            }

        # Store in config (encrypted) — verified to be real
        self.config.comms.mailslurp_api_key = api_key
        self.config.save()
        logger.info("Mailslurp API key self-provisioned, VERIFIED, and saved to config")

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
            validator = self.identity.validator
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

    def _execute_provisioning(self, step: ProvisioningStep) -> dict[str, Any]:
        """Execute a provisioning step (sync wrapper for async operations)."""
        action = step.action
        platform = step.platform

        # Pre-check: verify assets exist before attempting registration
        missing = AssetManager(self.db).get_missing_prerequisites(action)
        if missing:
            logger.warning(f"Cannot execute '{action}' ({platform}): missing {missing}")
            return {"status": "blocked", "missing_prerequisites": missing}

        # Match any platform registration step: register_platform_account,
        # register_on_platform, platform_signup, etc.
        is_platform_reg = (
            ("register" in action.lower() and "platform" in action.lower())
            or action.lower() in ("platform_signup", "platform_registration")
            or ("signup" in action.lower() and platform)
        )
        if is_platform_reg:
            # Use platform directly from the step — no LLM extraction needed
            if not platform or platform.lower() in ("platform", "unknown", ""):
                logger.warning(f"Step '{action}' has no valid platform name: '{platform}' — skipping")
                return {"status": "skipped", "reason": f"No valid platform name for step '{action}'"}
            return self._run_async(self.register_on_platform(platform.lower()))
        elif "email" in action.lower():
            return self._run_async(self.setup_email())
        elif "domain" in action.lower():
            # Extract or generate a domain, then validate before registering
            domain_name = self.think(
                f"Extract just the domain name from this step: '{action}'. "
                "Reply with only the domain name (e.g. 'example.com'). "
                "If no specific domain is mentioned, generate a unique, "
                "professional name (e.g. 'nexifydigital.com')."
            ).strip().strip("'\"").lower()

            # Guard: don't attempt registration with empty or invalid domain
            if not domain_name or "." not in domain_name or len(domain_name) < 4:
                logger.error(f"Invalid domain name extracted: '{domain_name}'")
                return {"status": "failed", "reason": f"Invalid domain name: '{domain_name}'"}

            # Validate and find an available domain
            try:
                validator = self.identity.validator
                check = validator.check_domain(domain_name)
                if check.available is False:
                    # Domain taken — generate and validate a new one
                    identity, validation = validator.generate_and_validate(
                        domain_tlds=[".com", ".io", ".co", ".dev"],
                    )
                    domain_name = identity.get("validated_domain") or ""
                    # Fallback: if generate_and_validate found a viable name
                    # but no available domain, try .dev / .ai / .agency TLDs
                    if not domain_name and identity.get("name"):
                        import re as _re
                        slug = _re.sub(r'[^a-z0-9]', '', identity["name"].lower())
                        for tld in [".dev", ".agency", ".tools", ".site"]:
                            fallback = f"{slug}{tld}"
                            fb_check = validator.check_domain(fallback)
                            if fb_check.available is True:
                                domain_name = fallback
                                break
                    logger.info(f"Original domain taken, using validated: {domain_name}")
            except Exception as e:
                logger.warning(f"Domain validation failed ({e}), using original: {domain_name}")

            if not domain_name:
                return {"status": "failed", "reason": "No viable domain name found after validation"}

            return self._run_async(self.register_domain(domain_name))
        elif "telegram_bot" in action.lower():
            return self._provision_telegram_bot()
        elif "api" in action.lower():
            return self._run_async(self.acquire_api_key(action))
        else:
            return self._run_async(
                self.executor.execute_task(action, json.dumps(self.identity.get_identity(), default=str))
            )
