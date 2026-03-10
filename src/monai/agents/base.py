"""Base agent class that all monAI agents inherit from."""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel

from monai.agents.ethics import CORE_DIRECTIVES, get_directives_for_context, is_action_blocked
from monai.agents.memory import SharedMemory
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Base class for all monAI agents.

    Every agent follows the same rules:
    - Plan before acting
    - Track everything (actions, spending, revenue)
    - Collaborate: share knowledge, communicate, learn from mistakes
    - Verify results before marking done
    - Log all decisions for audit trail
    """

    name: str = "base"
    description: str = ""

    def __init__(self, config: Config, db: Database, llm: LLM):
        self.config = config
        self.db = db
        self.llm = llm
        self.memory = SharedMemory(db)
        self.logger = logging.getLogger(f"monai.{self.name}")
        self._cycle: int = 0
        self._coder = None  # Lazy-loaded

    @property
    def coder(self):
        """Lazy-load coder — any agent can write code when needed."""
        if self._coder is None:
            from monai.agents.coder import Coder
            self._coder = Coder(self.config, self.db, self.llm)
        return self._coder

    def write_code(self, spec: str, project_dir: str | None = None,
                   language: str = "python") -> dict:
        """Write tested code. Returns only if tests pass."""
        return self.coder.generate_module(spec, project_dir, language)

    # ── Core Actions ────────────────────────────────────────────

    def log_action(self, action: str, details: str = "", result: str = ""):
        self.db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details, result) VALUES (?, ?, ?, ?)",
            (self.name, action, details, result),
        )
        self.logger.info(f"[{self.name}] {action}: {details}")

    def record_expense(self, amount: float, category: str, description: str,
                       strategy_id: int | None = None, project_id: int | None = None):
        self.db.execute_insert(
            "INSERT INTO transactions (strategy_id, project_id, type, category, amount, description) "
            "VALUES (?, ?, 'expense', ?, ?, ?)",
            (strategy_id, project_id, category, amount, description),
        )

    def record_revenue(self, amount: float, category: str, description: str,
                       strategy_id: int | None = None, project_id: int | None = None):
        self.db.execute_insert(
            "INSERT INTO transactions (strategy_id, project_id, type, category, amount, description) "
            "VALUES (?, ?, 'revenue', ?, ?, ?)",
            (strategy_id, project_id, category, amount, description),
        )

    # ── Thinking (LLM-powered reasoning) ───────────────────────

    def think(self, prompt: str, context: str = "") -> str:
        """Use LLM to reason about a decision. Enriched with lessons + knowledge."""
        system = self._build_system_prompt()
        if context:
            prompt = f"Context:\n{context}\n\nQuestion:\n{prompt}"

        # Inject relevant lessons and recent knowledge
        enrichment = self._get_context_enrichment(prompt)
        if enrichment:
            prompt = f"{enrichment}\n\n{prompt}"

        response = self.llm.quick(prompt, system=system)
        self.log_action("think", prompt[:200], response[:500])
        return response

    def think_json(self, prompt: str, context: str = "") -> dict:
        """Use LLM to reason and return structured JSON."""
        system = self._build_system_prompt() + "\nRespond with valid JSON only."
        if context:
            prompt = f"Context:\n{context}\n\nQuestion:\n{prompt}"

        enrichment = self._get_context_enrichment(prompt)
        if enrichment:
            prompt = f"{enrichment}\n\n{prompt}"

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        response = self.llm.chat_json(messages)
        self.log_action("think_json", prompt[:200], str(response)[:500])
        return response

    def think_structured(
        self,
        prompt: str,
        response_model: type[T],
        context: str = "",
    ) -> T:
        """Use LLM to reason and return a validated pydantic model instance."""
        system = self._build_system_prompt()
        if context:
            prompt = f"Context:\n{context}\n\nQuestion:\n{prompt}"

        enrichment = self._get_context_enrichment(prompt)
        if enrichment:
            prompt = f"{enrichment}\n\n{prompt}"

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        result = self.llm.chat_structured(messages, response_model)
        self.log_action("think_structured", prompt[:200], str(result.model_dump())[:500])
        return result

    def think_cheap(self, prompt: str, context: str = "") -> str:
        """Use nano model for simple decisions — 25x cheaper than full model."""
        system = self._build_system_prompt()
        if context:
            prompt = f"Context:\n{context}\n\nQuestion:\n{prompt}"
        response = self.llm.nano(prompt, system=system)
        self.log_action("think_cheap", prompt[:200], response[:500])
        return response

    def think_cheap_json(self, prompt: str, context: str = "") -> dict:
        """Use nano model for simple JSON extraction — 25x cheaper."""
        system = self._build_system_prompt() + "\nRespond with valid JSON only."
        if context:
            prompt = f"Context:\n{context}\n\nQuestion:\n{prompt}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        response = self.llm.chat_json(messages, model=self.llm.get_model("nano"))
        self.log_action("think_cheap_json", prompt[:200], str(response)[:500])
        return response

    def _build_system_prompt(self) -> str:
        """Build system prompt with ethics, identity, role, and learned rules."""
        # Ethics are ALWAYS first — non-negotiable
        learned_rules = self.memory.get_rules_for_agent(self.name)
        learned_text = ""
        if learned_rules:
            learned_text = "\n\nLEARNED RULES:\n" + "\n".join(f"- {r}" for r in learned_rules)

        return (
            f"{CORE_DIRECTIVES}\n\n"
            f"You are {self.name}, an autonomous AI agent part of the monAI system. "
            f"Your role: {self.description}. "
            "Think step by step. Be practical and profit-focused. "
            "Consider risks and expected returns before any action. "
            "You collaborate with other agents — share discoveries and ask for help when needed. "
            "Everything you produce must be HIGH QUALITY — no AI slop, no shortcuts, no filler."
            f"{learned_text}"
        )

    def _get_context_enrichment(self, prompt: str) -> str:
        """Pull relevant knowledge and lessons to enrich LLM context."""
        parts = []

        # Get lessons for this agent
        lessons = self.memory.get_lessons(self.name, include_shared=True)
        if lessons:
            lesson_text = "\n".join(
                f"- [{l['category']}] {l['lesson']}" + (f" Rule: {l['rule']}" if l.get('rule') else "")
                for l in lessons[:10]  # Cap at 10 most relevant
            )
            parts.append(f"LESSONS LEARNED:\n{lesson_text}")

        # Get recent activity from other agents (situational awareness)
        recent = self.memory.get_recent_activity(limit=5)
        other_activity = [a for a in recent if a["agent_name"] != self.name]
        if other_activity:
            activity_text = "\n".join(
                f"- [{a['agent_name']}] {a['summary']}"
                for a in other_activity
            )
            parts.append(f"RECENT ACTIVITY FROM OTHER AGENTS:\n{activity_text}")

        # Check for unread messages
        messages = self.memory.get_messages(self.name, unread_only=True, limit=5)
        if messages:
            msg_text = "\n".join(
                f"- From {m['from_agent']} ({m['msg_type']}): {m['subject']} — {m['body'][:100]}"
                for m in messages
            )
            parts.append(f"UNREAD MESSAGES:\n{msg_text}")

        return "\n\n".join(parts) if parts else ""

    # ── Collaboration ───────────────────────────────────────────

    def share_knowledge(self, category: str, topic: str, content: str,
                        confidence: float = 1.0, tags: list[str] | None = None):
        """Share a discovery or insight with all agents."""
        self.memory.store_knowledge(
            category=category,
            topic=topic,
            content=content,
            source_agent=self.name,
            confidence=confidence,
            tags=tags,
        )
        self.logger.info(f"[{self.name}] Shared knowledge: {topic}")

    def ask_knowledge(self, topic: str = "", category: str = "",
                      tags: list[str] | None = None) -> list[dict[str, Any]]:
        """Query the shared knowledge base."""
        results = self.memory.query_knowledge(topic, category, tags)
        # Mark as referenced
        for r in results:
            self.memory.mark_knowledge_used(r["id"])
        return results

    def send_to_agent(self, to_agent: str, msg_type: str, subject: str,
                      body: str, priority: int = 5, metadata: dict | None = None) -> int:
        """Send a message to another agent."""
        msg_id = self.memory.send_message(
            self.name, to_agent, msg_type, subject, body, priority, metadata=metadata,
        )
        self.logger.info(f"[{self.name}] → [{to_agent}] {msg_type}: {subject}")
        return msg_id

    def broadcast(self, msg_type: str, subject: str, body: str, priority: int = 5) -> int:
        """Broadcast a message to all agents."""
        return self.memory.broadcast(self.name, msg_type, subject, body, priority)

    def check_messages(self) -> list[dict[str, Any]]:
        """Check for new messages from other agents."""
        messages = self.memory.get_messages(self.name, unread_only=True)
        for msg in messages:
            self.memory.mark_message_read(msg["id"])
        return messages

    def request_help(self, task: str, from_agent: str = "orchestrator") -> int:
        """Request help from another agent (typically the orchestrator)."""
        return self.send_to_agent(
            from_agent, "request",
            f"Help needed: {task[:50]}",
            task,
            priority=3,
        )

    def handoff(self, to_agent: str, task: str, context: dict | None = None) -> int:
        """Hand off a task to another agent with full context."""
        return self.send_to_agent(
            to_agent, "handoff",
            f"Handoff: {task[:50]}",
            json.dumps({"task": task, "context": context or {}}, default=str),
            priority=2,
        )

    # ── Learning ────────────────────────────────────────────────

    def learn(self, category: str, situation: str, lesson: str,
              rule: str = "", severity: str = "medium"):
        """Record a lesson learned. Automatically shared with all agents."""
        self.memory.record_lesson(
            self.name, category, situation, lesson, rule, severity,
        )

    def learn_from_error(self, error: Exception, context: str = ""):
        """Automatically extract a lesson from an error."""
        lesson = self.llm.quick(
            f"An error occurred:\nError: {error}\nContext: {context}\n\n"
            "Extract a concise lesson and a concrete rule to prevent this. "
            "Reply in format: LESSON: ...\nRULE: ...",
            system="You analyze errors and extract actionable lessons.",
        )
        # Parse the response
        lesson_text = lesson.split("RULE:")[0].replace("LESSON:", "").strip()
        rule_text = lesson.split("RULE:")[-1].strip() if "RULE:" in lesson else ""

        self.learn(
            category="mistake",
            situation=f"Error: {error}",
            lesson=lesson_text,
            rule=rule_text,
            severity="high",
        )

    def get_my_lessons(self) -> list[dict[str, Any]]:
        """Get all lessons relevant to this agent."""
        return self.memory.get_lessons(self.name, include_shared=True)

    # ── Journal ─────────────────────────────────────────────────

    def journal(self, action_type: str, summary: str,
                details: dict | None = None, outcome: str = ""):
        """Write a journal entry — what I did and why."""
        self.memory.journal_entry(
            self.name, action_type, summary, details, outcome, self._cycle,
        )

    # ── Lifecycle ───────────────────────────────────────────────

    def start_cycle(self, cycle: int):
        """Called at the start of each orchestration cycle."""
        self._cycle = cycle
        # Process any pending messages
        messages = self.check_messages()
        if messages:
            self.journal("collaborate",
                         f"Received {len(messages)} messages",
                         {"messages": [m["subject"] for m in messages]})

    @abstractmethod
    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Execute the agent's main loop. Returns a status dict."""
        ...

    @abstractmethod
    def plan(self) -> list[str]:
        """Generate a plan of actions before executing. Returns list of steps."""
        ...

    def evaluate_opportunity(self, opportunity: str) -> dict:
        """Evaluate if an opportunity is worth pursuing."""
        return self.think_json(
            f"Evaluate this opportunity and return JSON with fields: "
            f"worth_pursuing (bool), expected_revenue (float), estimated_cost (float), "
            f"risk_level (low/medium/high), confidence (0-1), reasoning (string).\n\n"
            f"Opportunity: {opportunity}"
        )
