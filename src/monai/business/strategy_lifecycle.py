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
