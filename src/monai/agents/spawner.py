"""Sub-agent spawner — AutoGPT-style agent creation and delegation.

The orchestrator uses this to spin up specialized sub-agents on the fly.
Each sub-agent gets a task, a set of tools, and runs autonomously.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from monai.agents.executor import AutonomousExecutor
from monai.agents.identity import IdentityManager
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

_SUBAGENT_FAIL_SCHEMA = """\
CREATE TABLE IF NOT EXISTS subagent_failures (
    task_name TEXT NOT NULL,
    task_hash TEXT NOT NULL,
    failed_at REAL NOT NULL,
    fail_count INTEGER DEFAULT 1,
    reason TEXT,
    PRIMARY KEY (task_name, task_hash)
);
"""

# Block TTL tiers: 30min → 2hr → 8hr → 24hr
_SUBAGENT_FAIL_TTL = [1800, 7200, 28800, 86400]


class SubAgent:
    """A spawned sub-agent that handles a specific task."""

    def __init__(self, name: str, task: str, executor: AutonomousExecutor,
                 identity: IdentityManager):
        self.name = name
        self.task = task
        self.executor = executor
        self.identity = identity
        self.result: dict[str, Any] | None = None

    async def run(self) -> dict[str, Any]:
        """Execute the sub-agent's task."""
        # Build context with identity info
        agent_identity = self.identity.get_identity()
        accounts = self.identity.get_all_accounts()

        context = (
            f"You are sub-agent '{self.name}' of the monAI system.\n"
            f"Agent identity: {json.dumps(agent_identity, default=str)}\n"
            f"Available accounts: {json.dumps([{'platform': a['platform'], 'identifier': a['identifier']} for a in accounts], default=str)}\n"
            f"Use the agent identity for any registrations.\n\n"
            "SUB-AGENT CONSTRAINTS (MANDATORY):\n"
            "- STAY ON TASK: Only perform actions directly related to your assigned task.\n"
            "- Do NOT create accounts on platforms unless the task explicitly requires it.\n"
            "- Do NOT sign up for LinkedIn, Facebook, Twitter, Instagram, or other social "
            "media unless the task specifically says to.\n"
            "- Do NOT write marketing emails, strategy docs, or files unless the task says to.\n"
            "- Do NOT post to example.com, placeholder URLs, or made-up API endpoints.\n"
            "- Do NOT run diagnostic loops (checking IPs, proxy status, SSL certificates).\n"
            "- If the core action is IMPOSSIBLE (site blocked, missing credentials, access "
            "denied), call fail() immediately — do NOT burn steps trying random alternatives.\n"
            "- If you've tried 3 different approaches and all failed, call fail() with a "
            "clear explanation rather than continuing to waste steps.\n"
        )

        self.result = await self.executor.execute_task(self.task, context)
        logger.info(f"Sub-agent '{self.name}' completed: {self.result.get('status')}")
        return self.result


class AgentSpawner:
    """Creates and manages sub-agents for parallel task execution."""

    def __init__(self, config: Config, db: Database, llm: LLM):
        self.config = config
        self.db = db
        self.llm = llm
        self.identity = IdentityManager(config, db, llm)
        self.active_agents: dict[str, SubAgent] = {}
        self._executor_pool = ThreadPoolExecutor(max_workers=5)
        with db.connect() as conn:
            conn.executescript(_SUBAGENT_FAIL_SCHEMA)

    def _task_hash(self, task: str) -> str:
        """Short hash of a task to group similar tasks."""
        import hashlib
        # Normalize: lowercase, strip whitespace, first 200 chars
        normalized = task.lower().strip()[:200]
        return hashlib.md5(normalized.encode()).hexdigest()[:12]

    def _is_task_blocked(self, name: str, task: str) -> tuple[bool, str]:
        """Check if a sub-agent task is still blocked from past failures."""
        th = self._task_hash(task)
        rows = self.db.execute(
            "SELECT failed_at, fail_count, reason FROM subagent_failures "
            "WHERE task_name = ? AND task_hash = ?",
            (name, th),
        )
        if not rows:
            return False, ""
        failed_at = rows[0]["failed_at"]
        count = rows[0]["fail_count"]
        tier = min(count - 1, len(_SUBAGENT_FAIL_TTL) - 1)
        ttl = _SUBAGENT_FAIL_TTL[tier]
        if time.time() - failed_at < ttl:
            return True, rows[0]["reason"] or "unknown"
        # TTL expired
        return False, ""

    def _record_task_failure(self, name: str, task: str, reason: str) -> None:
        """Record a sub-agent task failure with escalating TTL."""
        th = self._task_hash(task)
        now = time.time()
        rows = self.db.execute(
            "SELECT fail_count FROM subagent_failures "
            "WHERE task_name = ? AND task_hash = ?",
            (name, th),
        )
        count = (rows[0]["fail_count"] + 1) if rows else 1
        self.db.execute(
            "INSERT INTO subagent_failures (task_name, task_hash, failed_at, "
            "fail_count, reason) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(task_name, task_hash) DO UPDATE SET "
            "failed_at = excluded.failed_at, "
            "fail_count = excluded.fail_count, "
            "reason = excluded.reason",
            (name, th, now, count, reason[:500]),
        )
        tier = min(count - 1, len(_SUBAGENT_FAIL_TTL) - 1)
        ttl = _SUBAGENT_FAIL_TTL[tier]
        logger.info(
            f"Sub-agent failure #{count} for '{name}' — blocked for {ttl}s"
        )

    def _get_failure_context(self) -> str:
        """Build failure context for sub-agent prompts."""
        rows = self.db.execute(
            "SELECT task_name, fail_count, reason FROM subagent_failures "
            "WHERE fail_count > 0 ORDER BY fail_count DESC LIMIT 15"
        )
        if not rows:
            return ""
        lines = ["PAST SUB-AGENT FAILURES (avoid repeating these):"]
        for r in rows:
            lines.append(
                f"  - '{r['task_name']}': failed {r['fail_count']}x — "
                f"{r['reason'] or 'unknown'}"
            )
        return "\n".join(lines)

    def spawn(self, name: str, task: str, max_steps: int = 15) -> SubAgent:
        """Spawn a new sub-agent to handle a task."""
        executor = AutonomousExecutor(
            self.config, self.db, self.llm,
            max_steps=max_steps, headless=True,
        )
        # Inject failure context so sub-agent knows what failed before
        failure_ctx = self._get_failure_context()
        if failure_ctx:
            task = f"{task}\n\n{failure_ctx}"
        agent = SubAgent(name, task, executor, self.identity)
        self.active_agents[name] = agent
        self.db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
            ("spawner", "spawn", f"Created sub-agent '{name}' for: {task[:200]}"),
        )
        logger.info(f"Spawned sub-agent: {name}")
        return agent

    async def spawn_and_run(self, name: str, task: str, max_steps: int = 15) -> dict[str, Any]:
        """Spawn a sub-agent and run it immediately."""
        # Check if this task is blocked from past failures
        blocked, reason = self._is_task_blocked(name, task)
        if blocked:
            logger.info(f"Sub-agent '{name}' blocked — still cooling down: {reason}")
            return {"status": "blocked", "reason": f"Task blocked (past failure): {reason}"}

        agent = self.spawn(name, task, max_steps)
        result = await agent.run()
        del self.active_agents[name]

        # Record failure if it didn't succeed
        status = result.get("status", "")
        if status in ("error", "failed"):
            self._record_task_failure(
                name, task, result.get("error", result.get("reason", "unknown"))
            )

        return result

    async def run_parallel(self, tasks: list[dict[str, str]],
                           max_steps: int = 15) -> dict[str, dict[str, Any]]:
        """Run multiple sub-agents in parallel.

        Args:
            tasks: List of {"name": str, "task": str} dicts
        """
        agents = [self.spawn(t["name"], t["task"], max_steps) for t in tasks]
        results = await asyncio.gather(
            *[a.run() for a in agents],
            return_exceptions=True,
        )

        output = {}
        for task_info, result in zip(tasks, results):
            name = task_info["name"]
            task_str = task_info["task"]
            if isinstance(result, Exception):
                output[name] = {"status": "error", "error": str(result)}
                self._record_task_failure(name, task_str, str(result))
            else:
                output[name] = result
                if result.get("status") in ("error", "failed"):
                    self._record_task_failure(
                        name, task_str,
                        result.get("error", result.get("reason", "unknown"))
                    )
            if name in self.active_agents:
                del self.active_agents[name]

        return output

    def plan_delegation(self, goal: str) -> list[dict[str, str]]:
        """Break down a goal into delegatable sub-tasks with dependency analysis.

        Uses keyword-based decomposition to extract action verbs and identify
        parallelizable vs sequential tasks, then falls back to LLM for complex
        goals that resist pattern matching.
        """
        # Try structured decomposition first
        structured = self._decompose_structured(goal)
        if structured:
            logger.info(f"Structured decomposition: {len(structured)} sub-tasks for: {goal[:100]}")
            return structured

        # Fall back to LLM for complex/ambiguous goals
        response = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    "You are a task planner for an autonomous AI system. "
                    "Break down goals into independent sub-tasks that can run in parallel. "
                    "Each sub-task should be self-contained and actionable. "
                    "Mark tasks that depend on other tasks with a 'depends_on' field."
                )},
                {"role": "user", "content": (
                    f"Break this goal into sub-agent tasks:\n{goal}\n\n"
                    "Return JSON: {{\"tasks\": [{{\"name\": str (short_snake_case), "
                    "\"task\": str (detailed task description), "
                    "\"depends_on\": [str] (names of tasks this depends on, empty if independent)}}]}}"
                )},
            ],
            temperature=0.3,
        )
        tasks = response.get("tasks", [])
        # Validate and clean up
        tasks = self._resolve_dependencies(tasks)
        logger.info(f"Planned {len(tasks)} sub-tasks for: {goal[:100]}")
        return tasks

    # ── Structured task decomposition ─────────────────────────────

    # Action patterns that map to recognizable task types
    _ACTION_PATTERNS = {
        "research": ["research", "analyze", "investigate", "study", "explore", "find"],
        "build": ["build", "create", "implement", "develop", "write", "code"],
        "test": ["test", "verify", "validate", "check", "ensure"],
        "deploy": ["deploy", "publish", "launch", "release", "ship"],
        "market": ["market", "promote", "advertise", "outreach", "campaign"],
        "monitor": ["monitor", "track", "watch", "observe", "measure"],
    }

    def _decompose_structured(self, goal: str) -> list[dict[str, str]] | None:
        """Try to decompose a goal using keyword patterns.

        Returns None if the goal is too complex for pattern matching.
        """
        goal_lower = goal.lower()

        # Check for explicit list separators (numbered lists, bullet points, "and")
        lines = [l.strip() for l in goal.split("\n") if l.strip()]
        # Detect numbered/bulleted lists
        list_items = []
        for line in lines:
            # Match "1. do X", "- do X", "* do X"
            match = re.match(r'^(?:\d+[.)]\s*|[-*]\s+)(.+)', line)
            if match:
                list_items.append(match.group(1))

        if len(list_items) >= 2:
            tasks = []
            for i, item in enumerate(list_items):
                name = re.sub(r'[^a-z0-9]+', '_', item.lower()[:30]).strip('_')
                tasks.append({"name": name or f"task_{i+1}", "task": item})
            return tasks

        # Check for "and"-separated tasks ("research X and build Y and test Z")
        if " and " in goal_lower and len(goal_lower.split(" and ")) >= 2:
            parts = goal.split(" and ")
            if all(len(p.split()) <= 15 for p in parts):  # Short enough to be separate tasks
                tasks = []
                for i, part in enumerate(parts):
                    part = part.strip().rstrip(".")
                    name = re.sub(r'[^a-z0-9]+', '_', part.lower()[:30]).strip('_')
                    tasks.append({"name": name or f"task_{i+1}", "task": part})
                return tasks

        return None

    def _resolve_dependencies(self, tasks: list[dict]) -> list[dict[str, str]]:
        """Validate dependency references and sort tasks topologically.

        Ensures tasks with dependencies come after their prerequisites.
        Invalid dependency references are silently dropped.
        """
        if not tasks:
            return tasks

        names = {t.get("name", "") for t in tasks}
        # Clean up invalid dependencies
        for t in tasks:
            deps = t.get("depends_on", [])
            if isinstance(deps, list):
                t["depends_on"] = [d for d in deps if d in names]
            else:
                t["depends_on"] = []

        # Topological sort (Kahn's algorithm)
        in_degree = {t["name"]: len(t.get("depends_on", [])) for t in tasks}
        queue = [t for t in tasks if in_degree.get(t["name"], 0) == 0]
        sorted_tasks = []
        while queue:
            current = queue.pop(0)
            sorted_tasks.append(current)
            for t in tasks:
                if current["name"] in t.get("depends_on", []):
                    in_degree[t["name"]] -= 1
                    if in_degree[t["name"]] == 0:
                        queue.append(t)

        # If cycle detected, return original order
        if len(sorted_tasks) != len(tasks):
            return tasks

        return sorted_tasks
