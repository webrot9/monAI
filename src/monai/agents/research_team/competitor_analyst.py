"""CompetitorAnalyst — maps the competitive landscape for a niche.

Real autonomous capabilities:
- Searches the web for actual competitors in a niche
- Scrapes competitor websites for pricing, features, positioning
- Checks review sites (G2, Capterra, Trustpilot) for real sentiment data
- Combines real web data with LLM reasoning and knowledge base
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


class CompetitorAnalyst(BaseAgent):
    """Maps who's already in a market and where the gaps are.

    Uses real web scraping to discover competitors, their pricing,
    and customer sentiment — not just LLM guessing.
    """

    name = "competitor_analyst"
    description = "Analyzes competitive landscape, identifies gaps, and assesses entry difficulty."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return [
            "Search the web for real competitors in the niche",
            "Scrape competitor websites for pricing and features",
            "Check review sites for customer sentiment",
            "Analyze strengths, weaknesses, and gaps",
            "Assess competitive moat and entry difficulty",
        ]

    def run(self, niche: str = "", **kwargs: Any) -> dict[str, Any]:
        """Analyze competition in a specific niche.

        Layer 1: Real web search to discover competitors
        Layer 2: Real scraping of competitor pricing/features pages
        Layer 3: Review site sentiment analysis
        Layer 4: LLM synthesis with knowledge base
        """
        if not niche:
            return {"competition_level": "unknown", "gaps": []}

        # ── Layer 1: Discover real competitors via web search ──────
        competitor_search = self._search_competitors(niche)

        # ── Layer 2: Scrape competitor details ─────────────────────
        competitor_details = []
        competitors_found = competitor_search.get("competitors", [])
        if isinstance(competitors_found, list):
            for comp in competitors_found[:5]:  # Top 5 competitors max
                url = comp.get("url") or comp.get("website")
                name = comp.get("name", "")
                if url:
                    details = self._scrape_competitor_site(name, url)
                    competitor_details.append(details)

        # ── Layer 3: Check review sites for sentiment ──────────────
        review_data = self._check_review_sites(niche)

        # ── Layer 4: LLM synthesis with all real data ──────────────
        prior = self.ask_knowledge(topic=niche, category="competitive_intel")
        prior_context = ""
        if prior:
            prior_context = "\nPrior intel:\n" + "\n".join(
                f"- {p['content'][:200]}" for p in prior[:3]
            )

        # Build real data context
        web_context = self._format_competitor_data(
            competitor_search, competitor_details, review_data
        )

        result = self.think_json(
            f"Analyze the competitive landscape for: {niche}\n\n"
            f"REAL WEB DATA:\n{web_context}\n"
            f"{prior_context}\n\n"
            "Consider: monAI is an automated AI system. We have advantages in speed, "
            "cost, and 24/7 operation, but disadvantages in brand trust and human touch.\n\n"
            "Base your analysis on the REAL competitor data above, not guesses.\n\n"
            "Return JSON: {\"niche\": str, \"competitors\": [{\"name\": str, "
            "\"url\": str, \"pricing\": str, \"strengths\": [str], \"weaknesses\": [str]}], "
            "\"competition_level\": \"low\"|\"medium\"|\"high\"|\"saturated\", "
            "\"gaps\": [str], \"moat_difficulty\": \"easy\"|\"medium\"|\"hard\", "
            "\"monai_advantages\": [str], \"entry_strategy\": str, \"confidence\": float}"
        )

        if result.get("gaps"):
            self.share_knowledge(
                "competitive_intel", niche,
                f"Competition: {result.get('competition_level')}. "
                f"Gaps: {', '.join(result.get('gaps', []))}. "
                f"Top competitors: {', '.join(c.get('name', '?') for c in result.get('competitors', [])[:3])}",
                confidence=result.get("confidence", 0.5),
                tags=["competition", niche],
            )

        self.log_action("competitor_analysis", niche,
                        f"level={result.get('competition_level')} "
                        f"competitors={len(result.get('competitors', []))} "
                        f"gaps={len(result.get('gaps', []))}")

        return result

    # ── Real web scraping methods ──────────────────────────────────

    def _search_competitors(self, niche: str) -> dict[str, Any]:
        """Search the web for real competitors in a niche."""
        try:
            return self.search_web(
                query=f"best {niche} tools software companies 2025",
                extraction_prompt=(
                    f"Find the main competitors/companies in the '{niche}' space. "
                    "For each competitor extract: {\"name\": str, \"url\": str, "
                    "\"description\": str, \"apparent_size\": \"startup\"|\"scaleup\"|\"enterprise\"}. "
                    "Return: {\"competitors\": [...]}"
                ),
                num_results=5,
            )
        except Exception as e:
            logger.warning(f"Competitor search failed for '{niche}': {e}")
            return {"competitors": [], "error": str(e)}

    def _scrape_competitor_site(self, name: str, url: str) -> dict[str, Any]:
        """Scrape a competitor's website for pricing and features."""
        try:
            # Try pricing page first (most valuable intel)
            pricing_url = url.rstrip("/") + "/pricing"
            data = self.browse_and_extract(
                url=pricing_url,
                extraction_prompt=(
                    f"Extract pricing information for {name}. "
                    "Find: plan names, prices, features per plan, free tier details. "
                    "Return: {\"has_pricing_page\": bool, \"plans\": ["
                    "{\"name\": str, \"price\": str, \"features\": [str]}], "
                    "\"free_tier\": bool, \"lowest_paid_price\": str}"
                ),
            )
            if data.get("status") == "error":
                # Fall back to main page
                data = self.browse_and_extract(
                    url=url,
                    extraction_prompt=(
                        f"Extract key information about {name}: "
                        "what they do, main features, target audience, "
                        "any visible pricing or CTAs. "
                        "Return: {\"description\": str, \"main_features\": [str], "
                        "\"target_audience\": str, \"cta\": str}"
                    ),
                )
            data["competitor_name"] = name
            data["competitor_url"] = url
            self.log_action("scrape_competitor", name, f"url={url}")
            return data
        except Exception as e:
            logger.warning(f"Failed to scrape {name} ({url}): {e}")
            return {"competitor_name": name, "competitor_url": url, "error": str(e)}

    def _check_review_sites(self, niche: str) -> dict[str, Any]:
        """Check G2/Capterra for real review data about the niche."""
        try:
            return self.search_web(
                query=f"{niche} software reviews G2 Capterra ratings 2025",
                extraction_prompt=(
                    f"Find review/rating data for tools in the '{niche}' space. "
                    "Extract: top rated tools, average ratings, common complaints, "
                    "what users wish was better. "
                    "Return: {\"top_rated\": [{\"name\": str, \"rating\": float, "
                    "\"review_count\": int}], \"common_complaints\": [str], "
                    "\"user_wishes\": [str]}"
                ),
                num_results=3,
            )
        except Exception as e:
            logger.warning(f"Review site check failed for '{niche}': {e}")
            return {"error": str(e)}

    def _format_competitor_data(self, search_results: dict, details: list[dict],
                                review_data: dict) -> str:
        """Format all competitor data into readable context for LLM."""
        parts = []

        # Search results
        competitors = search_results.get("competitors", [])
        if competitors:
            parts.append(f"Competitors found ({len(competitors)}):\n" +
                         json.dumps(competitors, default=str)[:1000])

        # Detailed scraping results
        for d in details:
            if d.get("error"):
                continue
            name = d.get("competitor_name", "?")
            d_str = json.dumps(d, default=str)[:500]
            parts.append(f"Details for {name}:\n{d_str}")

        # Review data
        if review_data and not review_data.get("error"):
            parts.append(f"Review site data:\n{json.dumps(review_data, default=str)[:500]}")

        return "\n\n".join(parts) if parts else "Limited web data available."
