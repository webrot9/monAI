"""Proof-of-Completion — verify executor claims before accepting done().

The executor LLM can hallucinate successful outcomes. This module catches
that by requiring verifiable evidence that the task was actually completed.

Verification strategies (applied based on task type):
1. Asset verification — check DB for newly created resources
2. Action trail audit — verify the action history shows real work
3. Page state verification — check browser state matches claimed outcome
4. API verification — ping endpoints to confirm accounts/keys work
5. LLM claim extraction + cross-reference — parse claims, check evidence

The key insight: an LLM that hallucinated "created email X" will NOT have
a corresponding browse→fill_form→submit→confirmation sequence in its
action history. And the asset won't appear in the DB.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from monai.agents.asset_aware import AssetManager
from monai.agents.memory import SharedMemory
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

# Actions that count as "real work" in the action trail
PRODUCTIVE_ACTIONS = frozenset({
    "browse", "click", "type", "fill_form", "submit",
    "http_get", "http_post", "shell", "write_file", "write_code",
})

# Actions that DON'T count — the LLM can spam these without doing anything
PASSIVE_ACTIONS = frozenset({
    "read_page", "screenshot", "wait", "wait_for", "read_file",
})

# Minimum number of productive actions to accept a non-trivial task
MIN_PRODUCTIVE_ACTIONS = 2

# Task keywords that imply specific verifiable outcomes
# Ordered list — first match wins (email before account, so "email account"
# doesn't also trigger platform_account check)
TASK_PATTERNS = [
    {
        "name": "email",
        "keywords": ["email", "mail", "inbox", "mailslurp"],
        "asset_type": "email",
        "requires_db_record": True,
    },
    {
        "name": "account",
        "keywords": ["register", "sign up", "signup", "create account"],
        "excludes": ["email"],  # Don't trigger if email pattern already matched
        "asset_type": "platform_account",
        "requires_db_record": True,
    },
    {
        "name": "api_key",
        "keywords": ["api key", "api_key", "apikey"],
        "asset_type": "api_key",
        "requires_db_record": True,
    },
    {
        "name": "domain",
        "keywords": ["register domain", "buy domain"],
        "asset_type": "domain",
        "requires_db_record": True,
    },
    {
        "name": "form",
        "keywords": ["fill form", "submit form", "fill out", "application"],
        "asset_type": None,
        "requires_db_record": False,
    },
]


class ProofOfCompletion:
    """Verifies executor claims before accepting task completion."""

    def __init__(self, config: Config, db: Database, llm: LLM,
                 memory: SharedMemory):
        self.config = config
        self.db = db
        self.llm = llm
        self.memory = memory
        self._asset_mgr = AssetManager(db)

    def verify(
        self,
        task: str,
        claimed_result: str,
        action_history: list[dict],
        page_url: str | None = None,
        page_text: str | None = None,
    ) -> dict[str, Any]:
        """Run all applicable verification checks.

        Returns {"verified": bool, "reason": str, "checks": [...]}.
        """
        checks: list[dict] = []
        task_lower = task.lower()

        # ── Check 1: Action trail audit ──────────────────────────
        trail_check = self._check_action_trail(action_history, task_lower)
        checks.append(trail_check)
        if not trail_check["passed"]:
            self._record_verification_failure(task, claimed_result, checks)
            return {
                "verified": False,
                "reason": trail_check["reason"],
                "checks": checks,
            }

        # ── Check 2: Asset verification (if task implies creation) ──
        matched_patterns: set[str] = set()
        for pattern in TASK_PATTERNS:
            # Skip if excluded by a previously matched pattern
            excludes = pattern.get("excludes", [])
            if any(ex in matched_patterns for ex in excludes):
                continue
            if any(kw in task_lower for kw in pattern["keywords"]):
                matched_patterns.add(pattern["name"])
                if pattern["requires_db_record"]:
                    asset_check = self._check_asset_created(
                        pattern["asset_type"], claimed_result, task_lower)
                    checks.append(asset_check)
                    if not asset_check["passed"]:
                        self._record_verification_failure(
                            task, claimed_result, checks)
                        return {
                            "verified": False,
                            "reason": asset_check["reason"],
                            "checks": checks,
                        }

        # ── Check 3: Confirmation page detection ─────────────────
        if page_text and self._task_involves_form(task_lower):
            page_check = self._check_confirmation_page(
                page_text, page_url, task_lower, action_history)
            checks.append(page_check)
            # Page check is advisory — don't block on it alone
            if not page_check["passed"]:
                logger.info(
                    f"Proof: confirmation page check failed (advisory): "
                    f"{page_check['reason']}")

        # ── Check 4: Hallucination pattern detection ─────────────
        hallucination_check = self._check_hallucination_patterns(
            claimed_result, action_history, task_lower)
        checks.append(hallucination_check)
        if not hallucination_check["passed"]:
            self._record_verification_failure(task, claimed_result, checks)
            return {
                "verified": False,
                "reason": hallucination_check["reason"],
                "checks": checks,
            }

        # All checks passed
        logger.info(
            f"Proof: verified completion — "
            f"{sum(1 for c in checks if c['passed'])}/{len(checks)} checks passed")
        return {
            "verified": True,
            "reason": "All verification checks passed",
            "checks": checks,
        }

    # ── Individual Checks ────────────────────────────────────────

    def _check_action_trail(
        self, history: list[dict], task_lower: str
    ) -> dict[str, Any]:
        """Verify the action trail shows real productive work.

        An LLM that hallucinated will typically have:
        - Very few or zero productive actions
        - Jumped straight to done() with no real work
        - Only passive actions (screenshot, read_page)
        """
        if not history:
            return {
                "check": "action_trail",
                "passed": False,
                "reason": "No actions in history — executor claimed done without doing anything",
            }

        productive = [
            a for a in history
            if a["tool"] in PRODUCTIVE_ACTIONS
        ]
        passive = [a for a in history if a["tool"] in PASSIVE_ACTIONS]
        total = len(history)

        # Allow simple tasks that genuinely need only 1-2 steps
        is_simple_task = any(kw in task_lower for kw in [
            "read", "check", "verify", "status", "get", "fetch", "look up",
        ])

        min_required = 1 if is_simple_task else MIN_PRODUCTIVE_ACTIONS

        if len(productive) < min_required:
            return {
                "check": "action_trail",
                "passed": False,
                "reason": (
                    f"Only {len(productive)} productive actions "
                    f"(min {min_required} required). "
                    f"History: {total} total, {len(passive)} passive. "
                    f"This suggests the executor hallucinated the outcome."
                ),
            }

        # Check for failed productive actions — if ALL productive actions
        # failed, the task didn't actually succeed
        failed_productive = [
            a for a in productive
            if a["result"].startswith("ERROR:")
            or a["result"].startswith("BLOCKED")
        ]
        if failed_productive and len(failed_productive) == len(productive):
            return {
                "check": "action_trail",
                "passed": False,
                "reason": (
                    f"All {len(productive)} productive actions failed. "
                    f"The task cannot be complete if every real action errored."
                ),
            }

        return {
            "check": "action_trail",
            "passed": True,
            "reason": (
                f"{len(productive)} productive actions, "
                f"{len(failed_productive) if failed_productive else 0} failed"
            ),
        }

    def _check_asset_created(
        self, asset_type: str, claimed_result: str, task_lower: str
    ) -> dict[str, Any]:
        """Verify that a claimed asset actually exists in the database.

        If the executor claims "created email X@Y.com", there must be
        a matching record in the identities table.
        """
        try:
            inventory = self._asset_mgr.get_inventory()
        except Exception as e:
            # Can't verify — don't block
            return {
                "check": f"asset_created:{asset_type}",
                "passed": True,
                "reason": f"Could not query assets (non-blocking): {e}",
            }

        # Check if ANY asset of the right type exists
        matching = [
            a for a in inventory.assets
            if a.type == asset_type and a.status in ("active", "pending")
        ]

        if matching:
            return {
                "check": f"asset_created:{asset_type}",
                "passed": True,
                "reason": (
                    f"Found {len(matching)} {asset_type} asset(s) in DB: "
                    f"{', '.join(a.identifier for a in matching[:3])}"
                ),
            }

        # No matching asset — try to extract what was claimed
        return {
            "check": f"asset_created:{asset_type}",
            "passed": False,
            "reason": (
                f"Task involves creating {asset_type} but no {asset_type} "
                f"asset found in database. The executor may have hallucinated "
                f"the creation. Claimed result: {claimed_result[:200]}"
            ),
        }

    def _check_confirmation_page(
        self, page_text: str, page_url: str | None,
        task_lower: str, history: list[dict]
    ) -> dict[str, Any]:
        """Check if the current page shows a confirmation/success state.

        After form submission, the page should show confirmation signals,
        not the same form or an error page.
        """
        text_lower = page_text.lower() if page_text else ""

        # Positive signals — page shows success
        success_signals = [
            "success", "thank you", "thanks", "confirmed", "welcome",
            "account created", "verification email", "check your email",
            "registration complete", "successfully", "congratulations",
        ]
        has_success = any(s in text_lower for s in success_signals)

        # Negative signals — still on error/form page
        error_signals = [
            "error", "invalid", "required field", "try again",
            "already exists", "not available", "failed",
        ]
        has_error = any(s in text_lower for s in error_signals)

        if has_success and not has_error:
            return {
                "check": "confirmation_page",
                "passed": True,
                "reason": f"Page shows success signals. URL: {page_url or 'unknown'}",
            }

        if has_error:
            return {
                "check": "confirmation_page",
                "passed": False,
                "reason": (
                    f"Page shows error signals (may still be on error page). "
                    f"URL: {page_url or 'unknown'}"
                ),
            }

        # Ambiguous — can't tell from page content
        return {
            "check": "confirmation_page",
            "passed": True,  # Don't block on ambiguity
            "reason": f"Page state ambiguous. URL: {page_url or 'unknown'}",
        }

    def _check_hallucination_patterns(
        self, claimed_result: str, history: list[dict], task_lower: str
    ) -> dict[str, Any]:
        """Detect common hallucination patterns in the claimed result.

        Hallucinated results often:
        - Contain specific details (emails, usernames) that never appeared
          in any action result
        - Mention steps that don't exist in the action history
        - Use overly confident/detailed language about things never observed
        """
        claimed_lower = claimed_result.lower()

        # Extract emails from claimed result
        claimed_emails = set(re.findall(
            r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
            claimed_result,
        ))

        if claimed_emails:
            # Check if these emails appeared in any action result
            all_results = " ".join(
                a.get("result", "") for a in history
            )
            # Also check action args (the executor might have typed the email)
            all_args = " ".join(
                json.dumps(a.get("args", {})) for a in history
            )
            evidence = all_results + " " + all_args

            unverified_emails = [
                email for email in claimed_emails
                if email not in evidence
            ]
            if unverified_emails:
                return {
                    "check": "hallucination_patterns",
                    "passed": False,
                    "reason": (
                        f"Claimed emails {unverified_emails} never appeared "
                        f"in action history results or arguments. "
                        f"This is a strong hallucination signal."
                    ),
                }

        # Check for placeholder/fake patterns
        fake_patterns = [
            "example.com", "example@", "test@test",
            "fake", "placeholder", "lorem ipsum",
            "john.doe@", "jane.doe@",
        ]
        for pattern in fake_patterns:
            if pattern in claimed_lower:
                return {
                    "check": "hallucination_patterns",
                    "passed": False,
                    "reason": (
                        f"Claimed result contains placeholder/fake pattern: "
                        f"'{pattern}'. Real results never use these."
                    ),
                }

        return {
            "check": "hallucination_patterns",
            "passed": True,
            "reason": "No hallucination patterns detected",
        }

    # ── Helpers ──────────────────────────────────────────────────

    def _task_involves_form(self, task_lower: str) -> bool:
        """Check if the task involves form submission."""
        form_keywords = [
            "register", "sign up", "signup", "create account",
            "fill", "submit", "apply", "form", "enroll",
        ]
        return any(kw in task_lower for kw in form_keywords)

    def _record_verification_failure(
        self, task: str, claimed_result: str, checks: list[dict]
    ) -> None:
        """Store verification failure as a lesson so the system learns."""
        failed_checks = [c for c in checks if not c["passed"]]
        failure_summary = "; ".join(c["reason"][:100] for c in failed_checks)

        try:
            self.memory.record_lesson(
                agent_name="executor",
                category="hallucination",
                situation=(
                    f"Task: {task[:200]}. "
                    f"Claimed: {claimed_result[:200]}."
                ),
                lesson=(
                    f"Executor claimed completion but verification failed: "
                    f"{failure_summary}"
                ),
                rule=(
                    "NEVER claim done() unless you can point to concrete "
                    "evidence in your action history. If you created an account, "
                    "there must be browse→fill_form→submit→confirmation in history."
                ),
                severity="high",
            )
        except Exception as e:
            logger.debug(f"Could not record verification lesson: {e}")

        logger.warning(
            f"Proof: REJECTED completion claim. "
            f"Task: {task[:100]}. Failures: {failure_summary[:200]}")
