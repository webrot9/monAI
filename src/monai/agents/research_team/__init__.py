"""Market Research Team — discovers and validates money-making opportunities.

Architecture:
  ResearchDirector (coordinator)
  ├── MarketResearcher (niche/demand analysis)
  ├── CompetitorAnalyst (competitive landscape)
  └── TrendScout (emerging trends and timing)

Each researcher figures out their own tools and methods:
- Web scraping, API calls, LLM analysis, knowledge base queries
- They share findings via SharedMemory and report back to the director
- The director synthesizes insights into actionable briefs for the orchestrator
"""

from __future__ import annotations

import logging
from typing import Any

from monai.agents.base import BaseAgent
from monai.agents.research_team.competitor_analyst import CompetitorAnalyst
from monai.agents.research_team.market_researcher import MarketResearcher
from monai.agents.research_team.trend_scout import TrendScout
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

RESEARCH_TEAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    brief_type TEXT NOT NULL,        -- opportunity, competitive, trend, market_size
    niche TEXT,
    findings TEXT NOT NULL,
    recommended_action TEXT,         -- pursue, monitor, skip
    confidence REAL DEFAULT 0.5,
    revenue_estimate REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS research_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    researcher TEXT NOT NULL,
    task_type TEXT NOT NULL,
    query TEXT NOT NULL,
    status TEXT DEFAULT 'pending',   -- pending, in_progress, completed, failed
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);
"""


class ResearchTeam(BaseAgent):
    """Market research coordinator — dispatches researchers, synthesizes findings."""

    name = "research_team"
    description = (
        "Coordinates market researchers to discover profitable niches, analyze "
        "competition, and spot trends before they mature."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.market_researcher = MarketResearcher(config, db, llm)
        self.competitor_analyst = CompetitorAnalyst(config, db, llm)
        self.trend_scout = TrendScout(config, db, llm)
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(RESEARCH_TEAM_SCHEMA)

    def plan(self) -> list[str]:
        return [
            "Identify research priorities from orchestrator signals",
            "Dispatch trend scout to find emerging opportunities",
            "Dispatch market researcher to size promising niches",
            "Dispatch competitor analyst to assess landscape",
            "Synthesize findings into actionable research briefs",
        ]

    def run(self, focus_areas: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Run a full research cycle.

        Args:
            focus_areas: Optional list of niches/directions to prioritize.
                         If empty, researchers determine their own focus.
        """
        # Step 1: Trend scout finds what's hot
        trends = self.trend_scout.run(focus_areas=focus_areas)

        # Step 2: Market researcher sizes the top opportunities
        top_trends = trends.get("opportunities", [])[:3]
        market_analyses = []
        for trend in top_trends:
            niche = trend if isinstance(trend, str) else trend.get("niche", "")
            if niche:
                analysis = self.market_researcher.run(niche=niche)
                market_analyses.append(analysis)

        # Step 3: Competitor analyst checks the landscape for viable ones
        viable = [
            a for a in market_analyses
            if a.get("viable", False)
        ]
        comp_analyses = []
        for market in viable:
            niche = market.get("niche", "")
            if niche:
                comp = self.competitor_analyst.run(niche=niche)
                comp_analyses.append(comp)

        # Step 4: Synthesize into research briefs
        briefs = self._synthesize_briefs(trends, market_analyses, comp_analyses)

        # Share key findings with all agents
        for brief in briefs:
            if brief.get("recommended_action") == "pursue":
                self.share_knowledge(
                    "opportunity", brief["title"],
                    brief["findings"],
                    confidence=brief.get("confidence", 0.5),
                    tags=["research", brief.get("niche", "")],
                )

        self.journal("research_cycle", f"Completed research: {len(briefs)} briefs", {
            "trends_found": len(top_trends),
            "viable_markets": len(viable),
            "briefs_produced": len(briefs),
        })

        return {
            "trends": len(top_trends),
            "markets_analyzed": len(market_analyses),
            "competitors_analyzed": len(comp_analyses),
            "briefs": briefs,
        }

    def _synthesize_briefs(self, trends: dict, markets: list[dict],
                           competitors: list[dict]) -> list[dict[str, Any]]:
        """Combine research from all three analysts into briefs."""
        briefs = []

        # Build context from all research
        context_parts = []
        if trends.get("opportunities"):
            context_parts.append(f"Trends: {trends['opportunities']}")
        for m in markets:
            context_parts.append(
                f"Market '{m.get('niche', '?')}': viable={m.get('viable')}, "
                f"size={m.get('market_size', '?')}"
            )
        for c in competitors:
            context_parts.append(
                f"Competition in '{c.get('niche', '?')}': "
                f"intensity={c.get('competition_level', '?')}, "
                f"gaps={c.get('gaps', [])}"
            )

        if not context_parts:
            return []

        synthesis = self.think_json(
            "Based on the following research, produce a list of research briefs. "
            "Return JSON: {\"briefs\": [{\"title\": str, \"niche\": str, "
            "\"findings\": str, \"recommended_action\": \"pursue\"|\"monitor\"|\"skip\", "
            "\"confidence\": float, \"revenue_estimate\": float}]}.\n\n"
            + "\n".join(context_parts)
        )

        for b in synthesis.get("briefs", []):
            self.db.execute_insert(
                "INSERT INTO research_briefs "
                "(title, brief_type, niche, findings, recommended_action, "
                "confidence, revenue_estimate) VALUES (?, 'opportunity', ?, ?, ?, ?, ?)",
                (b.get("title", ""), b.get("niche", ""),
                 b.get("findings", ""), b.get("recommended_action", "monitor"),
                 b.get("confidence", 0.5), b.get("revenue_estimate", 0)),
            )
            briefs.append(b)

        return briefs

    def research_specific(self, topic: str) -> dict[str, Any]:
        """On-demand research for a specific topic (called by orchestrator)."""
        market = self.market_researcher.run(niche=topic)
        comp = self.competitor_analyst.run(niche=topic)
        briefs = self._synthesize_briefs(
            {"opportunities": [topic]}, [market], [comp]
        )
        return {
            "topic": topic,
            "market": market,
            "competition": comp,
            "briefs": briefs,
        }

    def get_recent_briefs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get most recent research briefs."""
        rows = self.db.execute(
            "SELECT * FROM research_briefs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def get_pursue_briefs(self) -> list[dict[str, Any]]:
        """Get briefs recommended for pursuit."""
        rows = self.db.execute(
            "SELECT * FROM research_briefs WHERE recommended_action = 'pursue' "
            "ORDER BY revenue_estimate DESC"
        )
        return [dict(r) for r in rows]
