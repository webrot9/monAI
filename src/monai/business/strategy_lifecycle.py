"""Strategy lifecycle state machine — enforces valid transitions.

States:
    pending   → active      (start)
    active    → paused      (pause — manual or risk manager)
    active    → stopped     (stop — permanent shutdown)
    paused    → active      (resume)
    paused    → stopped     (stop)
    stopped   → (terminal)

Prevents:
- Running a paused/stopped strategy
- Pausing an already paused strategy
- Resuming a stopped strategy
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from monai.agents.social_presence import BRAND_PLATFORMS
from monai.db.database import Database

logger = logging.getLogger(__name__)

VALID_TRANSITIONS = {
    "pending": {"active"},
    "active": {"paused", "stopped"},
    "paused": {"active", "stopped"},
    "stopped": set(),  # Terminal state
}


class InvalidTransitionError(Exception):
    """Raised when a strategy state transition is not allowed."""
    pass


class StrategyLifecycle:
    """Manages strategy state transitions with enforcement."""

    def __init__(self, db: Database):
        self.db = db

    def get_status(self, strategy_id: int) -> str:
        """Get current status of a strategy."""
        rows = self.db.execute(
            "SELECT status FROM strategies WHERE id = ?", (strategy_id,)
        )
        if not rows:
            raise ValueError(f"Strategy {strategy_id} not found")
        return rows[0]["status"]

    def can_transition(self, strategy_id: int, target_status: str) -> bool:
        """Check if a transition is valid without performing it."""
        current = self.get_status(strategy_id)
        return target_status in VALID_TRANSITIONS.get(current, set())

    def transition(self, strategy_id: int, target_status: str,
                   reason: str = "") -> dict[str, Any]:
        """Perform a state transition with validation.

        Returns:
            Dict with old_status, new_status, and timestamp.

        Raises:
            InvalidTransitionError: If the transition is not valid.
        """
        current = self.get_status(strategy_id)
        allowed = VALID_TRANSITIONS.get(current, set())

        if target_status not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition strategy {strategy_id} from '{current}' to '{target_status}'. "
                f"Allowed transitions from '{current}': {allowed or 'none (terminal state)'}"
            )

        now = datetime.now().isoformat()
        self.db.execute(
            "UPDATE strategies SET status = ?, updated_at = ? WHERE id = ?",
            (target_status, now, strategy_id),
        )

        logger.info(
            f"Strategy {strategy_id}: {current} → {target_status}"
            + (f" (reason: {reason})" if reason else "")
        )

        return {
            "strategy_id": strategy_id,
            "old_status": current,
            "new_status": target_status,
            "timestamp": now,
            "reason": reason,
        }

    def activate(self, strategy_id: int, reason: str = "") -> dict[str, Any]:
        """Activate a pending or paused strategy."""
        return self.transition(strategy_id, "active", reason)

    def pause(self, strategy_id: int, reason: str = "") -> dict[str, Any]:
        """Pause an active strategy."""
        return self.transition(strategy_id, "paused", reason)

    def stop(self, strategy_id: int, reason: str = "") -> dict[str, Any]:
        """Permanently stop a strategy."""
        return self.transition(strategy_id, "stopped", reason)

    def resume(self, strategy_id: int, reason: str = "") -> dict[str, Any]:
        """Resume a paused strategy."""
        return self.transition(strategy_id, "active", reason)

    def is_runnable(self, strategy_id: int) -> bool:
        """Check if a strategy can be executed (must be 'active')."""
        return self.get_status(strategy_id) == "active"

    def get_all_by_status(self, status: str) -> list[dict[str, Any]]:
        """Get all strategies with a given status."""
        rows = self.db.execute(
            "SELECT * FROM strategies WHERE status = ?", (status,)
        )
        return [dict(r) for r in rows]

    def get_lifecycle_summary(self) -> dict[str, int]:
        """Get count of strategies in each state."""
        rows = self.db.execute(
            "SELECT status, COUNT(*) as count FROM strategies GROUP BY status"
        )
        return {r["status"]: r["count"] for r in rows}

    # ── Asset-gated strategy validation ──────────────────────────

    # Minimum assets each strategy needs before it can be activated.
    # Keys: email, platform:<name>, domain, payment, api_key:<name>
    # "email" is the baseline — almost every strategy needs it.
    STRATEGY_REQUIREMENTS: dict[str, list[str]] = {
        # Can start with just email (outreach-based)
        "freelance_writing": ["email"],
        "cold_outreach": ["email"],
        "lead_gen": ["email"],

        # Need email + platform accounts
        "social_media": ["email", "platform:linkedin"],
        "content_sites": ["email", "domain"],
        "newsletter": ["email"],
        "affiliate": ["email"],

        # Need payment infrastructure
        "digital_products": ["email", "payment"],
        "micro_saas": ["email", "payment"],
        "telegram_bots": ["email"],
        "course_creation": ["email", "payment"],

        # Need significant infrastructure
        "domain_flipping": ["email", "payment", "domain"],
        "print_on_demand": ["email", "payment"],
        "saas": ["email", "payment", "domain"],
    }

    def validate_strategies(self) -> dict[str, Any]:
        """Check all strategies against actual assets and activate/deactivate accordingly.

        - pending strategies with all requirements met → activate
        - active strategies missing requirements → pause
        - Cleans up brand_social_accounts for non-active strategies

        Returns summary of changes made.
        """
        # Build asset inventory from DB
        assets = self._get_asset_inventory()

        all_strategies = self.db.execute(
            "SELECT id, name, status FROM strategies WHERE status != 'stopped'"
        )

        activated = []
        paused = []
        already_ok = []

        for row in all_strategies:
            sid = row["id"]
            name = row["name"]
            status = row["status"]
            requirements = self.STRATEGY_REQUIREMENTS.get(name, ["email"])
            missing = self._check_requirements(requirements, assets)

            if not missing:
                # Requirements met
                if status == "pending":
                    try:
                        self.activate(sid, reason="asset requirements met")
                        activated.append(name)
                    except InvalidTransitionError:
                        pass
                elif status == "active":
                    already_ok.append(name)
                elif status == "paused":
                    # Check if it was paused due to missing assets (not manual pause)
                    # Re-activate only if previously paused by validation
                    try:
                        self.activate(sid, reason="asset requirements now met")
                        activated.append(name)
                    except InvalidTransitionError:
                        pass
            else:
                # Requirements NOT met
                if status == "active":
                    try:
                        self.pause(
                            sid,
                            reason=f"missing assets: {', '.join(missing)}",
                        )
                        paused.append({"name": name, "missing": missing})
                    except InvalidTransitionError:
                        pass
                elif status == "pending":
                    logger.debug(
                        f"Strategy '{name}' stays pending — missing: {missing}"
                    )

        # Sync brand_social_accounts with actual strategy states:
        # - Register brands for newly activated strategies
        # - Remove brands for paused/pending strategies
        brands_registered, brands_removed = self._sync_brands(activated, paused)

        summary = {
            "activated": activated,
            "paused": paused,
            "already_active": already_ok,
            "brands_registered": brands_registered,
            "brands_removed": brands_removed,
        }
        if activated or paused or brands_removed:
            logger.info(
                f"Strategy validation: activated={activated}, "
                f"paused={[p['name'] for p in paused]}, "
                f"brands registered={brands_registered}, "
                f"brands removed={brands_removed}"
            )
        return summary

    def _get_asset_inventory(self) -> dict[str, Any]:
        """Query DB for actual assets the system owns."""
        inventory: dict[str, Any] = {
            "has_email": False,
            "platforms": set(),
            "has_domain": False,
            "has_payment": False,
            "api_keys": set(),
        }

        # Check identities table for email, platform accounts
        try:
            rows = self.db.execute(
                "SELECT type, platform, status FROM identities WHERE status = 'active'"
            )
            for r in rows:
                if r["platform"] == "email":
                    inventory["has_email"] = True
                elif r["type"] == "platform_account":
                    inventory["platforms"].add(r["platform"])
                elif r["type"] == "domain":
                    inventory["has_domain"] = True
                elif r["type"] in ("payment_method", "payment"):
                    inventory["has_payment"] = True
                elif r["type"] == "api_key":
                    inventory["api_keys"].add(r["platform"])
        except Exception as e:
            logger.debug(f"Could not query identities: {e}")

        # Also check payment_accounts table if it exists
        try:
            rows = self.db.execute(
                "SELECT 1 FROM payment_accounts WHERE status = 'active' LIMIT 1"
            )
            if rows:
                inventory["has_payment"] = True
        except Exception:
            pass

        return inventory

    def _check_requirements(
        self, requirements: list[str], assets: dict[str, Any]
    ) -> list[str]:
        """Return list of unmet requirements (empty = all met)."""
        missing = []
        for req in requirements:
            if req == "email":
                if not assets["has_email"]:
                    missing.append("email")
            elif req.startswith("platform:"):
                platform = req.split(":", 1)[1]
                if platform not in assets["platforms"]:
                    missing.append(req)
            elif req == "domain":
                if not assets["has_domain"]:
                    missing.append("domain")
            elif req == "payment":
                if not assets["has_payment"]:
                    missing.append("payment")
            elif req.startswith("api_key:"):
                provider = req.split(":", 1)[1]
                if provider not in assets["api_keys"]:
                    missing.append(req)
        return missing

    def _sync_brands(
        self,
        activated: list[str],
        paused: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Sync brand_social_accounts to match active strategy set.

        - Inserts default brand entries for newly activated strategies.
        - Deletes brand entries for strategies that are no longer active
          (paused, pending, or otherwise non-active).

        Returns (registered_count, removed_count).
        """
        registered = 0
        removed = 0

        # 1. Register brands for newly activated strategies
        for name in activated:
            platforms = BRAND_PLATFORMS.get(name, ["twitter"])
            for platform in platforms:
                self.db.execute_insert(
                    "INSERT OR IGNORE INTO brand_social_accounts "
                    "(brand, platform, brand_voice) VALUES (?, ?, ?)",
                    (name, platform, ""),
                )
            registered += 1
            logger.info(f"Registered brand '{name}' (platforms={platforms})")

        # 2. Remove brands for ALL non-active strategies
        try:
            active_rows = self.db.execute(
                "SELECT name FROM strategies WHERE status = 'active'"
            )
            active_names = {r["name"] for r in active_rows}

            brand_rows = self.db.execute(
                "SELECT DISTINCT brand FROM brand_social_accounts"
            )
            for r in brand_rows:
                if r["brand"] not in active_names:
                    self.db.execute(
                        "DELETE FROM brand_social_accounts WHERE brand = ?",
                        (r["brand"],),
                    )
                    removed += 1
                    logger.info(f"Removed brand '{r['brand']}' (strategy not active)")
        except Exception as e:
            logger.warning(f"Brand cleanup failed: {e}")

        return registered, removed

    def demote_active_without_agent(self, registered_agents: set[str]) -> list[str]:
        """Pause active strategies that have no registered agent in memory.

        This catches the case where DB says a strategy is 'active' but
        no Python agent object was registered for it (e.g. from a
        previous session that registered different strategies).
        """
        active = self.db.execute(
            "SELECT id, name FROM strategies WHERE status = 'active'"
        )
        demoted = []
        for row in active:
            if row["name"] not in registered_agents:
                try:
                    self.pause(
                        row["id"],
                        reason="no agent registered in current session",
                    )
                    demoted.append(row["name"])
                except InvalidTransitionError:
                    pass
        if demoted:
            logger.info(f"Demoted strategies without agents: {demoted}")
        return demoted
