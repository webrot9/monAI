"""Social media management strategy agent.

Manages social media accounts for SMBs as a paid service.
Recurring monthly retainers. Uses Humanizer for content quality.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

SMM_SCHEMA = """
CREATE TABLE IF NOT EXISTS smm_clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_name TEXT NOT NULL,
    industry TEXT,
    platforms TEXT NOT NULL,                  -- JSON list: ["instagram", "twitter", "linkedin"]
    monthly_retainer REAL DEFAULT 0.0,
    content_per_week INTEGER DEFAULT 5,
    brand_voice TEXT,                        -- style profile name for Humanizer
    status TEXT DEFAULT 'prospect',          -- prospect, onboarding, active, paused, churned
    contract_start TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS social_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES smm_clients(id),
    platform TEXT NOT NULL,
    content TEXT NOT NULL,
    media_description TEXT,                  -- what image/video to create
    hashtags TEXT,
    scheduled_for TIMESTAMP,
    status TEXT DEFAULT 'draft',             -- draft, approved, scheduled, posted, failed
    engagement_likes INTEGER DEFAULT 0,
    engagement_comments INTEGER DEFAULT 0,
    engagement_shares INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class SocialMediaAgent(BaseAgent):
    name = "social_media"
    description = (
        "Manages social media accounts for small businesses. Creates content, "
        "schedules posts, monitors engagement, and reports to clients. "
        "Recurring monthly retainer model."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(SMM_SCHEMA)

    def plan(self) -> list[str]:
        clients = self.db.execute("SELECT status, COUNT(*) as c FROM smm_clients GROUP BY status")
        stats = {r["status"]: r["c"] for r in clients}
        plan = self.think_json(
            f"SMM client stats: {json.dumps(stats)}. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: find_clients, create_content_batch, schedule_posts, "
            "analyze_engagement, report_to_clients, optimize_strategy.",
        )
        return plan.get("steps", ["find_clients"])

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting social media cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "find_clients":
                results["prospecting"] = self._find_clients()
            elif step == "create_content_batch":
                results["content"] = self._create_content_batch()
            elif step == "analyze_engagement":
                results["engagement"] = self._analyze_engagement()
            elif step == "report_to_clients":
                results["reports"] = self._report_to_clients()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _find_clients(self) -> dict[str, Any]:
        """Find SMBs that need social media management."""
        return self.think_json(
            "Research 5 types of small businesses that need social media management. "
            "Focus on:\n"
            "- Businesses too busy to post themselves\n"
            "- Industries where social presence directly drives sales\n"
            "- Businesses with budget ($300-1500/month range)\n"
            "- Low-complexity content (not requiring real photos constantly)\n\n"
            "Return: {\"prospects\": [{\"business_type\": str, \"industry\": str, "
            "\"platforms\": [str], \"content_types\": [str], "
            "\"typical_retainer\": float, \"pitch_angle\": str, "
            "\"where_to_find\": str}]}"
        )

    def _create_content_batch(self) -> dict[str, Any]:
        """Create a batch of social media posts for active clients."""
        clients = self.db.execute(
            "SELECT * FROM smm_clients WHERE status = 'active'"
        )
        if not clients:
            return {"status": "no_active_clients"}

        total_posts = 0
        for client in clients:
            c = dict(client)
            platforms = json.loads(c["platforms"])
            posts_per_platform = c["content_per_week"]

            for platform in platforms:
                batch = self.think_json(
                    f"Create {posts_per_platform} {platform} posts for:\n"
                    f"Client: {c['client_name']}\n"
                    f"Industry: {c['industry']}\n"
                    f"Brand voice: {c.get('brand_voice', 'professional and engaging')}\n\n"
                    "Return: {\"posts\": [{\"content\": str, \"hashtags\": [str], "
                    "\"media_suggestion\": str, \"best_time\": str}]}"
                )

                for post in batch.get("posts", []):
                    self.db.execute_insert(
                        "INSERT INTO social_posts (client_id, platform, content, "
                        "hashtags, media_description, status) VALUES (?, ?, ?, ?, ?, 'draft')",
                        (c["id"], platform, post.get("content", ""),
                         json.dumps(post.get("hashtags", [])),
                         post.get("media_suggestion", "")),
                    )
                    total_posts += 1

        self.log_action("content_created", f"{total_posts} posts for {len(clients)} clients")
        return {"posts_created": total_posts, "clients_served": len(clients)}

    def _analyze_engagement(self) -> dict[str, Any]:
        """Analyze engagement metrics for posted content."""
        posts = self.db.execute(
            "SELECT client_id, platform, AVG(engagement_likes) as avg_likes, "
            "AVG(engagement_comments) as avg_comments, COUNT(*) as total "
            "FROM social_posts WHERE status = 'posted' "
            "GROUP BY client_id, platform"
        )
        return {"engagement_data": [dict(p) for p in posts]}

    def _report_to_clients(self) -> dict[str, Any]:
        """Generate performance reports for active clients."""
        clients = self.db.execute(
            "SELECT * FROM smm_clients WHERE status = 'active'"
        )
        reports_generated = 0
        for client in clients:
            c = dict(client)
            posts = self.db.execute(
                "SELECT platform, COUNT(*) as total, "
                "SUM(engagement_likes) as likes, SUM(engagement_comments) as comments "
                "FROM social_posts WHERE client_id = ? AND status = 'posted' "
                "GROUP BY platform",
                (c["id"],),
            )
            if posts:
                self.log_action("client_report", c["client_name"],
                                json.dumps([dict(p) for p in posts], default=str)[:500])
                reports_generated += 1

        return {"reports_generated": reports_generated}
