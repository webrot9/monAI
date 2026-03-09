"""CompetitorAnalyst — maps the competitive landscape for a niche.

Determines its own research approach:
- LLM reasoning about known competitors
- Knowledge base queries for prior intel
- Pattern analysis from agent discoveries
"""

from __future__ import annotations

from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class CompetitorAnalyst(BaseAgent):
    """Maps who's already in a market and where the gaps are."""

    name = "competitor_analyst"
    description = "Analyzes competitive landscape, identifies gaps, and assesses entry difficulty."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return [
            "Identify key competitors in the niche",
            "Analyze their strengths and weaknesses",
            "Find underserved gaps monAI can exploit",
            "Assess competitive moat and entry difficulty",
        ]

    def run(self, niche: str = "", **kwargs: Any) -> dict[str, Any]:
        """Analyze competition in a specific niche."""
        if not niche:
            return {"competition_level": "unknown", "gaps": []}

        # Check existing intel
        prior = self.ask_knowledge(topic=niche, category="competitive_intel")
        prior_context = ""
        if prior:
            prior_context = f"\nPrior intel:\n" + "\n".join(
                f"- {p['content'][:200]}" for p in prior[:3]
            )

        result = self.think_json(
            f"Analyze the competitive landscape for: {niche}\n{prior_context}\n\n"
            "Consider: monAI is an automated AI system. We have advantages in speed, "
            "cost, and 24/7 operation, but disadvantages in brand trust and human touch.\n\n"
            "Return JSON: {\"niche\": str, \"competitors\": [{\"name\": str, "
            "\"strengths\": [str], \"weaknesses\": [str]}], "
            "\"competition_level\": \"low\"|\"medium\"|\"high\"|\"saturated\", "
            "\"gaps\": [str], \"moat_difficulty\": \"easy\"|\"medium\"|\"hard\", "
            "\"monai_advantages\": [str], \"entry_strategy\": str, \"confidence\": float}"
        )

        if result.get("gaps"):
            self.share_knowledge(
                "competitive_intel", niche,
                f"Competition: {result.get('competition_level')}. "
                f"Gaps: {', '.join(result.get('gaps', []))}",
                confidence=result.get("confidence", 0.5),
                tags=["competition", niche],
            )

        self.log_action("competitor_analysis", niche,
                        f"level={result.get('competition_level')} gaps={len(result.get('gaps', []))}")

        return result
