"""Social media management strategy agent.

Manages social media accounts for SMBs as a paid service.
Recurring monthly retainers. Uses Humanizer for content quality.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.social.api import create_platform_client, SocialAPIError
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

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
    post_id TEXT,                            -- platform post ID after posting
    post_url TEXT,                           -- URL to the live post
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

        # Deterministic progression
        if not stats:
            return ["find_clients"]
        if stats.get("prospect", 0) > 0:
            return ["find_clients"]  # Keep prospecting until onboarding
        if stats.get("onboarding", 0) > 0:
            return ["create_content_batch"]
        if stats.get("active", 0) > 0:
            return ["create_content_batch"]

        # All clients churned — find new ones
        return ["find_clients"]

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
        """Find REAL SMBs that need social media management by browsing actual platforms."""
        prospects = []

        # Search LinkedIn for small businesses posting infrequently
        linkedin_data = self.browse_and_extract(
            "https://www.linkedin.com/search/results/companies/?companySize=B&companySize=C&origin=FACETED_SEARCH",
            "Find small businesses (1-50 employees) that appear to have inactive or "
            "infrequent social media activity. For each business extract:\n"
            "- company_name: the business name\n"
            "- industry: what industry they are in\n"
            "- employee_count: approximate number of employees\n"
            "- linkedin_url: their LinkedIn profile URL\n"
            "- last_post_date: when they last posted (if visible)\n"
            "- posting_frequency: how often they post (daily/weekly/monthly/rarely)\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"businesses\": [...]}"
        )
        for biz in linkedin_data.get("businesses", []):
            biz["source"] = "linkedin"
            prospects.append(biz)

        # Search Twitter for businesses with low engagement
        twitter_data = self.search_web(
            "small business social media help needed site:twitter.com OR site:x.com",
            "Find real small businesses on Twitter/X that are struggling with social media "
            "or posting inconsistently. For each extract:\n"
            "- company_name: the business name\n"
            "- industry: what industry\n"
            "- twitter_handle: their @handle\n"
            "- follower_count: approximate followers\n"
            "- posting_frequency: how often they post\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"businesses\": [...]}"
        )
        for biz in twitter_data.get("businesses", []):
            biz["source"] = "twitter"
            prospects.append(biz)

        # Search for businesses actively looking for SMM help
        seeking_help = self.search_web(
            "looking for social media manager small business 2024 2025",
            "Find real job postings, forum threads, or classified ads from small businesses "
            "looking for social media management help. For each extract:\n"
            "- business_name: the business or poster name\n"
            "- industry: what industry\n"
            "- platform_url: where the listing was found\n"
            "- budget_mentioned: any budget they mentioned\n"
            "- requirements: what they need\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"listings\": [...]}"
        )
        for listing in seeking_help.get("listings", []):
            listing["source"] = "job_board"
            prospects.append(listing)

        # Store qualifying prospects in DB
        stored = 0
        for prospect in prospects:
            name = prospect.get("company_name") or prospect.get("business_name", "Unknown")
            industry = prospect.get("industry", "")
            if name and name != "Unknown":
                self.db.execute_insert(
                    "INSERT INTO smm_clients (client_name, industry, platforms, status) "
                    "VALUES (?, ?, ?, 'prospect')",
                    (name, industry, json.dumps(["twitter", "linkedin", "instagram"])),
                )
                stored += 1

        self.log_action("find_clients", f"Found {len(prospects)} prospects, stored {stored}")
        return {"prospects_found": len(prospects), "stored": stored, "prospects": prospects}

    def _create_content_batch(self) -> dict[str, Any]:
        """Create a batch of social media posts for active clients and POST them live."""
        clients = self.db.execute(
            "SELECT * FROM smm_clients WHERE status = 'active'"
        )
        if not clients:
            return {"status": "no_active_clients"}

        total_posts = 0
        total_posted = 0
        for client in clients:
            c = dict(client)
            platforms = json.loads(c["platforms"])
            posts_per_platform = c["content_per_week"]

            for platform in platforms:
                # Use LLM for content creation — this is legitimate creative work
                batch = self.think_json(
                    f"Create {posts_per_platform} {platform} posts for:\n"
                    f"Client: {c['client_name']}\n"
                    f"Industry: {c['industry']}\n"
                    f"Brand voice: {c.get('brand_voice', 'professional and engaging')}\n\n"
                    "Return: {\"posts\": [{\"content\": str, \"hashtags\": [str], "
                    "\"media_suggestion\": str, \"best_time\": str}]}"
                )

                for post in batch.get("posts", []):
                    content = post.get("content", "")
                    hashtags = post.get("hashtags", [])
                    full_content = content
                    if hashtags:
                        full_content += "\n\n" + " ".join(
                            f"#{h}" if not h.startswith("#") else h for h in hashtags
                        )

                    # Save draft to DB first
                    post_row_id = self.db.execute_insert(
                        "INSERT INTO social_posts (client_id, platform, content, "
                        "hashtags, media_description, status) VALUES (?, ?, ?, ?, ?, 'draft')",
                        (c["id"], platform, content,
                         json.dumps(hashtags),
                         post.get("media_suggestion", "")),
                    )
                    total_posts += 1

                    # Actually POST via the social API client
                    try:
                        creds = self.get_platform_credentials(platform)
                        if creds:
                            api_client = create_platform_client(
                                platform, self.config, creds
                            )
                            result = api_client.post(full_content)
                            # Update DB with live post info
                            self.db.execute(
                                "UPDATE social_posts SET status = 'posted', "
                                "post_id = ?, post_url = ? WHERE id = ?",
                                (result.get("post_id", ""),
                                 result.get("url", ""), post_row_id),
                            )
                            total_posted += 1
                            self.log_action(
                                "post_published", platform,
                                f"client={c['client_name']} post_id={result.get('post_id', '')}"
                            )
                        else:
                            # No credentials — use platform_action as fallback
                            result = self.platform_action(
                                platform,
                                f"Post the following content:\n\n{full_content}",
                                f"Posting on behalf of client: {c['client_name']}"
                            )
                            self.db.execute(
                                "UPDATE social_posts SET status = 'posted', "
                                "post_id = ?, post_url = ? WHERE id = ?",
                                (result.get("post_id", ""),
                                 result.get("url", ""), post_row_id),
                            )
                            total_posted += 1
                    except SocialAPIError as e:
                        self.db.execute(
                            "UPDATE social_posts SET status = 'failed' WHERE id = ?",
                            (post_row_id,),
                        )
                        self.log_action("post_failed", platform, str(e)[:300])
                        self.learn_from_error(e, f"Posting to {platform} for {c['client_name']}")
                    except Exception as e:
                        self.db.execute(
                            "UPDATE social_posts SET status = 'failed' WHERE id = ?",
                            (post_row_id,),
                        )
                        self.log_action("post_failed", platform, str(e)[:300])

        self.log_action(
            "content_created",
            f"{total_posts} drafted, {total_posted} posted for {len(clients)} clients"
        )
        return {
            "posts_created": total_posts,
            "posts_published": total_posted,
            "clients_served": len(clients),
        }

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
