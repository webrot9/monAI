"""Engineering Team — self-healing system that monitors, fixes, and improves monAI.

Architecture:
    TechLead — monitors error logs, prioritizes bugs, assigns work, reviews fixes
    Engineer (x2-3) — receives assignments, writes fixes + tests, submits for review

The team works off a shared bug backlog. The orchestrator feeds failures from
production cycles. Engineers fix, test, and the TechLead reviews before deploy.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.agents.eng_team.tech_lead import TechLead
from monai.agents.eng_team.engineer import Engineer
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

ENG_TEAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS bugs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',  -- low, medium, high, critical
    source TEXT NOT NULL,                     -- error_log, test_failure, agent_report, self_detected
    source_agent TEXT,                        -- which agent reported it
    assigned_to TEXT,                         -- engineer name
    status TEXT NOT NULL DEFAULT 'open',      -- open, assigned, in_progress, fix_ready, reviewing, resolved, wont_fix
    fix_description TEXT,
    fix_files TEXT,                           -- JSON list of files changed
    test_results TEXT,                        -- JSON test output
    review_status TEXT,                       -- approved, rejected, needs_changes
    review_notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);
"""


class EngineeringTeam:
    """Coordinates the TechLead and Engineers for autonomous bug fixing."""

    def __init__(self, config: Config, db: Database, llm: LLM,
                 num_engineers: int = 2):
        self.config = config
        self.db = db
        self.llm = llm

        # Initialize schema
        with db.connect() as conn:
            conn.executescript(ENG_TEAM_SCHEMA)

        self.tech_lead = TechLead(config, db, llm)
        self.engineers = [
            Engineer(config, db, llm, name=f"engineer_{i+1}")
            for i in range(num_engineers)
        ]

    def run(self) -> dict[str, Any]:
        """Run one engineering cycle: triage → assign → fix → review."""
        result: dict[str, Any] = {}

        # Step 1: TechLead scans for new bugs from error logs
        new_bugs = self.tech_lead.scan_for_bugs()
        result["bugs_found"] = len(new_bugs)

        # Step 2: TechLead prioritizes and assigns bugs to engineers
        assignments = self.tech_lead.assign_bugs(self.engineers)
        result["bugs_assigned"] = len(assignments)

        # Step 3: Engineers work on their assigned bugs
        fixes = []
        for engineer in self.engineers:
            engineer_fixes = engineer.work_on_assigned_bugs()
            fixes.extend(engineer_fixes)
        result["fixes_submitted"] = len(fixes)

        # Step 4: TechLead reviews submitted fixes
        reviews = self.tech_lead.review_fixes()
        result["fixes_reviewed"] = len(reviews)
        result["fixes_approved"] = sum(1 for r in reviews if r["approved"])

        # Step 5: Report metrics
        result["backlog"] = self._get_backlog_stats()

        logger.info(f"Engineering cycle: {json.dumps(result)}")
        return result

    def report_bug(self, title: str, description: str,
                   severity: str = "medium", source: str = "agent_report",
                   source_agent: str = "") -> int:
        """External interface for other agents to report bugs."""
        return self.db.execute_insert(
            "INSERT INTO bugs (title, description, severity, source, source_agent) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, description, severity, source, source_agent),
        )

    def _get_backlog_stats(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT status, COUNT(*) as count FROM bugs GROUP BY status"
        )
        return {r["status"]: r["count"] for r in rows}
