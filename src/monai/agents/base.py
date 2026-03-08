"""Base agent class that all monAI agents inherit from."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Base class for all monAI agents.

    Every agent follows the same rules:
    - Plan before acting
    - Track everything (actions, spending, revenue)
    - Verify results before marking done
    - Log all decisions for audit trail
    """

    name: str = "base"
    description: str = ""

    def __init__(self, config: Config, db: Database, llm: LLM):
        self.config = config
        self.db = db
        self.llm = llm
        self.logger = logging.getLogger(f"monai.{self.name}")

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

    def think(self, prompt: str, context: str = "") -> str:
        """Use LLM to reason about a decision. Logs the thought for audit."""
        system = (
            f"You are {self.name}, an autonomous AI agent part of the monAI system. "
            f"Your role: {self.description}. "
            "Think step by step. Be practical and profit-focused. "
            "Consider risks and expected returns before any action."
        )
        if context:
            prompt = f"Context:\n{context}\n\nQuestion:\n{prompt}"
        response = self.llm.quick(prompt, system=system)
        self.log_action("think", prompt[:200], response[:500])
        return response

    def think_json(self, prompt: str, context: str = "") -> dict:
        """Use LLM to reason and return structured JSON."""
        system = (
            f"You are {self.name}, an autonomous AI agent part of the monAI system. "
            f"Your role: {self.description}. "
            "Think step by step. Respond with valid JSON only."
        )
        if context:
            prompt = f"Context:\n{context}\n\nQuestion:\n{prompt}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        response = self.llm.chat_json(messages)
        self.log_action("think_json", prompt[:200], str(response)[:500])
        return response

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
