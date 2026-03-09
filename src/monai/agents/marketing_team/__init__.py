"""Marketing Team — executes actual marketing campaigns.

Architecture:
  MarketingDirector (coordinator)
  ├── ContentMarketer (SEO content, blog posts, social content)
  ├── GrowthHacker (viral loops, referrals, product-led growth)
  └── OutreachSpecialist (cold email, partnerships, influencer outreach)

Unlike the research team that discovers opportunities, the marketing team
EXECUTES campaigns to acquire users, clients, and revenue.
"""

from __future__ import annotations

import logging
from typing import Any

from monai.agents.base import BaseAgent
from monai.agents.marketing_team.content_marketer import ContentMarketer
from monai.agents.marketing_team.growth_hacker import GrowthHacker
from monai.agents.marketing_team.outreach_specialist import OutreachSpecialist
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

MARKETING_TEAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS marketing_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    campaign_type TEXT NOT NULL,       -- content, growth, outreach, launch
    strategy_name TEXT,                -- which strategy this supports
    status TEXT DEFAULT 'planned',     -- planned, active, paused, completed
    channel TEXT,                      -- seo, social, email, partnership, product
    target_audience TEXT,
    budget_eur REAL DEFAULT 0,
    spent_eur REAL DEFAULT 0,
    leads_generated INTEGER DEFAULT 0,
    conversions INTEGER DEFAULT 0,
    revenue_attributed REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS marketing_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER REFERENCES marketing_campaigns(id),
    content_type TEXT NOT NULL,        -- blog_post, social_post, email_sequence, landing_page
    title TEXT,
    body TEXT,
    platform TEXT,
    status TEXT DEFAULT 'draft',       -- draft, published, scheduled
    engagement_score REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS marketing_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER REFERENCES marketing_campaigns(id),
    metric_date TEXT NOT NULL,
    impressions INTEGER DEFAULT 0,
    clicks INTEGER DEFAULT 0,
    leads INTEGER DEFAULT 0,
    conversions INTEGER DEFAULT 0,
    cost_eur REAL DEFAULT 0,
    revenue_eur REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class MarketingTeam(BaseAgent):
    """Marketing coordinator — plans and executes campaigns across channels."""

    name = "marketing_team"
    description = (
        "Coordinates marketing specialists to acquire users and clients. "
        "Runs content marketing, growth hacking, and outreach campaigns."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.content_marketer = ContentMarketer(config, db, llm)
        self.growth_hacker = GrowthHacker(config, db, llm)
        self.outreach_specialist = OutreachSpecialist(config, db, llm)
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(MARKETING_TEAM_SCHEMA)

    def plan(self) -> list[str]:
        return [
            "Review active strategies that need marketing support",
            "Plan campaigns per strategy (content, growth, outreach)",
            "Execute campaigns via specialist agents",
            "Track metrics and optimize",
        ]

    def run(self, target_strategy: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """Run a marketing cycle.

        Args:
            target_strategy: Optional specific strategy to market for.
                            If None, markets for all active strategies.
        """
        # Step 1: Determine what needs marketing
        campaigns = self._plan_campaigns(target_strategy)

        # Step 2: Execute via specialists
        results = {
            "campaigns_planned": len(campaigns),
            "content_produced": 0,
            "growth_experiments": 0,
            "outreach_sent": 0,
        }

        for campaign in campaigns:
            ctype = campaign.get("campaign_type", "")

            if ctype == "content":
                content_result = self.content_marketer.run(
                    campaign=campaign,
                    strategy=campaign.get("strategy_name", ""),
                )
                results["content_produced"] += content_result.get("pieces_created", 0)

            elif ctype == "growth":
                growth_result = self.growth_hacker.run(
                    campaign=campaign,
                    strategy=campaign.get("strategy_name", ""),
                )
                results["growth_experiments"] += growth_result.get("experiments_launched", 0)

            elif ctype == "outreach":
                outreach_result = self.outreach_specialist.run(
                    campaign=campaign,
                    strategy=campaign.get("strategy_name", ""),
                )
                results["outreach_sent"] += outreach_result.get("messages_sent", 0)

        # Step 3: Update metrics for active campaigns
        self._update_campaign_metrics()

        self.journal("marketing_cycle", f"Executed {len(campaigns)} campaigns", results)
        return results

    def _plan_campaigns(self, target_strategy: str | None) -> list[dict[str, Any]]:
        """Plan marketing campaigns based on strategy needs."""
        # Get active strategies from knowledge base
        strategies_knowledge = self.ask_knowledge(category="opportunity")

        context = ""
        if strategies_knowledge:
            context = "\n".join(
                f"- {k['topic']}: {k['content'][:150]}" for k in strategies_knowledge[:5]
            )

        focus = f"Focus on strategy: {target_strategy}" if target_strategy else ""

        plan = self.think_json(
            f"Plan marketing campaigns for our active strategies.\n"
            f"{focus}\n\n"
            f"Available intelligence:\n{context}\n\n"
            "For each campaign, choose the best channel and type. "
            "Return JSON: {{\"campaigns\": [{{\"name\": str, "
            "\"campaign_type\": \"content\"|\"growth\"|\"outreach\", "
            "\"strategy_name\": str, \"channel\": str, "
            "\"target_audience\": str, \"key_message\": str, "
            "\"budget_eur\": float}}]}}"
        )

        campaigns = plan.get("campaigns", [])

        # Store planned campaigns
        for c in campaigns:
            self.db.execute_insert(
                "INSERT INTO marketing_campaigns "
                "(name, campaign_type, strategy_name, channel, target_audience, budget_eur) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (c.get("name", ""), c.get("campaign_type", "content"),
                 c.get("strategy_name", ""), c.get("channel", ""),
                 c.get("target_audience", ""), c.get("budget_eur", 0)),
            )

        self.log_action("plan_campaigns", f"Planned {len(campaigns)} campaigns")
        return campaigns

    def _update_campaign_metrics(self):
        """Update metrics for all active campaigns."""
        active = self.db.execute(
            "SELECT * FROM marketing_campaigns WHERE status = 'active'"
        )
        for campaign in active:
            # Check for results (leads, conversions, revenue)
            leads = self.db.execute(
                "SELECT COUNT(*) as cnt FROM marketing_metrics "
                "WHERE campaign_id = ?", (campaign["id"],)
            )
            total_leads = leads[0]["cnt"] if leads else 0
            if total_leads > 0:
                self.db.execute_insert(
                    "UPDATE marketing_campaigns SET leads_generated = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (total_leads, campaign["id"]),
                )

    # ── Campaign for specific product/strategy ───────────────

    def launch_campaign(self, strategy_name: str, product_description: str,
                        budget: float = 0) -> dict[str, Any]:
        """Launch a marketing campaign for a specific product/strategy."""
        campaign_plan = self.think_json(
            f"Design a comprehensive marketing campaign for:\n"
            f"Strategy: {strategy_name}\n"
            f"Product: {product_description}\n"
            f"Budget: €{budget}\n\n"
            "Return JSON: {{\"campaign_name\": str, \"phases\": ["
            "{{\"phase\": str, \"type\": \"content\"|\"growth\"|\"outreach\", "
            "\"actions\": [str], \"expected_result\": str}}], "
            "\"total_expected_leads\": int, \"timeline_days\": int}}"
        )

        campaign_id = self.db.execute_insert(
            "INSERT INTO marketing_campaigns "
            "(name, campaign_type, strategy_name, status, budget_eur) "
            "VALUES (?, 'launch', ?, 'active', ?)",
            (campaign_plan.get("campaign_name", strategy_name),
             strategy_name, budget),
        )

        self.log_action("launch_campaign", strategy_name,
                        f"phases={len(campaign_plan.get('phases', []))}")

        return {
            "campaign_id": campaign_id,
            "plan": campaign_plan,
        }

    # ── Reporting ────────────────────────────────────────────

    def get_campaign_performance(self) -> list[dict[str, Any]]:
        """Get performance metrics for all campaigns."""
        rows = self.db.execute(
            "SELECT * FROM marketing_campaigns ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]

    def get_roi_by_campaign(self) -> list[dict[str, Any]]:
        """Campaign ROI: revenue_attributed / spent."""
        rows = self.db.execute(
            "SELECT name, campaign_type, strategy_name, spent_eur, "
            "revenue_attributed, leads_generated, conversions, "
            "CASE WHEN spent_eur > 0 THEN revenue_attributed / spent_eur ELSE 0 END as roi "
            "FROM marketing_campaigns WHERE spent_eur > 0 ORDER BY roi DESC"
        )
        return [dict(r) for r in rows]
