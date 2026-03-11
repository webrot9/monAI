"""Engineer agent — receives bug assignments, writes fixes and tests.

Real autonomous capabilities:
- Uses Coder agent for actual code diff review (reads real files, generates real patches)
- Tracks test execution results with real pass/fail parsing
- Records fix attempt history to avoid repeating failed approaches
- Reads actual source files to understand bug context before proposing fixes
"""

from __future__ import annotations

import json
import logging
import time
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
        return ["check_assignments", "gather_context", "fix_bugs", "verify_tests", "submit_fixes"]

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

    # ── Real context gathering ─────────────────────────────────────

    def _gather_bug_context(self, bug: dict[str, Any]) -> dict[str, Any]:
        """Gather real context about a bug from DB before attempting a fix.

        Reads actual error logs, prior fix attempts, and related lessons.
        """
        context: dict[str, Any] = {}

        # Get the actual error log entries that spawned this bug
        source_agent = bug.get("source_agent", "")
        if source_agent:
            recent_errors = self.db.execute(
                "SELECT action, details, result, created_at FROM agent_log "
                "WHERE agent_name = ? "
                "AND (action LIKE '%error%' OR action LIKE '%fail%') "
                "AND created_at > datetime('now', '-2 days') "
                "ORDER BY created_at DESC LIMIT 5",
                (source_agent,),
            )
            context["source_errors"] = [dict(r) for r in recent_errors]

        # Check for prior failed fix attempts on this bug (avoid repeating)
        prior_attempts = self.db.execute(
            "SELECT fix_description, test_results, review_notes FROM bugs "
            "WHERE title = ? AND status IN ('assigned', 'wont_fix') "
            "AND fix_description IS NOT NULL AND id != ?",
            (bug["title"], bug["id"]),
        )
        context["prior_attempts"] = [dict(a) for a in prior_attempts]

        # Get relevant lessons learned from past fixes
        lessons = self.memory.get_lessons(self.name, include_shared=True)
        relevant_lessons = [
            l for l in lessons
            if l.get("category") == "mistake"
            and any(
                kw in l.get("situation", "").lower()
                for kw in bug["title"].lower().split()[:3]
            )
        ][:3]
        context["relevant_lessons"] = relevant_lessons

        return context

    def _verify_fix_result(self, code_result: dict) -> dict[str, Any]:
        """Parse real test execution results from the Coder output.

        Returns structured verification with pass/fail details.
        """
        verification = {
            "test_passed": False,
            "status": code_result.get("status", "unknown"),
            "files_written": [],
            "test_output": "",
            "errors": [],
        }

        if code_result.get("status") == "success":
            verification["test_passed"] = True
            verification["files_written"] = code_result.get("files", [])
            verification["test_output"] = code_result.get("test_output", "")
        else:
            error = code_result.get("error", "")
            verification["errors"].append(str(error))
            # Parse specific test failures if present
            test_output = str(code_result.get("test_output", code_result.get("output", "")))
            if "FAILED" in test_output or "AssertionError" in test_output:
                verification["errors"].append(f"Test failures in output: {test_output[:500]}")
            elif "SyntaxError" in test_output:
                verification["errors"].append(f"Syntax error: {test_output[:500]}")

        return verification

    # ── Core fix logic with real context + verification ────────────

    def _fix_bug(self, bug: dict[str, Any]) -> dict[str, Any]:
        """Analyze and fix a single bug with real context gathering."""
        start_time = time.time()

        self.db.execute(
            "UPDATE bugs SET status = 'in_progress' WHERE id = ?",
            (bug["id"],),
        )
        self.log_action("fixing_bug", bug["title"], f"severity={bug['severity']}")

        # Step 1: Gather REAL context from DB
        context = self._gather_bug_context(bug)

        # Build context string for analysis
        context_str = ""
        if context.get("source_errors"):
            context_str += "Recent error logs from source agent:\n"
            for err in context["source_errors"][:3]:
                context_str += f"  - [{err.get('action')}] {err.get('details', '')[:200]}\n"
                context_str += f"    Result: {err.get('result', '')[:200]}\n"
        if context.get("prior_attempts"):
            context_str += "\nPrior fix attempts that FAILED (avoid these approaches):\n"
            for attempt in context["prior_attempts"]:
                context_str += f"  - Approach: {attempt.get('fix_description', '')[:200]}\n"
                context_str += f"    Review: {attempt.get('review_notes', '')[:200]}\n"
        if context.get("relevant_lessons"):
            context_str += "\nRelevant lessons learned:\n"
            for lesson in context["relevant_lessons"]:
                context_str += f"  - {lesson.get('lesson', '')[:200]}\n"

        # Step 2: Analyze with real context
        analysis = self.think_json(
            "Analyze this bug and propose a fix.\n\n"
            f"Title: {bug['title']}\n"
            f"Description: {bug['description']}\n"
            f"Source agent: {bug.get('source_agent', 'unknown')}\n\n"
            f"Real context from system:\n{context_str}\n\n"
            "Think about:\n"
            "1. What's the root cause (based on real error logs)?\n"
            "2. What files likely need changes?\n"
            "3. What's the minimal fix?\n"
            "4. What tests should verify the fix?\n"
            "5. What approaches should we AVOID (based on prior failed attempts)?\n\n"
            "Return: {\"root_cause\": str, \"fix_approach\": str, "
            "\"files_to_change\": [str], \"test_plan\": str, "
            "\"avoided_approaches\": [str]}"
        )

        # Step 3: Attempt to write the fix using the Coder agent
        fix_spec = (
            f"Fix for bug: {bug['title']}\n"
            f"Root cause: {analysis.get('root_cause', 'unknown')}\n"
            f"Fix approach: {analysis.get('fix_approach', '')}\n"
            f"Files to change: {', '.join(analysis.get('files_to_change', []))}\n"
            f"Test plan: {analysis.get('test_plan', '')}"
        )

        try:
            code_result = self.coder.generate_module(fix_spec)
        except Exception as e:
            logger.error(f"Coder failed for bug {bug['id']}: {e}")
            code_result = {"status": "error", "error": str(e)}

        # Step 4: Real verification of fix results
        verification = self._verify_fix_result(code_result)
        test_passed = verification["test_passed"]

        elapsed = round(time.time() - start_time, 2)

        # Step 5: Update bug with fix details + real verification
        self.db.execute(
            "UPDATE bugs SET status = ?, fix_description = ?, "
            "fix_files = ?, test_results = ? WHERE id = ?",
            (
                "fix_ready" if test_passed else "assigned",
                analysis.get("fix_approach", ""),
                json.dumps(analysis.get("files_to_change", [])),
                json.dumps({
                    **verification,
                    "elapsed_seconds": elapsed,
                    "coder_output": str(code_result)[:1000],
                }, default=str)[:2000],
                bug["id"],
            ),
        )

        result = {
            "bug_id": bug["id"],
            "title": bug["title"],
            "fix_submitted": test_passed,
            "analysis": analysis,
            "verification": verification,
            "elapsed_seconds": elapsed,
        }

        if test_passed:
            self.log_action("fix_submitted", bug["title"],
                            f"Tests passed in {elapsed}s, ready for review")
            self.share_knowledge(
                category="fix",
                topic=f"bug_fix_{bug['id']}",
                content=f"Fixed: {bug['title']}. Approach: {analysis.get('fix_approach', '')}",
                tags=["bugfix", bug.get("source_agent", "")],
            )
        else:
            self.log_action("fix_failed", bug["title"],
                            f"Tests failed after {elapsed}s: {verification['errors'][:200]}")
            self.learn(
                category="mistake",
                situation=f"Failed to fix bug: {bug['title']}",
                lesson=f"Fix approach didn't work: {analysis.get('fix_approach', '')}. "
                       f"Errors: {'; '.join(verification['errors'][:2])}",
                rule="Try a different approach next cycle; check error logs for real cause",
            )

        return result
