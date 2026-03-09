"""TrendScout — discovers emerging trends and timing signals.

Figures out its own tools: LLM analysis of patterns, knowledge base,
signals from other agents' discoveries.
"""

from __future__ import annotations

from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class TrendScout(BaseAgent):
    """Spots emerging trends before they mature — timing is everything."""

    name = "trend_scout"
    description = "Discovers emerging market trends and timing signals for new opportunities."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return [
            "Analyze recent agent discoveries for pattern signals",
            "Identify emerging niches from knowledge base",
            "Score opportunities by timing and growth potential",
        ]

    def run(self, focus_areas: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Find trending opportunities.

        Uses: LLM reasoning, shared knowledge base, agent signals.
        """
        # Gather signals from knowledge base
        existing_knowledge = self.ask_knowledge(
            category="opportunity",
        )
        knowledge_context = "\n".join(
            f"- {k['topic']}: {k['content'][:200]}"
            for k in existing_knowledge[:10]
        )

        focus_prompt = ""
        if focus_areas:
            focus_prompt = f"\nFocus especially on: {', '.join(focus_areas)}"

        result = self.think_json(
            "You are a trend scout for an autonomous AI business system. "
            "Identify 3-5 emerging opportunities that are trending UP and can "
            "generate revenue within 1-6 months. Consider: AI tools, SaaS gaps, "
            "content niches, service opportunities, automation plays.\n\n"
            f"Existing knowledge:\n{knowledge_context}\n{focus_prompt}\n\n"
            "Return JSON: {\"opportunities\": [{\"niche\": str, \"trend_direction\": "
            "\"rising\"|\"peaking\"|\"emerging\", \"timing_score\": float (0-1), "
            "\"reasoning\": str}]}"
        )

        opportunities = result.get("opportunities", [])

        self.log_action("trend_scan", f"Found {len(opportunities)} trends",
                        str([o.get("niche") for o in opportunities]))

        return {"opportunities": opportunities}
