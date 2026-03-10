"""OpenAI LLM integration with per-call cost tracking.

Every single API call is logged with token usage and cost in EUR.
The system must be self-sustaining — these costs must be covered by revenue.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Any, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from monai.config import Config

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Raised when a cycle exceeds its cost or call budget."""
    pass


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
        # Per-cycle budget enforcement
        self.cycle_cost: float = 0.0
        self.cycle_calls: int = 0
        self.max_cycle_cost: float = 5.0  # EUR
        self.max_cycle_calls: int = 200

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
            # Cycle tracking
            self.cycle_cost += cost
            self.cycle_calls += 1
            # Check cycle limits
            if self.cycle_cost > self.max_cycle_cost:
                raise BudgetExceededError(
                    f"Cycle cost limit exceeded: €{self.cycle_cost:.4f} > €{self.max_cycle_cost:.2f}"
                )
            if self.cycle_calls > self.max_cycle_calls:
                raise BudgetExceededError(
                    f"Cycle call limit exceeded: {self.cycle_calls} > {self.max_cycle_calls}"
                )

        return cost

    def reset_cycle(self) -> None:
        """Reset per-cycle counters. Called at the start of each orchestration cycle."""
        with self._lock:
            self.cycle_cost = 0.0
            self.cycle_calls = 0
            # Prevent unbounded dict growth — keep only top 100 entries
            if len(self.cost_by_caller) > 100:
                sorted_callers = sorted(self.cost_by_caller.items(), key=lambda x: x[1], reverse=True)
                self.cost_by_caller = dict(sorted_callers[:50])
            if len(self.cost_by_model) > 50:
                sorted_models = sorted(self.cost_by_model.items(), key=lambda x: x[1], reverse=True)
                self.cost_by_model = dict(sorted_models[:25])

    def set_cycle_limits(self, max_cost: float, max_calls: int) -> None:
        """Update per-cycle budget limits."""
        with self._lock:
            self.max_cycle_cost = max_cost
            self.max_cycle_calls = max_calls

    def record_minor(self, cost_type: str, cost_eur: float,
                     caller: str = "unknown", description: str = "") -> float:
        """Record a non-API cost (platform fees, subscriptions, tools, etc.).

        Args:
            cost_type: One of: platform_fee, subscription, tool, hosting, domain, other
            cost_eur: Cost in EUR
            caller: Which agent incurred this cost
            description: Human-readable description
        """
        with self._lock:
            self.total_cost_eur += cost_eur
            self.cost_by_caller[caller] = self.cost_by_caller.get(caller, 0) + cost_eur
            key = f"minor:{cost_type}"
            self.cost_by_model[key] = self.cost_by_model.get(key, 0) + cost_eur
            self.cycle_cost += cost_eur
            if self.cycle_cost > self.max_cycle_cost:
                raise BudgetExceededError(
                    f"Cycle cost limit exceeded: €{self.cycle_cost:.4f} > €{self.max_cycle_cost:.2f}"
                )
        return cost_eur

    def get_summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_calls": self.calls,
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_cost_eur": round(self.total_cost_eur, 6),
                "cost_by_model": {k: round(v, 6) for k, v in self.cost_by_model.items()},
                "cost_by_caller": {k: round(v, 6) for k, v in self.cost_by_caller.items()},
                "cycle_cost": round(self.cycle_cost, 6),
                "cycle_calls": self.cycle_calls,
            }

    def save_state(self, path: str) -> None:
        """Persist tracker state to a JSON file for session continuity."""
        with self._lock:
            state = {
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_cost_eur": self.total_cost_eur,
                "calls": self.calls,
                "cost_by_model": self.cost_by_model,
                "cost_by_caller": self.cost_by_caller,
            }
        import pathlib
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)

    def load_state(self, path: str) -> bool:
        """Load tracker state from a JSON file. Returns True if loaded."""
        import pathlib
        p = pathlib.Path(path)
        if not p.exists():
            return False
        try:
            with open(p) as f:
                state = json.load(f)
            with self._lock:
                self.total_input_tokens = state.get("total_input_tokens", 0)
                self.total_output_tokens = state.get("total_output_tokens", 0)
                self.total_cost_eur = state.get("total_cost_eur", 0.0)
                self.calls = state.get("calls", 0)
                self.cost_by_model = state.get("cost_by_model", {})
                self.cost_by_caller = state.get("cost_by_caller", {})
            logger.info(f"Loaded cost tracker state: {self.calls} calls, €{self.total_cost_eur:.4f}")
            return True
        except Exception as e:
            logger.warning(f"Failed to load cost tracker state: {e}")
            return False


# Global tracker — shared across all LLM instances
_global_tracker = CostTracker()


def get_cost_tracker() -> CostTracker:
    return _global_tracker


class LLM:
    """Wrapper around OpenAI API with mandatory cost tracking."""

    # Model tiers for cost-aware selection
    TIER_FULL = "full"       # gpt-4o / gpt-4.1 — complex reasoning, quality content
    TIER_MINI = "mini"       # gpt-4o-mini / gpt-4.1-mini — routine tasks, planning
    TIER_NANO = "nano"       # gpt-4.1-nano — simple extraction, classification, formatting

    def __init__(self, config: Config | None = None, caller: str = "unknown"):
        self.config = config or Config.load()
        self.client = OpenAI(api_key=self.config.llm.api_key)
        self.caller = caller  # Which agent/module is making the call
        self.tracker = _global_tracker
        self._db = None  # Lazy — set when DB is available

    def get_model(self, tier: str = "mini") -> str:
        """Get the appropriate model for a cost tier.

        Tiers:
            full — Complex reasoning, content generation, code writing
            mini — Routine planning, analysis, JSON extraction (default)
            nano — Simple classification, formatting, yes/no decisions
        """
        if tier == self.TIER_FULL:
            return self.config.llm.model
        elif tier == self.TIER_NANO:
            return "gpt-4.1-nano"
        return self.config.llm.model_mini

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

    def nano(self, prompt: str, system: str = "") -> str:
        """Ultra-cheap call using nano model. For simple extraction/classification."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, model=self.get_model(self.TIER_NANO), max_tokens=512)

    def nano_json(self, prompt: str, system: str = "") -> dict:
        """Ultra-cheap JSON call using nano model."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat_json(messages, model=self.get_model(self.TIER_NANO))

    def chat_structured(
        self,
        messages: list[dict[str, str]],
        response_model: type[T],
        model: str | None = None,
        temperature: float = 0.3,
    ) -> T:
        """Chat that returns a validated pydantic model instance.

        Uses json_mode to get JSON from the LLM, then validates it against
        the provided pydantic model. Retries once on validation failure,
        appending the error details so the LLM can self-correct.
        """
        schema_hint = json.dumps(response_model.model_json_schema(), indent=2)
        # Inject schema instruction into the last user message (or system)
        augmented = list(messages)
        schema_instruction = (
            f"\n\nRespond with JSON matching this exact schema:\n```json\n{schema_hint}\n```"
        )
        # Append to the last user message
        if augmented and augmented[-1]["role"] == "user":
            augmented[-1] = {
                "role": "user",
                "content": augmented[-1]["content"] + schema_instruction,
            }
        else:
            augmented.append({"role": "user", "content": schema_instruction})

        raw = self.chat(augmented, model=model, temperature=temperature, json_mode=True)

        # First attempt at validation
        try:
            return response_model.model_validate_json(raw)
        except ValidationError as e:
            logger.warning(
                f"Structured output validation failed, retrying: {e.error_count()} errors"
            )

        # Retry once with error feedback
        augmented.append({"role": "assistant", "content": raw})
        augmented.append({
            "role": "user",
            "content": (
                f"The JSON you returned failed validation:\n{e}\n\n"
                f"Fix the errors and return valid JSON matching the schema."
            ),
        })

        raw_retry = self.chat(augmented, model=model, temperature=temperature, json_mode=True)
        return response_model.model_validate_json(raw_retry)

    def quick_structured(
        self,
        prompt: str,
        response_model: type[T],
        system: str = "",
    ) -> T:
        """Quick structured response using mini model. Returns a validated pydantic model."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat_structured(
            messages, response_model, model=self.config.llm.model_mini,
        )

    def _persist_cost(self, model: str, input_tokens: int, output_tokens: int, cost: float):
        """Log cost to database for the commercialista."""
        try:
            self._db.execute_insert(
                "INSERT INTO transactions (type, category, amount, currency, description) "
                "VALUES ('expense', 'api_cost', ?, 'EUR', ?)",
                (cost, f"OpenAI {model}: {input_tokens}in/{output_tokens}out by {self.caller}"),
            )
        except Exception as e:
            logger.warning(f"Failed to persist cost to DB: {e}")
