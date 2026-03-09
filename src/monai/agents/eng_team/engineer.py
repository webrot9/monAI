"""Engineer agent — receives bug assignments, writes fixes and tests."""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)


class Engineer(BaseAgent):
    description = (
        "Software engineer agent. Receives bug assignments from TechLead, "
        "analyzes root causes, writes fixes with tests, and submits for review."
    )

    def __init__(self, config: Config, db: Database, llm: LLM, name: str = "engineer_1"):
        self.name = name  # Set before super().__init__ so logger uses correct name
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return ["check_assignments", "fix_bugs", "submit_fixes"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        fixes = self.work_on_assigned_bugs()
        return {"fixes_submitted": len(fixes)}

    def work_on_assigned_bugs(self) -> list[dict[str, Any]]:
        """Work on all bugs assigned to this engineer."""
        assigned = self.db.execute(
            "SELECT * FROM bugs WHERE assigned_to = ? AND status IN ('assigned', 'in_progress') "
            "ORDER BY CASE severity "
            "  WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
            "  WHEN 'medium' THEN 3 WHEN 'low' THEN 4 END "
            "LIMIT 3",
            (self.name,),
        )

        fixes = []
        for bug in assigned:
            bug = dict(bug)
            fix = self._fix_bug(bug)
            fixes.append(fix)

        return fixes

    def _fix_bug(self, bug: dict[str, Any]) -> dict[str, Any]:
        """Analyze and fix a single bug."""
        self.db.execute(
            "UPDATE bugs SET status = 'in_progress' WHERE id = ?",
            (bug["id"],),
        )
        self.log_action("fixing_bug", bug["title"], f"severity={bug['severity']}")

        # Step 1: Analyze the bug and propose a fix
        analysis = self.think_json(
            "Analyze this bug and propose a fix.\n\n"
            f"Title: {bug['title']}\n"
            f"Description: {bug['description']}\n"
            f"Source agent: {bug.get('source_agent', 'unknown')}\n\n"
            "Think about:\n"
            "1. What's the root cause?\n"
            "2. What files likely need changes?\n"
            "3. What's the minimal fix?\n"
            "4. What tests should verify the fix?\n\n"
            "Return: {\"root_cause\": str, \"fix_approach\": str, "
            "\"files_to_change\": [str], \"test_plan\": str}"
        )

        # Step 2: Attempt to write the fix using the Coder agent
        fix_spec = (
            f"Fix for bug: {bug['title']}\n"
            f"Root cause: {analysis.get('root_cause', 'unknown')}\n"
            f"Fix approach: {analysis.get('fix_approach', '')}\n"
            f"Test plan: {analysis.get('test_plan', '')}"
        )

        try:
            code_result = self.coder.generate_module(fix_spec)
            test_passed = code_result.get("status") == "success"
        except Exception as e:
            logger.error(f"Coder failed for bug {bug['id']}: {e}")
            code_result = {"status": "error", "error": str(e)}
            test_passed = False

        # Step 3: Update bug with fix details
        self.db.execute(
            "UPDATE bugs SET status = ?, fix_description = ?, "
            "fix_files = ?, test_results = ? WHERE id = ?",
            (
                "fix_ready" if test_passed else "assigned",
                analysis.get("fix_approach", ""),
                json.dumps(analysis.get("files_to_change", [])),
                json.dumps(code_result, default=str)[:2000],
                bug["id"],
            ),
        )

        result = {
            "bug_id": bug["id"],
            "title": bug["title"],
            "fix_submitted": test_passed,
            "analysis": analysis,
        }

        if test_passed:
            self.log_action("fix_submitted", bug["title"], "Tests passed, ready for review")
            self.share_knowledge(
                category="fix",
                topic=f"bug_fix_{bug['id']}",
                content=f"Fixed: {bug['title']}. Approach: {analysis.get('fix_approach', '')}",
                tags=["bugfix", bug.get("source_agent", "")],
            )
        else:
            self.log_action("fix_failed", bug["title"], "Tests did not pass")
            self.learn(
                category="mistake",
                situation=f"Failed to fix bug: {bug['title']}",
                lesson=f"Fix approach didn't work: {analysis.get('fix_approach', '')}",
                rule="Try a different approach next cycle",
            )

        return result
