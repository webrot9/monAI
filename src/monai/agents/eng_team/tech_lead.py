"""Tech Lead agent — monitors errors, prioritizes bugs, assigns work, reviews fixes.

Real autonomous capabilities:
- Parses actual agent_log DB rows for error patterns (not just LLM guessing)
- Categorizes errors by type using real log analysis
- Tracks engineering metrics (MTTR, fix rate, bug velocity) from DB
- Workload-aware assignment based on real engineer queue depth
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Any, TYPE_CHECKING

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

if TYPE_CHECKING:
    from monai.agents.eng_team.engineer import Engineer

logger = logging.getLogger(__name__)

# ── Error categorization patterns (real parsing, not LLM) ──────────
ERROR_CATEGORIES = {
    "import_error": re.compile(r"(ImportError|ModuleNotFoundError|No module named)", re.I),
    "type_error": re.compile(r"(TypeError|AttributeError|'NoneType')", re.I),
    "connection_error": re.compile(r"(ConnectionError|TimeoutError|ECONNREFUSED|ETIMEDOUT|HTTPError)", re.I),
    "auth_error": re.compile(r"(401|403|Unauthorized|Forbidden|auth.*fail|login.*fail)", re.I),
    "budget_error": re.compile(r"(Budget.*exceeded|BudgetExceeded|cost.*limit)", re.I),
    "db_error": re.compile(r"(OperationalError|IntegrityError|sqlite|database.*locked)", re.I),
    "browser_error": re.compile(r"(playwright|browser.*crash|navigation.*fail|selector.*not.*found)", re.I),
    "llm_error": re.compile(r"(RateLimitError|APIError|model.*not.*found|context.*length)", re.I),
    "file_error": re.compile(r"(FileNotFoundError|PermissionError|IsADirectory|No such file)", re.I),
    "validation_error": re.compile(r"(ValidationError|pydantic|JSON.*decode|invalid.*json)", re.I),
}


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
        return ["scan_errors", "categorize", "compute_metrics", "prioritize", "assign", "review"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        bugs = self.scan_for_bugs()
        metrics = self.compute_engineering_metrics()
        return {"bugs_found": len(bugs), "metrics": metrics}

    # ── Real log parsing and error categorization ──────────────────

    def _parse_error_logs(self, hours: int = 24) -> list[dict[str, Any]]:
        """Parse real agent_log rows and categorize errors programmatically."""
        errors = self.db.execute(
            "SELECT id, agent_name, action, details, result, created_at FROM agent_log "
            "WHERE (action LIKE '%error%' OR action LIKE '%fail%' OR action LIKE '%BLOCKED%' "
            "  OR result LIKE '%Error%' OR result LIKE '%Traceback%' OR result LIKE '%exception%') "
            "AND created_at > datetime('now', ? || ' hours') "
            "ORDER BY created_at DESC LIMIT 50",
            (str(-hours),),
        )
        parsed = []
        for row in errors:
            row = dict(row)
            text = f"{row.get('action', '')} {row.get('details', '')} {row.get('result', '')}"
            # Real pattern-based categorization
            category = "unknown"
            for cat_name, pattern in ERROR_CATEGORIES.items():
                if pattern.search(text):
                    category = cat_name
                    break
            row["error_category"] = category
            parsed.append(row)
        return parsed

    def _compute_error_frequency(self, parsed_errors: list[dict]) -> dict[str, list[dict]]:
        """Group errors by category and agent, detecting repeated patterns."""
        by_category: dict[str, list[dict]] = {}
        for err in parsed_errors:
            cat = err["error_category"]
            by_category.setdefault(cat, []).append(err)
        return by_category

    def _auto_severity_from_category(self, category: str, count: int) -> str:
        """Determine severity from error category and frequency — no LLM needed."""
        critical_categories = {"auth_error", "budget_error", "db_error"}
        high_categories = {"connection_error", "browser_error", "llm_error"}

        if category in critical_categories or count >= 10:
            return "critical"
        if category in high_categories or count >= 5:
            return "high"
        if count >= 3:
            return "medium"
        return "low"

    # ── Real engineering metrics from DB ───────────────────────────

    def compute_engineering_metrics(self) -> dict[str, Any]:
        """Compute real engineering metrics from the bugs table."""
        # Mean time to resolve (MTTR) — hours
        mttr_rows = self.db.execute(
            "SELECT AVG((julianday(resolved_at) - julianday(created_at)) * 24) as mttr_hours "
            "FROM bugs WHERE status = 'resolved' AND resolved_at IS NOT NULL "
            "AND created_at > datetime('now', '-7 days')"
        )
        mttr = mttr_rows[0]["mttr_hours"] if mttr_rows and mttr_rows[0]["mttr_hours"] else 0.0

        # Fix rate (approved / total reviewed) last 7 days
        review_rows = self.db.execute(
            "SELECT COUNT(*) as total, "
            "  SUM(CASE WHEN review_status = 'approved' THEN 1 ELSE 0 END) as approved "
            "FROM bugs WHERE review_status IS NOT NULL "
            "AND created_at > datetime('now', '-7 days')"
        )
        total_reviewed = review_rows[0]["total"] if review_rows else 0
        approved = review_rows[0]["approved"] if review_rows else 0
        fix_rate = (approved / total_reviewed) if total_reviewed > 0 else 0.0

        # Bug velocity — new bugs per day (last 7 days)
        velocity_rows = self.db.execute(
            "SELECT COUNT(*) as cnt FROM bugs "
            "WHERE created_at > datetime('now', '-7 days')"
        )
        weekly_bugs = velocity_rows[0]["cnt"] if velocity_rows else 0
        daily_velocity = weekly_bugs / 7.0

        # Backlog aging — oldest unresolved bug
        oldest_rows = self.db.execute(
            "SELECT MIN(created_at) as oldest FROM bugs "
            "WHERE status NOT IN ('resolved', 'wont_fix')"
        )
        oldest = oldest_rows[0]["oldest"] if oldest_rows and oldest_rows[0]["oldest"] else None

        # Per-engineer workload
        workload_rows = self.db.execute(
            "SELECT assigned_to, COUNT(*) as cnt FROM bugs "
            "WHERE status IN ('assigned', 'in_progress') AND assigned_to IS NOT NULL "
            "GROUP BY assigned_to"
        )
        workload = {r["assigned_to"]: r["cnt"] for r in workload_rows}

        metrics = {
            "mttr_hours": round(mttr, 2),
            "fix_rate": round(fix_rate, 3),
            "daily_bug_velocity": round(daily_velocity, 2),
            "weekly_bugs": weekly_bugs,
            "total_reviewed_7d": total_reviewed,
            "total_approved_7d": approved,
            "oldest_unresolved": oldest,
            "engineer_workload": workload,
        }
        self.log_action("eng_metrics", json.dumps(metrics, default=str))
        return metrics

    # ── Bug scanning with real parsing + LLM dedup ────────────────

    def scan_for_bugs(self) -> list[dict[str, Any]]:
        """Scan error logs and agent failures to create bug reports.

        Uses real log parsing for categorization + LLM only for dedup/description.
        """
        # Step 1: Real log parsing — categorize errors programmatically
        parsed_errors = self._parse_error_logs(hours=24)
        if not parsed_errors:
            return []

        error_groups = self._compute_error_frequency(parsed_errors)

        # Step 2: Get existing open bugs to avoid duplicates
        open_bugs = self.db.execute(
            "SELECT title, description FROM bugs WHERE status NOT IN ('resolved', 'wont_fix')"
        )
        existing = [dict(b) for b in open_bugs]

        # Step 3: Build candidate bugs from real error groups
        candidates = []
        for category, errors_in_cat in error_groups.items():
            # Sub-group by agent to detect per-agent issues
            by_agent: dict[str, list[dict]] = {}
            for e in errors_in_cat:
                agent = e.get("agent_name", "unknown")
                by_agent.setdefault(agent, []).append(e)

            for agent, agent_errors in by_agent.items():
                severity = self._auto_severity_from_category(category, len(agent_errors))
                sample_detail = (agent_errors[0].get("details") or "")[:300]
                sample_result = (agent_errors[0].get("result") or "")[:300]
                candidates.append({
                    "category": category,
                    "agent": agent,
                    "count": len(agent_errors),
                    "severity": severity,
                    "sample_detail": sample_detail,
                    "sample_result": sample_result,
                    "first_seen": agent_errors[-1].get("created_at", ""),
                    "last_seen": agent_errors[0].get("created_at", ""),
                })

        if not candidates:
            return []

        # Step 4: Use LLM ONLY for dedup against existing bugs and writing descriptions
        response = self.think_json(
            "Create bug reports for NEW issues only. I've already categorized errors from logs.\n\n"
            f"Error groups (real data):\n{json.dumps(candidates, default=str)}\n\n"
            f"Existing open bugs (skip duplicates):\n{json.dumps(existing, default=str)}\n\n"
            "For each NEW group, write a clear title and description. "
            "Keep the severity I assigned (based on error type and frequency).\n"
            "Return: {\"bugs\": [{\"title\": str, \"description\": str, "
            "\"severity\": str, \"source_agent\": str, \"error_category\": str}]}"
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
            self.log_action("bug_created", bug["title"],
                            f"severity={bug.get('severity')} cat={bug.get('error_category', '?')}")

        return new_bugs

    def assign_bugs(self, engineers: list[Engineer]) -> list[dict[str, Any]]:
        """Assign open bugs to engineers using workload-aware scheduling.

        Instead of simple round-robin, checks each engineer's real queue depth
        from the DB and assigns to the least-loaded engineer.
        """
        open_bugs = self.db.execute(
            "SELECT * FROM bugs WHERE status = 'open' "
            "ORDER BY CASE severity "
            "  WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
            "  WHEN 'medium' THEN 3 WHEN 'low' THEN 4 END, "
            "created_at ASC"
        )

        # Real workload query — count active bugs per engineer
        workload_rows = self.db.execute(
            "SELECT assigned_to, COUNT(*) as active_count FROM bugs "
            "WHERE status IN ('assigned', 'in_progress') AND assigned_to IS NOT NULL "
            "GROUP BY assigned_to"
        )
        workload = {r["assigned_to"]: r["active_count"] for r in workload_rows}

        assignments = []
        for bug in open_bugs:
            bug = dict(bug)
            # Pick the engineer with the fewest active bugs
            engineer = min(
                engineers,
                key=lambda e: workload.get(e.name, 0),
            )
            workload[engineer.name] = workload.get(engineer.name, 0) + 1

            self.db.execute(
                "UPDATE bugs SET assigned_to = ?, status = 'assigned' WHERE id = ?",
                (engineer.name, bug["id"]),
            )
            assignments.append({
                "bug_id": bug["id"],
                "title": bug["title"],
                "assigned_to": engineer.name,
                "engineer_queue_depth": workload[engineer.name],
            })
            self.log_action("bug_assigned", bug["title"],
                            f"→ {engineer.name} (queue={workload[engineer.name]})")

        return assignments

    def review_fixes(self) -> list[dict[str, Any]]:
        """Review fixes submitted by engineers.

        Performs real review:
        1. Parses test_results JSON to check actual pass/fail status
        2. Validates fix_files list is non-empty
        3. Uses LLM only for semantic review of the approach
        """
        fixes = self.db.execute(
            "SELECT * FROM bugs WHERE status = 'fix_ready'"
        )

        reviews = []
        for fix in fixes:
            fix = dict(fix)

            # ── Real validation checks (no LLM needed) ────────────
            auto_concerns = []

            # Check 1: Were any files actually changed?
            fix_files_raw = fix.get("fix_files", "[]")
            try:
                fix_files = json.loads(fix_files_raw) if fix_files_raw else []
            except (json.JSONDecodeError, TypeError):
                fix_files = []
            if not fix_files:
                auto_concerns.append("No files listed in fix — may be incomplete")

            # Check 2: Parse real test results
            test_results_raw = fix.get("test_results", "{}")
            try:
                test_results = json.loads(test_results_raw) if test_results_raw else {}
            except (json.JSONDecodeError, TypeError):
                test_results = {}
            test_status = test_results.get("status", "unknown")
            if test_status != "success":
                auto_concerns.append(f"Test status is '{test_status}', not 'success'")
            if test_results.get("error"):
                auto_concerns.append(f"Test error: {str(test_results['error'])[:200]}")

            # Check 3: Fix description present
            if not fix.get("fix_description"):
                auto_concerns.append("No fix description provided")

            # ── LLM semantic review (on top of real checks) ────────
            review = self.think_json(
                "Review this bug fix. I've already done automated checks.\n\n"
                f"Bug: {fix['title']}\n"
                f"Description: {fix['description']}\n"
                f"Fix approach: {fix.get('fix_description', 'N/A')}\n"
                f"Files changed: {fix_files}\n"
                f"Test status: {test_status}\n"
                f"Automated concerns: {auto_concerns}\n\n"
                "Check: does the fix address root cause? Could it introduce regressions?\n"
                "Return: {\"approved\": bool, \"notes\": str, \"concerns\": [str]}"
            )

            # Merge auto concerns with LLM concerns
            all_concerns = auto_concerns + review.get("concerns", [])

            # Auto-reject if critical automated checks fail
            approved = review.get("approved", False)
            if test_status not in ("success", "unknown") or not fix_files:
                approved = False

            self.db.execute(
                "UPDATE bugs SET review_status = ?, review_notes = ?, "
                "status = ?, resolved_at = ? WHERE id = ?",
                (
                    "approved" if approved else "needs_changes",
                    json.dumps({"notes": review.get("notes", ""), "concerns": all_concerns},
                               default=str)[:2000],
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
                "concerns": all_concerns,
            })
            self.log_action(
                "fix_reviewed",
                fix["title"],
                f"{'APPROVED' if approved else 'NEEDS CHANGES'}: "
                f"concerns={len(all_concerns)} {review.get('notes', '')[:150]}"
            )

        return reviews
