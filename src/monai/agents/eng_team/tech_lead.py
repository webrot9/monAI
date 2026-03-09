"""Tech Lead agent — monitors errors, prioritizes bugs, assigns work, reviews fixes."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, TYPE_CHECKING

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

if TYPE_CHECKING:
    from monai.agents.eng_team.engineer import Engineer

logger = logging.getLogger(__name__)


class TechLead(BaseAgent):
    name = "tech_lead"
    description = (
        "Engineering team lead. Monitors error logs and agent failures, "
        "prioritizes bugs by severity and business impact, assigns work "
        "to engineers, and reviews fixes before deployment."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return ["scan_errors", "prioritize", "assign", "review"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        bugs = self.scan_for_bugs()
        return {"bugs_found": len(bugs)}

    def scan_for_bugs(self) -> list[dict[str, Any]]:
        """Scan error logs and agent failures to create bug reports."""
        # Get recent errors from agent_log
        errors = self.db.execute(
            "SELECT agent_name, action, details, result, created_at FROM agent_log "
            "WHERE (action LIKE '%error%' OR action LIKE '%fail%' OR action LIKE '%BLOCKED%') "
            "AND created_at > datetime('now', '-1 day') "
            "ORDER BY created_at DESC LIMIT 20"
        )
        error_list = [dict(e) for e in errors]

        if not error_list:
            return []

        # Get existing open bugs to avoid duplicates
        open_bugs = self.db.execute(
            "SELECT title, description FROM bugs WHERE status NOT IN ('resolved', 'wont_fix')"
        )
        existing = [dict(b) for b in open_bugs]

        # Use LLM to analyze errors and create bug reports
        response = self.think_json(
            "Analyze these error logs and create bug reports for NEW issues only. "
            "Skip errors that already have open bug reports.\n\n"
            f"Errors:\n{json.dumps(error_list, default=str)}\n\n"
            f"Existing bugs (skip duplicates):\n{json.dumps(existing, default=str)}\n\n"
            "Return: {\"bugs\": [{\"title\": str, \"description\": str, "
            "\"severity\": \"low\"|\"medium\"|\"high\"|\"critical\", "
            "\"source_agent\": str, \"root_cause_guess\": str}]}"
        )

        new_bugs = []
        for bug in response.get("bugs", []):
            bug_id = self.db.execute_insert(
                "INSERT INTO bugs (title, description, severity, source, source_agent) "
                "VALUES (?, ?, ?, 'error_log', ?)",
                (bug["title"], bug["description"], bug.get("severity", "medium"),
                 bug.get("source_agent", "")),
            )
            new_bugs.append({**bug, "id": bug_id})
            self.log_action("bug_created", bug["title"], f"severity={bug.get('severity')}")

        return new_bugs

    def assign_bugs(self, engineers: list[Engineer]) -> list[dict[str, Any]]:
        """Assign open bugs to available engineers based on priority."""
        open_bugs = self.db.execute(
            "SELECT * FROM bugs WHERE status = 'open' "
            "ORDER BY CASE severity "
            "  WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
            "  WHEN 'medium' THEN 3 WHEN 'low' THEN 4 END, "
            "created_at ASC"
        )

        assignments = []
        for i, bug in enumerate(open_bugs):
            bug = dict(bug)
            # Round-robin assignment to engineers
            engineer = engineers[i % len(engineers)]

            self.db.execute(
                "UPDATE bugs SET assigned_to = ?, status = 'assigned' WHERE id = ?",
                (engineer.name, bug["id"]),
            )
            assignments.append({
                "bug_id": bug["id"],
                "title": bug["title"],
                "assigned_to": engineer.name,
            })
            self.log_action("bug_assigned", bug["title"], f"→ {engineer.name}")

        return assignments

    def review_fixes(self) -> list[dict[str, Any]]:
        """Review fixes submitted by engineers."""
        fixes = self.db.execute(
            "SELECT * FROM bugs WHERE status = 'fix_ready'"
        )

        reviews = []
        for fix in fixes:
            fix = dict(fix)
            # Use LLM to review the fix
            review = self.think_json(
                "Review this bug fix. Check:\n"
                "1. Does the fix address the root cause?\n"
                "2. Are tests included and do they pass?\n"
                "3. Is the fix minimal (doesn't change unrelated code)?\n"
                "4. Could the fix introduce new bugs?\n\n"
                f"Bug: {fix['title']}\n"
                f"Description: {fix['description']}\n"
                f"Fix: {fix.get('fix_description', 'N/A')}\n"
                f"Files changed: {fix.get('fix_files', 'N/A')}\n"
                f"Test results: {fix.get('test_results', 'N/A')}\n\n"
                "Return: {\"approved\": bool, \"notes\": str, \"concerns\": [str]}"
            )

            approved = review.get("approved", False)
            self.db.execute(
                "UPDATE bugs SET review_status = ?, review_notes = ?, "
                "status = ?, resolved_at = ? WHERE id = ?",
                (
                    "approved" if approved else "needs_changes",
                    review.get("notes", ""),
                    "resolved" if approved else "assigned",
                    datetime.now().isoformat() if approved else None,
                    fix["id"],
                ),
            )

            reviews.append({
                "bug_id": fix["id"],
                "title": fix["title"],
                "approved": approved,
                "notes": review.get("notes", ""),
            })
            self.log_action(
                "fix_reviewed",
                fix["title"],
                f"{'APPROVED' if approved else 'NEEDS CHANGES'}: {review.get('notes', '')[:200]}"
            )

        return reviews
