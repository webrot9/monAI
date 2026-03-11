"""MarketResearcher — sizes markets and validates demand.

Real autonomous capabilities:
- Searches Google for real market size data and industry reports
- Browses Statista, industry blogs, and news for market intelligence
- Checks job boards and freelancer platforms for demand signals
- Searches for real pricing data from existing solutions
- Combines real web data with LLM reasoning for viability assessment
"""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)


class MarketResearcher(BaseAgent):
    """Sizes markets and validates whether there's real demand.

    Uses real web data collection to validate market size and demand,
    not just LLM reasoning.
    """

    name = "market_researcher"
    description = "Analyzes market size, demand signals, and willingness to pay for specific niches."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return [
            "Search web for real market size data and reports",
            "Check demand signals on job boards and freelancer platforms",
            "Research real pricing from existing solutions",
            "Validate willingness to pay with real data",
            "Synthesize into viability assessment",
        ]

    def run(self, niche: str = "", **kwargs: Any) -> dict[str, Any]:
        """Research a specific niche for viability using real web data.

        Layer 1: Real web search for market size data
        Layer 2: Real demand signal collection (job boards, platforms)
        Layer 3: Real pricing research from existing solutions
        Layer 4: LLM synthesis with knowledge base
        """
        if not niche:
            return {"viable": False, "reason": "No niche specified"}

        # ── Layer 1: Real market size data ─────────────────────────
        market_data = self._search_market_size(niche)

        # ── Layer 2: Real demand signals ───────────────────────────
        demand_signals = self._check_demand_signals(niche)

        # ── Layer 3: Real pricing data ─────────────────────────────
        pricing_data = self._research_pricing(niche)

        # ── Layer 4: LLM synthesis ─────────────────────────────────
        prior = self.ask_knowledge(topic=niche, category="market_research")
        prior_context = ""
        if prior:
            prior_context = "\nPrior research:\n" + "\n".join(
                f"- {p['content'][:200]}" for p in prior[:3]
            )

        web_context = self._format_research_data(market_data, demand_signals, pricing_data)

        result = self.think_json(
            f"Analyze the market for: {niche}\n\n"
            f"REAL WEB DATA:\n{web_context}\n"
            f"{prior_context}\n\n"
            "Estimate realistically using the REAL data above — monAI is an AI system "
            "with limited starting capital. We need niches where an automated system can compete.\n\n"
            "Base your market size estimates on the real data collected, not guesses.\n\n"
            "Return JSON: {\"niche\": str, \"market_size\": str (e.g. '$50M TAM'), "
            "\"target_customer\": str, \"willingness_to_pay\": str, "
            "\"demand_signals\": [str], \"barriers_to_entry\": [str], "
            "\"viable\": bool, \"estimated_monthly_revenue\": float, "
            "\"confidence\": float (0-1), \"reasoning\": str, "
            "\"data_sources\": [str]}"
        )

        if result.get("viable"):
            self.share_knowledge(
                "market_research", niche, str(result),
                confidence=result.get("confidence", 0.5),
                tags=["market", niche],
            )

        self.log_action("market_research", niche,
                        f"viable={result.get('viable')} "
                        f"rev={result.get('estimated_monthly_revenue')} "
                        f"sources={len(result.get('data_sources', []))}")

        return result

    # ── Real data collection methods ───────────────────────────────

    def _search_market_size(self, niche: str) -> dict[str, Any]:
        """Search for real market size data from reports and analyses."""
        try:
            return self.search_web(
                query=f"{niche} market size TAM revenue 2024 2025 report",
                extraction_prompt=(
                    f"Find real market size data for the '{niche}' market. "
                    "Extract: total addressable market (TAM), growth rate, "
                    "key market reports mentioned, geographic breakdown if available. "
                    "Return: {\"tam\": str, \"growth_rate\": str, "
                    "\"report_sources\": [str], \"key_stats\": [str], "
                    "\"year\": str}"
                ),
                num_results=5,
            )
        except Exception as e:
            logger.warning(f"Market size search failed for '{niche}': {e}")
            return {"error": str(e)}

    def _check_demand_signals(self, niche: str) -> dict[str, Any]:
        """Check real demand signals from job boards and freelancer platforms."""
        try:
            return self.search_web(
                query=f"{niche} jobs freelance gigs demand Upwork Fiverr",
                extraction_prompt=(
                    f"Find demand signals for '{niche}' services/products. "
                    "Look for: number of job postings, freelance gig demand, "
                    "common project types, typical budgets. "
                    "Return: {\"job_signals\": [str], \"freelance_demand\": str, "
                    "\"typical_budgets\": [str], \"common_project_types\": [str], "
                    "\"demand_level\": \"low\"|\"medium\"|\"high\"}"
                ),
                num_results=3,
            )
        except Exception as e:
            logger.warning(f"Demand signal check failed for '{niche}': {e}")
            return {"error": str(e)}

    def _research_pricing(self, niche: str) -> dict[str, Any]:
        """Research real pricing from existing solutions in the niche."""
        try:
            return self.search_web(
                query=f"{niche} pricing plans cost comparison 2025",
                extraction_prompt=(
                    f"Find real pricing data for products/services in '{niche}'. "
                    "Extract: price ranges, common pricing models (subscription, "
                    "one-time, per-use), what the cheapest and most expensive "
                    "options cost. "
                    "Return: {\"price_range\": str, \"pricing_models\": [str], "
                    "\"cheapest\": str, \"most_expensive\": str, "
                    "\"average_price\": str, \"examples\": [{\"name\": str, \"price\": str}]}"
                ),
                num_results=3,
            )
        except Exception as e:
            logger.warning(f"Pricing research failed for '{niche}': {e}")
            return {"error": str(e)}

    def _format_research_data(self, market_data: dict, demand_signals: dict,
                               pricing_data: dict) -> str:
        """Format all research data into readable context for LLM."""
        parts = []

        if market_data and not market_data.get("error"):
            parts.append(f"Market size data:\n{json.dumps(market_data, default=str)[:600]}")

        if demand_signals and not demand_signals.get("error"):
            parts.append(f"Demand signals:\n{json.dumps(demand_signals, default=str)[:600]}")

        if pricing_data and not pricing_data.get("error"):
            parts.append(f"Pricing data:\n{json.dumps(pricing_data, default=str)[:600]}")

        return "\n\n".join(parts) if parts else "Limited web data available."
