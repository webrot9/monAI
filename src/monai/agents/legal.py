"""Legal Advisor agent — spawned per-activity to ensure legal compliance.

Every activity the agents engage with spawns a Legal Advisor that:
1. Researches all legal aspects of the activity
2. Identifies jurisdiction-specific requirements
3. Provides step-by-step legal guidance to the operating agent
4. Monitors ongoing compliance during execution
5. Flags risks and blocks illegal actions

The Legal Advisor is NOT optional. It's spawned automatically for every
new strategy, platform registration, client engagement, or financial operation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

LEGAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS legal_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_name TEXT NOT NULL,
    activity_type TEXT NOT NULL,    -- strategy, registration, client_work, financial, marketing
    requesting_agent TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, approved, blocked, needs_review
    jurisdiction TEXT NOT NULL DEFAULT 'EU',
    risk_level TEXT NOT NULL DEFAULT 'unknown',  -- low, medium, high, critical
    assessment TEXT NOT NULL,       -- Full legal assessment JSON
    requirements TEXT,              -- Legal requirements the agent must follow
    blockers TEXT,                  -- Things that would make this illegal
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS legal_guidance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER REFERENCES legal_assessments(id),
    agent_name TEXT NOT NULL,
    guidance_type TEXT NOT NULL,    -- requirement, warning, blocker, tip
    content TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Legal domains the advisor checks for each activity type
LEGAL_CHECKS = {
    "strategy": [
        "business_registration",
        "tax_obligations",
        "consumer_protection",
        "intellectual_property",
        "data_protection_gdpr",
        "advertising_standards",
        "terms_of_service",
    ],
    "registration": [
        "terms_of_service",
        "identity_requirements",
        "age_verification",
        "data_protection_gdpr",
        "platform_rules",
    ],
    "client_work": [
        "contract_law",
        "intellectual_property",
        "liability",
        "data_protection_gdpr",
        "consumer_protection",
        "tax_invoicing",
        "payment_processing",
    ],
    "financial": [
        "tax_obligations",
        "money_transmission",
        "anti_money_laundering",
        "cryptocurrency_regulation",
        "payment_processing",
        "bookkeeping_requirements",
    ],
    "marketing": [
        "advertising_standards",
        "spam_laws",
        "data_protection_gdpr",
        "consumer_protection",
        "endorsement_disclosure",
        "email_marketing_consent",
    ],
    "content": [
        "copyright",
        "defamation",
        "trademark",
        "advertising_standards",
        "consumer_protection",
    ],
}


class LegalAdvisor(BaseAgent):
    """Per-activity legal advisor — ensures everything monAI does is legal."""

    name = "legal_advisor"
    description = (
        "Legal compliance agent that researches and enforces legal requirements "
        "for every activity. Spawned automatically per-activity. "
        "Blocks illegal actions, provides guidance, monitors compliance."
    )

    def __init__(self, config: Config, db: Database, llm: LLM,
                 activity_name: str = "", activity_type: str = "strategy"):
        super().__init__(config, db, llm)
        self.activity_name = activity_name
        self.activity_type = activity_type
        self.name = f"legal_advisor_{activity_name}"

        with db.connect() as conn:
            conn.executescript(LEGAL_SCHEMA)

    def plan(self) -> list[str]:
        """Plan the legal review for this activity."""
        checks = LEGAL_CHECKS.get(self.activity_type, LEGAL_CHECKS["strategy"])
        return [f"review_{check}" for check in checks]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Run a full legal assessment for the activity."""
        requesting_agent = kwargs.get("requesting_agent", "orchestrator")
        activity_description = kwargs.get("description", self.activity_name)
        jurisdiction = kwargs.get("jurisdiction", "EU")

        self.log_action("legal_review_start",
                        f"Reviewing: {self.activity_name} ({self.activity_type})")

        # Get relevant legal checks
        checks = LEGAL_CHECKS.get(self.activity_type, LEGAL_CHECKS["strategy"])

        # Ask LLM for comprehensive legal assessment
        assessment = self._assess_legality(
            activity_description, self.activity_type, checks, jurisdiction
        )

        # Store assessment
        assessment_id = self.db.execute_insert(
            "INSERT INTO legal_assessments "
            "(activity_name, activity_type, requesting_agent, status, jurisdiction, "
            "risk_level, assessment, requirements, blockers) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.activity_name,
                self.activity_type,
                requesting_agent,
                assessment["status"],
                jurisdiction,
                assessment["risk_level"],
                json.dumps(assessment),
                json.dumps(assessment.get("requirements", [])),
                json.dumps(assessment.get("blockers", [])),
            ),
        )

        # Generate guidance for the requesting agent
        guidance_items = self._generate_guidance(assessment, assessment_id, requesting_agent)

        result = {
            "assessment_id": assessment_id,
            "activity": self.activity_name,
            "status": assessment["status"],
            "risk_level": assessment["risk_level"],
            "requirements_count": len(assessment.get("requirements", [])),
            "blockers_count": len(assessment.get("blockers", [])),
            "guidance_items": len(guidance_items),
        }

        self.log_action("legal_review_complete", json.dumps(result, default=str))

        # If blocked, alert
        if assessment["status"] == "blocked":
            self.learn(
                "legal", f"Activity '{self.activity_name}' blocked",
                f"Legal blockers: {assessment.get('blockers', [])}",
                rule=f"Do NOT proceed with '{self.activity_name}' — it has legal blockers",
                severity="critical",
            )

        return result

    def _assess_legality(self, description: str, activity_type: str,
                         checks: list[str], jurisdiction: str) -> dict[str, Any]:
        """Use LLM to perform legal assessment."""
        response = self.think_json(
            f"You are a legal compliance advisor. Assess the legality of this activity.\n\n"
            f"Activity: {description}\n"
            f"Type: {activity_type}\n"
            f"Jurisdiction: {jurisdiction}\n"
            f"Legal areas to check: {', '.join(checks)}\n\n"
            f"For EACH legal area, assess:\n"
            f"1. Is this activity legal in {jurisdiction}?\n"
            f"2. What specific requirements must be met?\n"
            f"3. Are there any absolute blockers (things that make this illegal)?\n"
            f"4. What are the risks if requirements are not met?\n\n"
            f"Return JSON:\n"
            f'{{"status": "approved"|"blocked"|"needs_review",\n'
            f' "risk_level": "low"|"medium"|"high"|"critical",\n'
            f' "summary": "one-line summary",\n'
            f' "checks": [{{"area": str, "legal": bool, "notes": str}}],\n'
            f' "requirements": ["specific requirement 1", ...],\n'
            f' "blockers": ["blocker 1", ...] (empty if legal),\n'
            f' "recommendations": ["recommendation 1", ...]}}'
        )

        # Validate and ensure required fields
        if not isinstance(response, dict):
            response = {}

        response.setdefault("status", "needs_review")
        response.setdefault("risk_level", "unknown")
        response.setdefault("summary", "Assessment could not be completed")
        response.setdefault("checks", [])
        response.setdefault("requirements", [])
        response.setdefault("blockers", [])
        response.setdefault("recommendations", [])

        # If there are blockers, status MUST be blocked
        if response["blockers"]:
            response["status"] = "blocked"

        return response

    def _generate_guidance(self, assessment: dict, assessment_id: int,
                           agent_name: str) -> list[dict[str, Any]]:
        """Generate specific guidance items for the operating agent."""
        guidance_items = []

        # Blockers first
        for blocker in assessment.get("blockers", []):
            gid = self.db.execute_insert(
                "INSERT INTO legal_guidance "
                "(assessment_id, agent_name, guidance_type, content) "
                "VALUES (?, ?, 'blocker', ?)",
                (assessment_id, agent_name, blocker),
            )
            guidance_items.append({"id": gid, "type": "blocker", "content": blocker})

        # Requirements
        for req in assessment.get("requirements", []):
            gid = self.db.execute_insert(
                "INSERT INTO legal_guidance "
                "(assessment_id, agent_name, guidance_type, content) "
                "VALUES (?, ?, 'requirement', ?)",
                (assessment_id, agent_name, req),
            )
            guidance_items.append({"id": gid, "type": "requirement", "content": req})

        # Recommendations as tips
        for rec in assessment.get("recommendations", []):
            gid = self.db.execute_insert(
                "INSERT INTO legal_guidance "
                "(assessment_id, agent_name, guidance_type, content) "
                "VALUES (?, ?, 'tip', ?)",
                (assessment_id, agent_name, rec),
            )
            guidance_items.append({"id": gid, "type": "tip", "content": rec})

        return guidance_items

    # ── Query Methods ─────────────────────────────────────────

    def get_assessment(self, activity_name: str) -> dict[str, Any] | None:
        """Get the latest legal assessment for an activity."""
        rows = self.db.execute(
            "SELECT * FROM legal_assessments WHERE activity_name = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (activity_name,),
        )
        if not rows:
            return None
        row = dict(rows[0])
        row["assessment"] = json.loads(row["assessment"])
        row["requirements"] = json.loads(row["requirements"]) if row["requirements"] else []
        row["blockers"] = json.loads(row["blockers"]) if row["blockers"] else []
        return row

    def is_activity_approved(self, activity_name: str) -> bool:
        """Check if an activity has been legally approved."""
        rows = self.db.execute(
            "SELECT status FROM legal_assessments WHERE activity_name = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (activity_name,),
        )
        return rows[0]["status"] == "approved" if rows else False

    def is_activity_blocked(self, activity_name: str) -> bool:
        """Check if an activity has legal blockers."""
        rows = self.db.execute(
            "SELECT status FROM legal_assessments WHERE activity_name = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (activity_name,),
        )
        return rows[0]["status"] == "blocked" if rows else False

    def get_guidance_for_agent(self, agent_name: str,
                               unacknowledged_only: bool = True) -> list[dict[str, Any]]:
        """Get legal guidance items for a specific agent."""
        if unacknowledged_only:
            rows = self.db.execute(
                "SELECT * FROM legal_guidance "
                "WHERE agent_name = ? AND acknowledged = 0 "
                "ORDER BY guidance_type, created_at",
                (agent_name,),
            )
        else:
            rows = self.db.execute(
                "SELECT * FROM legal_guidance WHERE agent_name = ? "
                "ORDER BY guidance_type, created_at",
                (agent_name,),
            )
        return [dict(r) for r in rows]

    def acknowledge_guidance(self, guidance_id: int) -> None:
        """Mark a guidance item as acknowledged by the agent."""
        self.db.execute(
            "UPDATE legal_guidance SET acknowledged = 1 WHERE id = ?",
            (guidance_id,),
        )

    def get_all_assessments(self, status: str = "") -> list[dict[str, Any]]:
        """Get all legal assessments, optionally filtered by status."""
        if status:
            rows = self.db.execute(
                "SELECT id, activity_name, activity_type, requesting_agent, "
                "status, risk_level, jurisdiction, created_at "
                "FROM legal_assessments WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            rows = self.db.execute(
                "SELECT id, activity_name, activity_type, requesting_agent, "
                "status, risk_level, jurisdiction, created_at "
                "FROM legal_assessments ORDER BY created_at DESC",
            )
        return [dict(r) for r in rows]

    def get_blocked_activities(self) -> list[str]:
        """Get names of all currently blocked activities."""
        rows = self.db.execute(
            "SELECT DISTINCT activity_name FROM legal_assessments WHERE status = 'blocked'"
        )
        return [r["activity_name"] for r in rows]


class LegalAdvisorFactory:
    """Creates Legal Advisors for specific activities.

    Used by the orchestrator to spawn a legal advisor per-activity.
    """

    def __init__(self, config: Config, db: Database, llm: LLM):
        self.config = config
        self.db = db
        self.llm = llm

        # Ensure schema exists
        with db.connect() as conn:
            conn.executescript(LEGAL_SCHEMA)

    def create_for_activity(self, activity_name: str,
                            activity_type: str = "strategy") -> LegalAdvisor:
        """Create a Legal Advisor for a specific activity."""
        return LegalAdvisor(
            self.config, self.db, self.llm,
            activity_name=activity_name,
            activity_type=activity_type,
        )

    def assess_activity(self, activity_name: str, activity_type: str,
                        description: str = "",
                        requesting_agent: str = "orchestrator",
                        jurisdiction: str = "EU") -> dict[str, Any]:
        """One-shot: create advisor, run assessment, return result."""
        advisor = self.create_for_activity(activity_name, activity_type)
        return advisor.run(
            requesting_agent=requesting_agent,
            description=description or activity_name,
            jurisdiction=jurisdiction,
        )

    def is_approved(self, activity_name: str) -> bool:
        """Check if an activity is legally approved (without creating a new advisor)."""
        rows = self.db.execute(
            "SELECT status FROM legal_assessments WHERE activity_name = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (activity_name,),
        )
        return rows[0]["status"] == "approved" if rows else False

    def is_blocked(self, activity_name: str) -> bool:
        """Check if an activity has legal blockers."""
        rows = self.db.execute(
            "SELECT status FROM legal_assessments WHERE activity_name = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (activity_name,),
        )
        return rows[0]["status"] == "blocked" if rows else False

    def get_blocked_activities(self) -> list[str]:
        """Get all currently blocked activities."""
        rows = self.db.execute(
            "SELECT DISTINCT activity_name FROM legal_assessments WHERE status = 'blocked'"
        )
        return [r["activity_name"] for r in rows]
