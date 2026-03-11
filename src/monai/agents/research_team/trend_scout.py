"""TrendScout — discovers emerging trends and timing signals.

Real autonomous capabilities:
- Browses Product Hunt for trending products via browse_and_extract
- Scrapes Hacker News front page for tech trends
- Checks Reddit for emerging niches (r/SaaS, r/startups, r/Entrepreneur)
- Browses Google Trends for search volume data
- Combines real web data with LLM analysis and knowledge base
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

# ── Real data sources for trend discovery ──────────────────────────
TREND_SOURCES = {
    "product_hunt": {
        "url": "https://www.producthunt.com/",
        "prompt": (
            "Extract the top 10 trending products from this page. "
            "For each: {\"name\": str, \"tagline\": str, \"category\": str, "
            "\"upvotes\": int or estimate, \"topics\": [str]}"
        ),
    },
    "hacker_news": {
        "url": "https://news.ycombinator.com/",
        "prompt": (
            "Extract the top 15 stories from the Hacker News front page. "
            "For each: {\"title\": str, \"url\": str, \"points\": int, "
            "\"comments\": int, \"category_guess\": str}. "
            "Focus on stories related to: AI, SaaS, startups, developer tools, "
            "automation, business, or side projects."
        ),
    },
    "reddit_saas": {
        "url": "https://www.reddit.com/r/SaaS/top/?t=week",
        "prompt": (
            "Extract the top 10 posts from this subreddit. "
            "For each: {\"title\": str, \"upvotes\": int, \"comments\": int, "
            "\"flair\": str, \"summary\": str}. "
            "Focus on posts about market opportunities, tool gaps, or success stories."
        ),
    },
    "reddit_startups": {
        "url": "https://www.reddit.com/r/startups/top/?t=week",
        "prompt": (
            "Extract the top 10 posts about startup ideas, market validation, "
            "or emerging trends. For each: {\"title\": str, \"upvotes\": int, "
            "\"key_insight\": str}"
        ),
    },
}


class TrendScout(BaseAgent):
    """Spots emerging trends before they mature — timing is everything.

    Combines real web scraping from Product Hunt, Hacker News, and Reddit
    with LLM analysis to identify actionable trends.
    """

    name = "trend_scout"
    description = "Discovers emerging market trends and timing signals for new opportunities."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return [
            "Browse Product Hunt for trending products",
            "Scrape Hacker News for tech trend signals",
            "Check Reddit for emerging business niches",
            "Search Google Trends for rising queries",
            "Analyze agent knowledge base for pattern signals",
            "Synthesize real data into scored opportunities",
        ]

    def run(self, focus_areas: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Find trending opportunities using real web data + LLM synthesis.

        Layer 1: Real web browsing for live trend data
        Layer 2: Knowledge base signals from other agents
        Layer 3: LLM synthesis of all data into scored opportunities
        """
        # ── Layer 1: Real web browsing ─────────────────────────────
        web_signals = self._scrape_trend_sources()

        # Google Trends search for focus areas
        google_trends_data = {}
        if focus_areas:
            for area in focus_areas[:3]:
                google_trends_data[area] = self._check_google_trends(area)

        # ── Layer 2: Knowledge base signals ────────────────────────
        existing_knowledge = self.ask_knowledge(category="opportunity")
        knowledge_context = "\n".join(
            f"- {k['topic']}: {k['content'][:200]}"
            for k in existing_knowledge[:10]
        )

        # ── Layer 3: LLM synthesis ────────────────────────────────
        focus_prompt = ""
        if focus_areas:
            focus_prompt = f"\nFocus especially on: {', '.join(focus_areas)}"

        # Build real data context
        web_context = self._format_web_signals(web_signals)
        trends_context = ""
        if google_trends_data:
            trends_context = "\nGoogle Trends data:\n" + json.dumps(
                google_trends_data, default=str)[:1000]

        result = self.think_json(
            "You are a trend scout for an autonomous AI business system. "
            "I've gathered REAL data from the web. Analyze it and identify "
            "3-5 emerging opportunities that are trending UP and can "
            "generate revenue within 1-6 months.\n\n"
            f"REAL WEB DATA:\n{web_context}\n"
            f"{trends_context}\n\n"
            f"Existing agent knowledge:\n{knowledge_context}\n{focus_prompt}\n\n"
            "Cross-reference the real web data with what you know. "
            "Only recommend opportunities backed by REAL signals from the data above.\n\n"
            "Return JSON: {\"opportunities\": [{\"niche\": str, \"trend_direction\": "
            "\"rising\"|\"peaking\"|\"emerging\", \"timing_score\": float (0-1), "
            "\"reasoning\": str, \"evidence_sources\": [str]}]}"
        )

        opportunities = result.get("opportunities", [])

        self.log_action("trend_scan",
                        f"Found {len(opportunities)} trends from {len(web_signals)} sources",
                        str([o.get("niche") for o in opportunities]))

        return {
            "opportunities": opportunities,
            "sources_scraped": list(web_signals.keys()),
            "google_trends_checked": list(google_trends_data.keys()),
        }

    # ── Real web scraping methods ──────────────────────────────────

    def _scrape_trend_sources(self) -> dict[str, Any]:
        """Browse real trend sources and extract structured data."""
        results = {}
        for source_name, source_config in TREND_SOURCES.items():
            try:
                data = self.browse_and_extract(
                    url=source_config["url"],
                    extraction_prompt=source_config["prompt"],
                )
                if data.get("status") != "error":
                    results[source_name] = data
                    self.log_action("trend_scrape", source_name, f"data_keys={list(data.keys())}")
                else:
                    logger.warning(f"Failed to scrape {source_name}: {data.get('reason', '?')}")
            except Exception as e:
                logger.warning(f"Error scraping {source_name}: {e}")
                results[source_name] = {"status": "error", "error": str(e)}
        return results

    def _check_google_trends(self, query: str) -> dict[str, Any]:
        """Check Google Trends for a specific query via real web search."""
        try:
            return self.search_web(
                query=f"{query} trend 2024 2025",
                extraction_prompt=(
                    f"Find data about the search trend for '{query}'. "
                    "Extract: is the search volume rising, falling, or stable? "
                    "Any notable spikes? What's the estimated interest level? "
                    "Return: {\"trend_direction\": str, \"interest_level\": str, "
                    "\"notable_signals\": [str]}"
                ),
                num_results=3,
            )
        except Exception as e:
            logger.warning(f"Google Trends check failed for '{query}': {e}")
            return {"status": "error", "error": str(e)}

    def _format_web_signals(self, web_signals: dict[str, Any]) -> str:
        """Format raw web scraping results into readable context for LLM."""
        parts = []
        for source, data in web_signals.items():
            if data.get("status") == "error":
                continue
            # Truncate to keep prompt manageable
            data_str = json.dumps(data, default=str)[:800]
            parts.append(f"[{source}]:\n{data_str}")
        return "\n\n".join(parts) if parts else "No web data available."
