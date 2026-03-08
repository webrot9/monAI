"""Sub-agent spawner — AutoGPT-style agent creation and delegation.

The orchestrator uses this to spin up specialized sub-agents on the fly.
Each sub-agent gets a task, a set of tools, and runs autonomously.
"""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from monai.agents.executor import AutonomousExecutor
from monai.agents.identity import IdentityManager
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)


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
            f"You can register new accounts on platforms as needed.\n"
            f"Use the agent identity for any registrations.\n"
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

    def spawn(self, name: str, task: str, max_steps: int = 30) -> SubAgent:
        """Spawn a new sub-agent to handle a task."""
        executor = AutonomousExecutor(
            self.config, self.db, self.llm,
            max_steps=max_steps, headless=True,
        )
        agent = SubAgent(name, task, executor, self.identity)
        self.active_agents[name] = agent
        self.db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
            ("spawner", "spawn", f"Created sub-agent '{name}' for: {task[:200]}"),
        )
        logger.info(f"Spawned sub-agent: {name}")
        return agent

    async def spawn_and_run(self, name: str, task: str, max_steps: int = 30) -> dict[str, Any]:
        """Spawn a sub-agent and run it immediately."""
        agent = self.spawn(name, task, max_steps)
        result = await agent.run()
        del self.active_agents[name]
        return result

    async def run_parallel(self, tasks: list[dict[str, str]],
                           max_steps: int = 30) -> dict[str, dict[str, Any]]:
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
            if isinstance(result, Exception):
                output[name] = {"status": "error", "error": str(result)}
            else:
                output[name] = result
            if name in self.active_agents:
                del self.active_agents[name]

        return output

    def plan_delegation(self, goal: str) -> list[dict[str, str]]:
        """Use LLM to break down a goal into delegatable sub-tasks."""
        response = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    "You are a task planner for an autonomous AI system. "
                    "Break down goals into independent sub-tasks that can run in parallel. "
                    "Each sub-task should be self-contained and actionable."
                )},
                {"role": "user", "content": (
                    f"Break this goal into sub-agent tasks:\n{goal}\n\n"
                    "Return JSON: {\"tasks\": [{\"name\": str (short_snake_case), "
                    "\"task\": str (detailed task description)}]}"
                )},
            ],
            temperature=0.3,
        )
        tasks = response.get("tasks", [])
        logger.info(f"Planned {len(tasks)} sub-tasks for: {goal[:100]}")
        return tasks
