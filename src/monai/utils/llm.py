"""OpenAI LLM integration with per-call cost tracking.

Every single API call is logged with token usage and cost in EUR.
The system must be self-sustaining — these costs must be covered by revenue.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Any

from openai import OpenAI

from monai.config import Config

logger = logging.getLogger(__name__)

# Pricing per 1M tokens (EUR, approximate — updated March 2026)
# Source: OpenAI pricing page. Adjust if prices change.
MODEL_PRICING = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o-2024-11-20": {"input": 2.50, "output": 10.00},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
}


class CostTracker:
    """Thread-safe tracker for all API costs."""

    def __init__(self):
        self._lock = threading.Lock()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_eur = 0.0
        self.calls = 0
        self.cost_by_model: dict[str, float] = {}
        self.cost_by_caller: dict[str, float] = {}

    def record(self, model: str, input_tokens: int, output_tokens: int,
               caller: str = "unknown") -> float:
        """Record a call and return its cost in EUR."""
        pricing = MODEL_PRICING.get(model, MODEL_PRICING.get("gpt-4o-mini"))
        cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

        with self._lock:
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.total_cost_eur += cost
            self.calls += 1
            self.cost_by_model[model] = self.cost_by_model.get(model, 0) + cost
            self.cost_by_caller[caller] = self.cost_by_caller.get(caller, 0) + cost

        return cost

    def get_summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_calls": self.calls,
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_cost_eur": round(self.total_cost_eur, 6),
                "cost_by_model": {k: round(v, 6) for k, v in self.cost_by_model.items()},
                "cost_by_caller": {k: round(v, 6) for k, v in self.cost_by_caller.items()},
            }


# Global tracker — shared across all LLM instances
_global_tracker = CostTracker()


def get_cost_tracker() -> CostTracker:
    return _global_tracker


class LLM:
    """Wrapper around OpenAI API with mandatory cost tracking."""

    def __init__(self, config: Config | None = None, caller: str = "unknown"):
        self.config = config or Config.load()
        self.client = OpenAI(api_key=self.config.llm.api_key)
        self.caller = caller  # Which agent/module is making the call
        self.tracker = _global_tracker
        self._db = None  # Lazy — set when DB is available

    def set_db(self, db):
        """Attach database for persistent cost logging."""
        self._db = db

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        used_model = model or self.config.llm.model
        kwargs: dict[str, Any] = {
            "model": used_model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.llm.temperature,
            "max_tokens": max_tokens or self.config.llm.max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = self.client.chat.completions.create(**kwargs)

        # Track cost
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        cost = self.tracker.record(used_model, input_tokens, output_tokens, self.caller)

        # Persist to DB if available
        if self._db:
            self._persist_cost(used_model, input_tokens, output_tokens, cost)

        logger.debug(
            f"LLM [{self.caller}] {used_model}: "
            f"{input_tokens}in/{output_tokens}out = €{cost:.6f}"
        )

        return response.choices[0].message.content

    def chat_json(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.3,
    ) -> dict:
        raw = self.chat(messages, model=model, temperature=temperature, json_mode=True)
        return json.loads(raw)

    def quick(self, prompt: str, system: str = "", model: str | None = None) -> str:
        """Quick single-prompt call. Uses mini model by default for cost savings."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, model=model or self.config.llm.model_mini)

    def quick_json(self, prompt: str, system: str = "") -> dict:
        """Quick JSON response using mini model."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat_json(messages, model=self.config.llm.model_mini)

    def _persist_cost(self, model: str, input_tokens: int, output_tokens: int, cost: float):
        """Log cost to database for the commercialista."""
        try:
            self._db.execute_insert(
                "INSERT INTO transactions (type, category, amount, currency, description) "
                "VALUES ('expense', 'api_cost', ?, 'EUR', ?)",
                (cost, f"OpenAI {model}: {input_tokens}in/{output_tokens}out by {self.caller}"),
            )
        except Exception:
            pass  # Don't let logging failures break the agent
