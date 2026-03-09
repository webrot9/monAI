"""MarketResearcher — sizes markets and validates demand.

Determines its own research methodology per niche:
- LLM reasoning for market sizing
- Knowledge base for existing data
- Competitor signals from other agents
"""

from __future__ import annotations

from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class MarketResearcher(BaseAgent):
    """Sizes markets and validates whether there's real demand."""

    name = "market_researcher"
    description = "Analyzes market size, demand signals, and willingness to pay for specific niches."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return [
            "Define the target niche and customer profile",
            "Estimate market size (TAM, SAM, SOM)",
            "Assess demand signals and willingness to pay",
            "Determine viability threshold",
        ]

    def run(self, niche: str = "", **kwargs: Any) -> dict[str, Any]:
        """Research a specific niche for viability.

        Returns market analysis with viability verdict.
        """
        if not niche:
            return {"viable": False, "reason": "No niche specified"}

        # Check existing knowledge first
        prior = self.ask_knowledge(topic=niche, category="market_research")
        prior_context = ""
        if prior:
            prior_context = f"\nPrior research:\n" + "\n".join(
                f"- {p['content'][:200]}" for p in prior[:3]
            )

        result = self.think_json(
            f"Analyze the market for: {niche}\n{prior_context}\n\n"
            "Estimate realistically — monAI is an AI system with €500 starting capital. "
            "We need niches where an automated system can compete.\n\n"
            "Return JSON: {\"niche\": str, \"market_size\": str (e.g. '$50M TAM'), "
            "\"target_customer\": str, \"willingness_to_pay\": str, "
            "\"demand_signals\": [str], \"barriers_to_entry\": [str], "
            "\"viable\": bool, \"estimated_monthly_revenue\": float, "
            "\"confidence\": float (0-1), \"reasoning\": str}"
        )

        if result.get("viable"):
            self.share_knowledge(
                "market_research", niche, str(result),
                confidence=result.get("confidence", 0.5),
                tags=["market", niche],
            )

        self.log_action("market_research", niche,
                        f"viable={result.get('viable')} rev={result.get('estimated_monthly_revenue')}")

        return result
