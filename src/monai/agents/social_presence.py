"""Social Media Presence — manages monAI's own social accounts.

NOT a client service (that's strategies/social_media.py). This agent builds
monAI's brand, attracts inbound leads, establishes authority, and drives
traffic to our products/services.

Platforms: Twitter/X, LinkedIn, Reddit, Indie Hackers, Product Hunt.
Strategy: thought leadership + case studies + engagement + community building.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

SOCIAL_PRESENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS own_social_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL UNIQUE,     -- twitter, linkedin, reddit, indie_hackers, product_hunt
    username TEXT,
    profile_url TEXT,
    followers INTEGER DEFAULT 0,
    status TEXT DEFAULT 'planned',     -- planned, created, active, suspended
    bio TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS own_social_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    post_type TEXT NOT NULL,           -- thread, post, comment, reply, article
    content TEXT NOT NULL,
    topic TEXT,                        -- what this post is about
    target_audience TEXT,
    hashtags TEXT,
    status TEXT DEFAULT 'draft',       -- draft, scheduled, posted, failed
    engagement_likes INTEGER DEFAULT 0,
    engagement_comments INTEGER DEFAULT 0,
    engagement_shares INTEGER DEFAULT 0,
    engagement_clicks INTEGER DEFAULT 0,
    leads_generated INTEGER DEFAULT 0,
    posted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS content_calendar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_date TEXT NOT NULL,
    platform TEXT NOT NULL,
    post_type TEXT NOT NULL,
    topic TEXT NOT NULL,
    angle TEXT,                        -- specific angle or hook
    status TEXT DEFAULT 'planned',     -- planned, drafted, posted
    post_id INTEGER REFERENCES own_social_posts(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS social_engagement_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    action_type TEXT NOT NULL,         -- like, comment, reply, follow, dm, repost
    target_user TEXT,
    target_post TEXT,
    our_response TEXT,
    resulted_in_lead INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Platform-specific content strategies
PLATFORM_STRATEGIES = {
    "twitter": {
        "post_types": ["thread", "post", "reply"],
        "ideal_length": 280,
        "frequency": "2-3/day",
        "content_mix": {
            "value_posts": 0.4,       # tips, insights, how-tos
            "case_studies": 0.2,       # results, before/after
            "engagement": 0.2,         # questions, polls, hot takes
            "promotion": 0.1,          # product/service mentions
            "community": 0.1,          # replies, retweets, collabs
        },
    },
    "linkedin": {
        "post_types": ["article", "post", "comment"],
        "ideal_length": 1300,
        "frequency": "1/day",
        "content_mix": {
            "thought_leadership": 0.3,
            "case_studies": 0.25,
            "industry_insights": 0.2,
            "behind_the_scenes": 0.15,
            "promotion": 0.1,
        },
    },
    "reddit": {
        "post_types": ["post", "comment"],
        "ideal_length": 500,
        "frequency": "3-5 comments/day, 1 post/week",
        "content_mix": {
            "helpful_comments": 0.5,   # answer questions in relevant subs
            "discussion_posts": 0.3,   # start discussions
            "showcase": 0.1,           # share results (carefully, no spam)
            "ama": 0.1,               # engage in AMAs
        },
        "subreddits": [
            "r/SaaS", "r/startups", "r/Entrepreneur", "r/smallbusiness",
            "r/freelance", "r/digitalnomad", "r/artificial",
            "r/SideProject", "r/indiehackers",
        ],
    },
    "indie_hackers": {
        "post_types": ["post", "comment"],
        "ideal_length": 800,
        "frequency": "2-3/week",
        "content_mix": {
            "build_in_public": 0.4,    # progress updates, revenue
            "lessons_learned": 0.3,
            "helpful_comments": 0.2,
            "product_launches": 0.1,
        },
    },
}


class SocialPresence(BaseAgent):
    """Manages monAI's own social media presence across platforms."""

    name = "social_presence"
    description = (
        "Builds monAI's brand on social media. Creates content, engages with "
        "communities, generates inbound leads, and establishes authority."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(SOCIAL_PRESENCE_SCHEMA)

    def plan(self) -> list[str]:
        return [
            "Check status of social accounts across platforms",
            "Plan content calendar for the week",
            "Create content batches per platform",
            "Engage with relevant communities and conversations",
            "Track engagement metrics and optimize",
        ]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Full social presence cycle."""
        # Step 1: Check which accounts are active
        accounts = self._get_account_status()

        # Step 2: Plan content
        calendar = self._plan_content(accounts)

        # Step 3: Create content for each platform
        posts_created = 0
        for entry in calendar:
            post = self._create_post(entry)
            if post:
                posts_created += 1

        # Step 4: Plan engagement actions
        engagement = self._plan_engagement(accounts)

        # Step 5: Check metrics on recent posts
        metrics = self._check_metrics()

        self.journal("social_cycle", f"Created {posts_created} posts", {
            "accounts": len(accounts),
            "posts_created": posts_created,
            "engagement_actions": len(engagement),
        })

        return {
            "active_accounts": len([a for a in accounts if a.get("status") == "active"]),
            "posts_created": posts_created,
            "engagement_actions": len(engagement),
            "metrics": metrics,
        }

    # ── Account Management ───────────────────────────────────

    def _get_account_status(self) -> list[dict[str, Any]]:
        """Get status of all social accounts."""
        rows = self.db.execute("SELECT * FROM own_social_accounts")
        accounts = [dict(r) for r in rows]

        # Ensure all platforms are tracked
        existing_platforms = {a["platform"] for a in accounts}
        for platform in PLATFORM_STRATEGIES:
            if platform not in existing_platforms:
                self.db.execute_insert(
                    "INSERT INTO own_social_accounts (platform, status) VALUES (?, 'planned')",
                    (platform,),
                )
                accounts.append({"platform": platform, "status": "planned"})

        return accounts

    def setup_account(self, platform: str, username: str,
                      profile_url: str = "", bio: str = "") -> dict[str, Any]:
        """Register a newly created social account."""
        self.db.execute(
            "UPDATE own_social_accounts SET username = ?, profile_url = ?, "
            "bio = ?, status = 'active', updated_at = CURRENT_TIMESTAMP "
            "WHERE platform = ?",
            (username, profile_url, bio, platform),
        )
        self.log_action("setup_social_account", platform, username)
        self.share_knowledge(
            "account", f"social_{platform}",
            f"Social account active: @{username} on {platform}",
            tags=["social", platform],
        )
        return {"platform": platform, "username": username, "status": "active"}

    # ── Content Planning ─────────────────────────────────────

    def _plan_content(self, accounts: list[dict]) -> list[dict[str, Any]]:
        """Plan content calendar based on active accounts."""
        active = [a for a in accounts if a["status"] == "active"]
        if not active:
            return []

        platforms = [a["platform"] for a in active]

        # Get recent research/knowledge for content ideas
        knowledge = self.ask_knowledge(category="opportunity")
        knowledge_context = "\n".join(
            f"- {k['topic']}: {k['content'][:100]}" for k in knowledge[:5]
        )

        result = self.think_json(
            f"Plan social media content for our active platforms: {', '.join(platforms)}.\n\n"
            f"Recent business intelligence:\n{knowledge_context}\n\n"
            "Create 3-5 content pieces that build authority and attract clients. "
            "Mix value posts with case studies and engagement hooks.\n\n"
            "Return JSON: {{\"calendar\": [{{\"platform\": str, \"post_type\": str, "
            "\"topic\": str, \"angle\": str, \"target_audience\": str}}]}}"
        )

        calendar = result.get("calendar", [])

        for entry in calendar:
            self.db.execute_insert(
                "INSERT INTO content_calendar "
                "(scheduled_date, platform, post_type, topic, angle) "
                "VALUES (?, ?, ?, ?, ?)",
                (datetime.now().strftime("%Y-%m-%d"),
                 entry.get("platform", "twitter"),
                 entry.get("post_type", "post"),
                 entry.get("topic", ""),
                 entry.get("angle", "")),
            )

        return calendar

    # ── Content Creation ─────────────────────────────────────

    def _create_post(self, calendar_entry: dict) -> dict[str, Any] | None:
        """Create a single social media post."""
        platform = calendar_entry.get("platform", "twitter")
        strategy = PLATFORM_STRATEGIES.get(platform, {})
        max_length = strategy.get("ideal_length", 500)

        content = self.think(
            f"Write a {calendar_entry.get('post_type', 'post')} for {platform}.\n"
            f"Topic: {calendar_entry.get('topic', '')}\n"
            f"Angle: {calendar_entry.get('angle', '')}\n"
            f"Target audience: {calendar_entry.get('target_audience', 'entrepreneurs')}\n"
            f"Max length: {max_length} characters\n\n"
            "Write in first person. Be authentic, not corporate. "
            "Include a hook in the first line. No hashtag spam. "
            "Sound like a founder sharing real experience, NOT an AI."
        )

        if not content:
            return None

        post_id = self.db.execute_insert(
            "INSERT INTO own_social_posts "
            "(platform, post_type, content, topic, target_audience, status) "
            "VALUES (?, ?, ?, ?, ?, 'draft')",
            (platform, calendar_entry.get("post_type", "post"),
             content, calendar_entry.get("topic", ""),
             calendar_entry.get("target_audience", "")),
        )

        self.log_action("create_social_post", f"{platform}: {calendar_entry.get('topic', '')[:50]}")

        return {
            "post_id": post_id,
            "platform": platform,
            "content": content[:200] + "..." if len(content) > 200 else content,
        }

    # ── Community Engagement ─────────────────────────────────

    def _plan_engagement(self, accounts: list[dict]) -> list[dict[str, Any]]:
        """Plan engagement actions — replies, comments, community participation."""
        active = [a for a in accounts if a["status"] == "active"]
        if not active:
            return []

        platforms = [a["platform"] for a in active]

        actions = self.think_json(
            f"Plan engagement actions for: {', '.join(platforms)}.\n\n"
            "We want to build genuine relationships and attract potential clients. "
            "Focus on: answering questions, adding value to discussions, "
            "connecting with potential clients.\n\n"
            "Return JSON: {{\"actions\": [{{\"platform\": str, "
            "\"action_type\": \"comment\"|\"reply\"|\"like\"|\"follow\"|\"dm\", "
            "\"target_description\": str, \"our_approach\": str}}]}}"
        )

        engagement_actions = actions.get("actions", [])

        for action in engagement_actions:
            self.db.execute_insert(
                "INSERT INTO social_engagement_log "
                "(platform, action_type, target_user, our_response) "
                "VALUES (?, ?, ?, ?)",
                (action.get("platform", ""),
                 action.get("action_type", "comment"),
                 action.get("target_description", ""),
                 action.get("our_approach", "")),
            )

        return engagement_actions

    # ── Metrics & Optimization ───────────────────────────────

    def _check_metrics(self) -> dict[str, Any]:
        """Check engagement metrics on recent posts."""
        rows = self.db.execute(
            "SELECT platform, "
            "COUNT(*) as total_posts, "
            "SUM(engagement_likes) as total_likes, "
            "SUM(engagement_comments) as total_comments, "
            "SUM(engagement_shares) as total_shares, "
            "SUM(engagement_clicks) as total_clicks, "
            "SUM(leads_generated) as total_leads "
            "FROM own_social_posts WHERE status = 'posted' "
            "GROUP BY platform"
        )
        return {r["platform"]: dict(r) for r in rows}

    def get_best_performing_content(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get top-performing posts by engagement."""
        rows = self.db.execute(
            "SELECT *, "
            "(engagement_likes + engagement_comments * 3 + "
            "engagement_shares * 5 + engagement_clicks * 2) as score "
            "FROM own_social_posts WHERE status = 'posted' "
            "ORDER BY score DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def get_follower_growth(self) -> list[dict[str, Any]]:
        """Get current follower counts across platforms."""
        rows = self.db.execute(
            "SELECT platform, username, followers, status "
            "FROM own_social_accounts WHERE status = 'active'"
        )
        return [dict(r) for r in rows]

    def update_metrics(self, post_id: int, likes: int = 0, comments: int = 0,
                       shares: int = 0, clicks: int = 0, leads: int = 0):
        """Update engagement metrics for a posted piece of content."""
        self.db.execute(
            "UPDATE own_social_posts SET "
            "engagement_likes = ?, engagement_comments = ?, "
            "engagement_shares = ?, engagement_clicks = ?, "
            "leads_generated = ? WHERE id = ?",
            (likes, comments, shares, clicks, leads, post_id),
        )

    def update_followers(self, platform: str, followers: int):
        """Update follower count for a platform."""
        self.db.execute(
            "UPDATE own_social_accounts SET followers = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE platform = ?",
            (followers, platform),
        )
