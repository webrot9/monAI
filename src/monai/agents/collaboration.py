"""Agent collaboration hub — structured help requests between agents.

Agents can request help from other agents for specialized tasks:
- Marketing design from a creative agent
- Legal review from the legal advisor
- Code from the coder agent
- Research from a research agent
- Financial analysis from the commercialista

The hub routes requests, tracks fulfillment, and ensures quality.
All collaborations are logged for the creator's audit trail.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable

from monai.config import Config
from monai.db.database import Database

logger = logging.getLogger(__name__)

COLLABORATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS help_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requesting_agent TEXT NOT NULL,
    target_agent TEXT,                -- null = anyone who can help
    skill_needed TEXT NOT NULL,       -- legal, marketing, design, code, research, finance
    task_description TEXT NOT NULL,
    context TEXT,                     -- JSON context for the helper
    priority INTEGER NOT NULL DEFAULT 5,  -- 1=urgent, 10=low
    status TEXT NOT NULL DEFAULT 'open',  -- open, claimed, in_progress, completed, failed
    claimed_by TEXT,                  -- agent that took the request
    result TEXT,                      -- the deliverable
    quality_score REAL,              -- 0-1, rated by requester
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    claimed_at TIMESTAMP,
    completed_at TIMESTAMP
);
"""

# Skills and which agent types can fulfill them
SKILL_REGISTRY = {
    "legal": {
        "description": "Legal compliance review, jurisdiction research, ToS analysis",
        "auto_spawn": True,  # Always spawn a legal advisor
    },
    "marketing": {
        "description": "Marketing copy, ad campaigns, outreach strategy, SEO",
        "auto_spawn": False,
    },
    "design": {
        "description": "Visual design, logos, UI mockups, branding",
        "auto_spawn": False,
    },
    "code": {
        "description": "Code generation, debugging, testing, deployment",
        "auto_spawn": False,
    },
    "research": {
        "description": "Market research, competitor analysis, opportunity discovery",
        "auto_spawn": False,
    },
    "finance": {
        "description": "Financial analysis, cost projections, ROI calculations",
        "auto_spawn": False,
    },
    "content": {
        "description": "Article writing, copywriting, translations, proofreading",
        "auto_spawn": False,
    },
    "devops": {
        "description": "Server setup, domain config, deployment, monitoring",
        "auto_spawn": False,
    },
}


class CollaborationHub:
    """Routes help requests between agents and tracks fulfillment."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

        with db.connect() as conn:
            conn.executescript(COLLABORATION_SCHEMA)

    # ── Request Help ──────────────────────────────────────────

    def request_help(self, requesting_agent: str, skill_needed: str,
                     task_description: str, context: dict | None = None,
                     target_agent: str | None = None,
                     priority: int = 5) -> int:
        """Submit a help request.

        Args:
            requesting_agent: Who needs help
            skill_needed: Type of skill (legal, marketing, design, etc.)
            task_description: What needs to be done
            context: Additional context as dict
            target_agent: Specific agent to ask (None = anyone)
            priority: 1=urgent, 10=low

        Returns:
            Request ID
        """
        if skill_needed not in SKILL_REGISTRY:
            logger.warning(f"Unknown skill '{skill_needed}' — accepting anyway")

        request_id = self.db.execute_insert(
            "INSERT INTO help_requests "
            "(requesting_agent, target_agent, skill_needed, task_description, "
            "context, priority, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open')",
            (
                requesting_agent,
                target_agent,
                skill_needed,
                task_description,
                json.dumps(context or {}),
                priority,
            ),
        )

        logger.info(
            f"Help request #{request_id}: {requesting_agent} needs {skill_needed} "
            f"(priority={priority})"
        )
        return request_id

    # ── Claim & Fulfill ───────────────────────────────────────

    def claim_request(self, request_id: int, agent_name: str) -> bool:
        """Claim an open help request."""
        rows = self.db.execute(
            "SELECT status FROM help_requests WHERE id = ?", (request_id,)
        )
        if not rows or rows[0]["status"] != "open":
            return False

        self.db.execute(
            "UPDATE help_requests SET status = 'claimed', claimed_by = ?, "
            "claimed_at = ? WHERE id = ?",
            (agent_name, datetime.now().isoformat(), request_id),
        )
        return True

    def start_work(self, request_id: int) -> None:
        """Mark a request as in progress."""
        self.db.execute(
            "UPDATE help_requests SET status = 'in_progress' WHERE id = ?",
            (request_id,),
        )

    def complete_request(self, request_id: int, result: str) -> None:
        """Mark a request as completed with a result."""
        self.db.execute(
            "UPDATE help_requests SET status = 'completed', result = ?, "
            "completed_at = ? WHERE id = ?",
            (result, datetime.now().isoformat(), request_id),
        )

    def fail_request(self, request_id: int, reason: str) -> None:
        """Mark a request as failed."""
        self.db.execute(
            "UPDATE help_requests SET status = 'failed', result = ?, "
            "completed_at = ? WHERE id = ?",
            (f"FAILED: {reason}", datetime.now().isoformat(), request_id),
        )

    def rate_result(self, request_id: int, quality_score: float) -> None:
        """Rate the quality of a completed request (0.0 to 1.0)."""
        score = max(0.0, min(1.0, quality_score))
        self.db.execute(
            "UPDATE help_requests SET quality_score = ? WHERE id = ?",
            (score, request_id),
        )

    # ── Query Requests ────────────────────────────────────────

    def get_open_requests(self, skill: str = "",
                          target_agent: str = "") -> list[dict[str, Any]]:
        """Get open help requests, optionally filtered."""
        query = "SELECT * FROM help_requests WHERE status = 'open'"
        params: list = []

        if skill:
            query += " AND skill_needed = ?"
            params.append(skill)
        if target_agent:
            query += " AND (target_agent = ? OR target_agent IS NULL)"
            params.append(target_agent)

        query += " ORDER BY priority ASC, created_at ASC"
        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        """Get a specific help request."""
        rows = self.db.execute(
            "SELECT * FROM help_requests WHERE id = ?", (request_id,)
        )
        return dict(rows[0]) if rows else None

    def get_agent_requests(self, agent_name: str,
                           status: str = "") -> list[dict[str, Any]]:
        """Get all requests made by an agent."""
        if status:
            rows = self.db.execute(
                "SELECT * FROM help_requests "
                "WHERE requesting_agent = ? AND status = ? ORDER BY created_at DESC",
                (agent_name, status),
            )
        else:
            rows = self.db.execute(
                "SELECT * FROM help_requests "
                "WHERE requesting_agent = ? ORDER BY created_at DESC",
                (agent_name,),
            )
        return [dict(r) for r in rows]

    def get_agent_claims(self, agent_name: str) -> list[dict[str, Any]]:
        """Get all requests claimed by an agent."""
        rows = self.db.execute(
            "SELECT * FROM help_requests "
            "WHERE claimed_by = ? AND status IN ('claimed', 'in_progress') "
            "ORDER BY priority ASC",
            (agent_name,),
        )
        return [dict(r) for r in rows]

    def get_pending_legal_reviews(self) -> list[dict[str, Any]]:
        """Get all open legal review requests (convenience method)."""
        return self.get_open_requests(skill="legal")

    # ── Statistics ────────────────────────────────────────────

    def get_collaboration_stats(self) -> dict[str, Any]:
        """Get overall collaboration statistics."""
        rows = self.db.execute(
            "SELECT status, COUNT(*) as count FROM help_requests GROUP BY status"
        )
        by_status = {r["status"]: r["count"] for r in rows}

        skill_rows = self.db.execute(
            "SELECT skill_needed, COUNT(*) as count "
            "FROM help_requests GROUP BY skill_needed ORDER BY count DESC"
        )
        by_skill = {r["skill_needed"]: r["count"] for r in skill_rows}

        quality_rows = self.db.execute(
            "SELECT AVG(quality_score) as avg_quality "
            "FROM help_requests WHERE quality_score IS NOT NULL"
        )
        avg_quality = quality_rows[0]["avg_quality"] if quality_rows else None

        return {
            "by_status": by_status,
            "by_skill": by_skill,
            "total_requests": sum(by_status.values()),
            "avg_quality": avg_quality,
        }

    def needs_legal_skill(self, skill: str) -> bool:
        """Check if a skill type should auto-spawn a legal advisor."""
        return SKILL_REGISTRY.get(skill, {}).get("auto_spawn", False)
