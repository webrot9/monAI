"""Social Media Presence — manages social accounts per business/brand.

Each monAI strategy (freelance_writing, micro_saas, newsletter, etc.) gets
its own social identity, content strategy, and audience. NOT a client service
(that's strategies/social_media.py). This builds each business's brand,
attracts inbound leads, and establishes authority.

Features:
- Per-brand accounts on Twitter, LinkedIn, Reddit, Indie Hackers
- Content approval flow: draft → approved → scheduled → posted
- Real posting via platform APIs (social.api)
- Analytics pull: fetches real engagement metrics from platforms
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.social.api import SocialAPIError, create_platform_client
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

SOCIAL_PRESENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS brand_social_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,               -- strategy name: micro_saas, newsletter, etc.
    platform TEXT NOT NULL,            -- twitter, linkedin, reddit, indie_hackers
    username TEXT,
    profile_url TEXT,
    bio TEXT,
    brand_voice TEXT,                  -- tone/style description for this brand
    credentials TEXT,                  -- JSON: API keys/tokens for this platform account
    followers INTEGER DEFAULT 0,
    status TEXT DEFAULT 'planned',     -- planned, created, active, suspended
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(brand, platform)
);

CREATE TABLE IF NOT EXISTS brand_social_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    platform TEXT NOT NULL,
    post_type TEXT NOT NULL,           -- thread, post, comment, reply, article
    content TEXT NOT NULL,
    topic TEXT,
    target_audience TEXT,
    hashtags TEXT,
    status TEXT DEFAULT 'draft',       -- draft, approved, scheduled, posted, rejected, failed
    quality_score REAL,                -- LLM quality gate score (0-1)
    rejection_reason TEXT,             -- why the quality gate rejected it
    platform_post_id TEXT,             -- ID returned by the platform after posting
    platform_url TEXT,                 -- URL of the live post
    engagement_likes INTEGER DEFAULT 0,
    engagement_comments INTEGER DEFAULT 0,
    engagement_shares INTEGER DEFAULT 0,
    engagement_clicks INTEGER DEFAULT 0,
    leads_generated INTEGER DEFAULT 0,
    approved_at TIMESTAMP,
    scheduled_for TIMESTAMP,
    posted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS brand_content_calendar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    scheduled_date TEXT NOT NULL,
    platform TEXT NOT NULL,
    post_type TEXT NOT NULL,
    topic TEXT NOT NULL,
    angle TEXT,
    status TEXT DEFAULT 'planned',     -- planned, drafted, posted
    post_id INTEGER REFERENCES brand_social_posts(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS brand_engagement_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    platform TEXT NOT NULL,
    action_type TEXT NOT NULL,         -- like, comment, reply, follow, dm, repost
    target_user TEXT,
    target_post TEXT,
    our_response TEXT,
    resulted_in_lead INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Platform-specific content strategies (defaults — brands can override)
PLATFORM_STRATEGIES = {
    "twitter": {
        "post_types": ["thread", "post", "reply"],
        "ideal_length": 280,
        "frequency": "2-3/day",
        "content_mix": {
            "value_posts": 0.4,
            "case_studies": 0.2,
            "engagement": 0.2,
            "promotion": 0.1,
            "community": 0.1,
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
            "helpful_comments": 0.5,
            "discussion_posts": 0.3,
            "showcase": 0.1,
            "ama": 0.1,
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
            "build_in_public": 0.4,
            "lessons_learned": 0.3,
            "helpful_comments": 0.2,
            "product_launches": 0.1,
        },
    },
}

# Brand-specific platform recommendations (which platforms suit which business)
BRAND_PLATFORMS = {
    "freelance_writing": ["twitter", "linkedin"],
    "digital_products": ["twitter", "indie_hackers", "reddit"],
    "content_sites": ["twitter", "reddit"],
    "micro_saas": ["twitter", "indie_hackers", "reddit", "linkedin"],
    "telegram_bots": ["twitter", "reddit"],
    "affiliate": ["twitter", "reddit"],
    "newsletter": ["twitter", "linkedin", "indie_hackers"],
    "lead_gen": ["linkedin", "twitter"],
    "social_media": ["twitter", "linkedin"],
    "course_creation": ["twitter", "linkedin", "reddit"],
    "domain_flipping": ["twitter"],
    "print_on_demand": ["twitter", "reddit"],
    "saas": ["twitter", "linkedin", "indie_hackers", "reddit"],
    "cold_outreach": ["linkedin", "twitter"],
}


class SocialPresence(BaseAgent):
    """Manages social media presence for each monAI business/brand."""

    name = "social_presence"
    description = (
        "Builds social media presence per business brand. Creates content, "
        "engages communities, generates inbound leads per strategy."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(SOCIAL_PRESENCE_SCHEMA)

    def plan(self) -> list[str]:
        return [
            "Check registered brands and their social accounts",
            "Plan content calendar per brand per platform",
            "Create content batches tailored to each brand voice",
            "Engage with relevant communities per brand",
            "Track engagement metrics and optimize per brand",
        ]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Full social presence cycle across all brands.

        Pipeline: create drafts → quality gate → schedule → publish → pull metrics.
        """
        brand_filter = kwargs.get("brand")
        brands = self._get_brands(brand_filter)

        results = {}
        total_posts = 0
        total_engagement = 0

        # Phase 1: Create new content per brand
        for brand in brands:
            accounts = self._get_brand_accounts(brand)
            active = [a for a in accounts if a["status"] == "active"]
            if not active:
                results[brand] = {"status": "no_active_accounts"}
                continue

            calendar = self._plan_content(brand, active)

            posts_created = 0
            for entry in calendar:
                post = self._create_post(brand, entry)
                if post:
                    posts_created += 1

            engagement = self._plan_engagement(brand, active)
            metrics = self._check_metrics(brand)

            results[brand] = {
                "active_accounts": len(active),
                "posts_created": posts_created,
                "engagement_actions": len(engagement),
                "metrics": metrics,
            }
            total_posts += posts_created
            total_engagement += len(engagement)

        # Phase 2: Quality gate — review all drafts
        review_results = self.review_all_drafts()

        # Phase 3: Schedule approved posts
        schedule_results = self.schedule_all_approved()

        # Phase 4: Publish scheduled posts via platform APIs
        publish_results = self.publish_all_scheduled()

        # Phase 5: Pull real metrics for previously posted content
        metrics_results = self.pull_all_metrics()
        follower_results = self.pull_follower_counts()

        self.journal("social_cycle",
                     f"Created {total_posts}, published {publish_results.get('posted', 0)} "
                     f"across {len(brands)} brands", {
                         "brands": len(brands),
                         "total_drafts": total_posts,
                         "approved": review_results.get("approved", 0),
                         "rejected": review_results.get("rejected", 0),
                         "published": publish_results.get("posted", 0),
                         "metrics_updated": metrics_results.get("updated", 0),
                         "total_engagement": total_engagement,
                     })

        return {
            "brands_processed": len(brands),
            "total_posts": total_posts,
            "total_engagement": total_engagement,
            "review": review_results,
            "scheduled": schedule_results,
            "published": publish_results,
            "metrics_pull": metrics_results,
            "followers_pull": follower_results,
            "per_brand": results,
        }

    # ── Brand & Account Management ───────────────────────────

    def _get_brands(self, brand_filter: str | None = None) -> list[str]:
        """Get all brands with registered social accounts."""
        if brand_filter:
            return [brand_filter]
        rows = self.db.execute(
            "SELECT DISTINCT brand FROM brand_social_accounts"
        )
        return [r["brand"] for r in rows]

    def register_brand(self, brand: str, platforms: list[str] | None = None,
                       brand_voice: str = "") -> list[dict[str, Any]]:
        """Register a brand and seed its platform accounts."""
        if platforms is None:
            platforms = BRAND_PLATFORMS.get(brand, ["twitter"])

        accounts = []
        for platform in platforms:
            self.db.execute_insert(
                "INSERT OR IGNORE INTO brand_social_accounts "
                "(brand, platform, brand_voice) VALUES (?, ?, ?)",
                (brand, platform, brand_voice),
            )
            accounts.append({
                "brand": brand, "platform": platform, "status": "planned",
            })

        self.log_action("register_brand", brand, f"platforms={platforms}")
        return accounts

    def _get_brand_accounts(self, brand: str) -> list[dict[str, Any]]:
        """Get all social accounts for a brand."""
        rows = self.db.execute(
            "SELECT * FROM brand_social_accounts WHERE brand = ?", (brand,)
        )
        return [dict(r) for r in rows]

    def setup_account(self, brand: str, platform: str, username: str,
                      profile_url: str = "", bio: str = "",
                      credentials: dict[str, str] | None = None) -> dict[str, Any]:
        """Activate a social account for a brand.

        Checks that credentials contain the fields the platform API actually
        requires (e.g. LinkedIn needs access_token + person_urn, not just a
        password). This prevents phantom "active" accounts that can't post.

        Full API validation happens at publish time — if credentials are
        rejected by the platform, publish_post() marks the account as
        auth_failed so we stop retrying.
        """
        from monai.social.api import get_required_credential_fields

        # Check required credential fields BEFORE marking active
        required = get_required_credential_fields(platform)
        if required:
            creds = credentials or {}
            missing = [f for f in required if not creds.get(f)]
            if missing:
                logger.warning(
                    "Cannot activate %s/%s — missing required credential "
                    "fields: %s (have: %s)",
                    brand, platform, missing, list(creds.keys()),
                )
                return {
                    "brand": brand, "platform": platform,
                    "status": "rejected",
                    "reason": f"Missing required fields: {', '.join(missing)}",
                }

        creds_json = json.dumps(credentials) if credentials else None
        self.db.execute(
            "UPDATE brand_social_accounts SET username = ?, profile_url = ?, "
            "bio = ?, credentials = ?, status = 'active', updated_at = CURRENT_TIMESTAMP "
            "WHERE brand = ? AND platform = ?",
            (username, profile_url, bio, creds_json, brand, platform),
        )
        self.log_action("setup_social_account", f"{brand}/{platform}", username)
        self.share_knowledge(
            "account", f"social_{brand}_{platform}",
            f"Social account active: @{username} on {platform} for {brand}",
            tags=["social", brand, platform],
        )
        return {"brand": brand, "platform": platform,
                "username": username, "status": "active"}

    # ── Content Planning ─────────────────────────────────────

    def _plan_content(self, brand: str,
                      accounts: list[dict]) -> list[dict[str, Any]]:
        """Plan content calendar for a specific brand."""
        active = [a for a in accounts if a["status"] == "active"]
        if not active:
            return []

        platforms = [a["platform"] for a in active]
        brand_voice = active[0].get("brand_voice", "") or ""

        knowledge = self.ask_knowledge(category="opportunity")
        knowledge_context = "\n".join(
            f"- {k['topic']}: {k['content'][:100]}" for k in knowledge[:5]
        )

        result = self.think_json(
            f"Plan social media content for the '{brand}' brand.\n"
            f"Active platforms: {', '.join(platforms)}\n"
            f"Brand voice: {brand_voice or 'authentic, knowledgeable, approachable'}\n\n"
            f"Recent business intelligence:\n{knowledge_context}\n\n"
            f"Create 3-5 content pieces that build authority for this "
            f"specific business and attract its target audience.\n\n"
            "Return JSON: {{\"calendar\": [{{\"platform\": str, \"post_type\": str, "
            "\"topic\": str, \"angle\": str, \"target_audience\": str}}]}}"
        )

        calendar = result.get("calendar", [])

        for entry in calendar:
            self.db.execute_insert(
                "INSERT INTO brand_content_calendar "
                "(brand, scheduled_date, platform, post_type, topic, angle) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (brand,
                 datetime.now().strftime("%Y-%m-%d"),
                 entry.get("platform", "twitter"),
                 entry.get("post_type", "post"),
                 entry.get("topic", ""),
                 entry.get("angle", "")),
            )

        return calendar

    # ── Content Creation ─────────────────────────────────────

    def _create_post(self, brand: str,
                     calendar_entry: dict) -> dict[str, Any] | None:
        """Create a single post for a brand."""
        platform = calendar_entry.get("platform", "twitter")
        strategy = PLATFORM_STRATEGIES.get(platform, {})
        max_length = strategy.get("ideal_length", 500)

        # Fetch brand voice
        rows = self.db.execute(
            "SELECT brand_voice FROM brand_social_accounts "
            "WHERE brand = ? AND platform = ?", (brand, platform)
        )
        brand_voice = rows[0]["brand_voice"] if rows and rows[0]["brand_voice"] else ""

        content = self.think(
            f"Write a {calendar_entry.get('post_type', 'post')} for {platform}.\n"
            f"Brand: {brand}\n"
            f"Brand voice: {brand_voice or 'authentic, expert, approachable'}\n"
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
            "INSERT INTO brand_social_posts "
            "(brand, platform, post_type, content, topic, target_audience, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'draft')",
            (brand, platform, calendar_entry.get("post_type", "post"),
             content, calendar_entry.get("topic", ""),
             calendar_entry.get("target_audience", "")),
        )

        self.log_action("create_social_post",
                        f"{brand}/{platform}: {calendar_entry.get('topic', '')[:50]}")

        return {
            "post_id": post_id,
            "brand": brand,
            "platform": platform,
            "content": content[:200] + "..." if len(content) > 200 else content,
        }

    # ── Community Engagement ─────────────────────────────────

    def _plan_engagement(self, brand: str,
                         accounts: list[dict]) -> list[dict[str, Any]]:
        """Plan engagement actions for a brand."""
        active = [a for a in accounts if a["status"] == "active"]
        if not active:
            return []

        platforms = [a["platform"] for a in active]

        actions = self.think_json(
            f"Plan engagement actions for the '{brand}' brand on: "
            f"{', '.join(platforms)}.\n\n"
            "Build genuine relationships and attract potential customers "
            "for this specific business. Focus on: answering questions, "
            "adding value to discussions, connecting with the target audience.\n\n"
            "Return JSON: {{\"actions\": [{{\"platform\": str, "
            "\"action_type\": \"comment\"|\"reply\"|\"like\"|\"follow\"|\"dm\", "
            "\"target_description\": str, \"our_approach\": str}}]}}"
        )

        engagement_actions = actions.get("actions", [])

        for action in engagement_actions:
            self.db.execute_insert(
                "INSERT INTO brand_engagement_log "
                "(brand, platform, action_type, target_user, our_response) "
                "VALUES (?, ?, ?, ?, ?)",
                (brand,
                 action.get("platform", ""),
                 action.get("action_type", "comment"),
                 action.get("target_description", ""),
                 action.get("our_approach", "")),
            )

        return engagement_actions

    # ── Metrics & Optimization ───────────────────────────────

    def _check_metrics(self, brand: str) -> dict[str, Any]:
        """Check engagement metrics for a brand's posts."""
        rows = self.db.execute(
            "SELECT platform, "
            "COUNT(*) as total_posts, "
            "SUM(engagement_likes) as total_likes, "
            "SUM(engagement_comments) as total_comments, "
            "SUM(engagement_shares) as total_shares, "
            "SUM(engagement_clicks) as total_clicks, "
            "SUM(leads_generated) as total_leads "
            "FROM brand_social_posts WHERE brand = ? AND status = 'posted' "
            "GROUP BY platform",
            (brand,),
        )
        return {r["platform"]: dict(r) for r in rows}

    def get_best_performing_content(self, brand: str | None = None,
                                    limit: int = 10) -> list[dict[str, Any]]:
        """Get top-performing posts, optionally filtered by brand."""
        query = (
            "SELECT *, "
            "(engagement_likes + engagement_comments * 3 + "
            "engagement_shares * 5 + engagement_clicks * 2) as score "
            "FROM brand_social_posts WHERE status = 'posted' "
        )
        params: tuple = ()
        if brand:
            query += "AND brand = ? "
            params = (brand,)
        query += "ORDER BY score DESC LIMIT ?"
        params = (*params, limit)

        rows = self.db.execute(query, params)
        return [dict(r) for r in rows]

    def get_follower_growth(self, brand: str | None = None) -> list[dict[str, Any]]:
        """Get follower counts, optionally filtered by brand."""
        query = (
            "SELECT brand, platform, username, followers, status "
            "FROM brand_social_accounts WHERE status = 'active'"
        )
        params: tuple = ()
        if brand:
            query += " AND brand = ?"
            params = (brand,)

        rows = self.db.execute(query, params)
        return [dict(r) for r in rows]

    def update_metrics(self, post_id: int, likes: int = 0, comments: int = 0,
                       shares: int = 0, clicks: int = 0, leads: int = 0):
        """Update engagement metrics for a posted piece of content."""
        self.db.execute(
            "UPDATE brand_social_posts SET "
            "engagement_likes = ?, engagement_comments = ?, "
            "engagement_shares = ?, engagement_clicks = ?, "
            "leads_generated = ? WHERE id = ?",
            (likes, comments, shares, clicks, leads, post_id),
        )

    def update_followers(self, brand: str, platform: str, followers: int):
        """Update follower count for a brand's platform account."""
        self.db.execute(
            "UPDATE brand_social_accounts SET followers = ?, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE brand = ? AND platform = ?",
            (followers, brand, platform),
        )

    def get_all_brands_summary(self) -> list[dict[str, Any]]:
        """Summary of all brands and their social presence status."""
        rows = self.db.execute(
            "SELECT brand, "
            "COUNT(*) as total_accounts, "
            "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active_accounts, "
            "SUM(followers) as total_followers "
            "FROM brand_social_accounts "
            "GROUP BY brand"
        )
        return [dict(r) for r in rows]

    # ── Content Approval Flow ────────────────────────────────

    def review_post(self, post_id: int) -> dict[str, Any]:
        """Run quality gate on a draft post. Approves or rejects with reason."""
        rows = self.db.execute(
            "SELECT * FROM brand_social_posts WHERE id = ?", (post_id,)
        )
        if not rows:
            return {"status": "not_found"}

        post = dict(rows[0])
        if post["status"] != "draft":
            return {"status": "already_reviewed", "current_status": post["status"]}

        platform = post["platform"]
        strategy = PLATFORM_STRATEGIES.get(platform, {})
        max_length = strategy.get("ideal_length", 500)

        verdict = self.think_json(
            f"Review this social media post for quality.\n\n"
            f"Platform: {platform}\n"
            f"Brand: {post['brand']}\n"
            f"Content:\n{post['content']}\n\n"
            f"Ideal max length: {max_length} chars (actual: {len(post['content'])})\n\n"
            "Criteria:\n"
            "1. Does it sound human and authentic (not AI-generated)?\n"
            "2. Does it have a strong hook in the first line?\n"
            "3. Is it appropriate for the platform and audience?\n"
            "4. Is it within reasonable length for the platform?\n"
            "5. Would it embarrass us if posted publicly?\n\n"
            "Return JSON: {{\"approved\": bool, \"score\": float (0-1), "
            "\"reason\": str (why rejected, if not approved)}}"
        )

        approved = verdict.get("approved", False)
        score = verdict.get("score", 0.0)
        reason = verdict.get("reason", "")

        if approved:
            self.db.execute(
                "UPDATE brand_social_posts SET status = 'approved', "
                "quality_score = ?, approved_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (score, post_id),
            )
            self.log_action("approve_post", f"Post {post_id} approved (score={score:.2f})")
            return {"status": "approved", "score": score, "post_id": post_id}
        else:
            self.db.execute(
                "UPDATE brand_social_posts SET status = 'rejected', "
                "quality_score = ?, rejection_reason = ? WHERE id = ?",
                (score, reason, post_id),
            )
            self.log_action("reject_post", f"Post {post_id} rejected: {reason[:100]}")
            return {"status": "rejected", "score": score, "reason": reason,
                    "post_id": post_id}

    def review_all_drafts(self) -> dict[str, Any]:
        """Review all draft posts through the quality gate."""
        rows = self.db.execute(
            "SELECT id FROM brand_social_posts WHERE status = 'draft'"
        )
        results = {"approved": 0, "rejected": 0, "total": len(rows)}
        for row in rows:
            result = self.review_post(row["id"])
            if result.get("status") == "approved":
                results["approved"] += 1
            elif result.get("status") == "rejected":
                results["rejected"] += 1
        return results

    def schedule_post(self, post_id: int,
                      scheduled_for: str | None = None) -> dict[str, Any]:
        """Mark an approved post as scheduled for publishing."""
        rows = self.db.execute(
            "SELECT status FROM brand_social_posts WHERE id = ?", (post_id,)
        )
        if not rows:
            return {"status": "not_found"}
        if rows[0]["status"] != "approved":
            return {"status": "not_approved", "current_status": rows[0]["status"]}

        schedule_time = scheduled_for or datetime.now().isoformat()
        self.db.execute(
            "UPDATE brand_social_posts SET status = 'scheduled', "
            "scheduled_for = ? WHERE id = ?",
            (schedule_time, post_id),
        )
        return {"status": "scheduled", "post_id": post_id,
                "scheduled_for": schedule_time}

    def schedule_all_approved(self) -> dict[str, Any]:
        """Schedule all approved posts for immediate publishing."""
        rows = self.db.execute(
            "SELECT id FROM brand_social_posts WHERE status = 'approved'"
        )
        now = datetime.now().isoformat()
        for row in rows:
            self.schedule_post(row["id"], scheduled_for=now)
        return {"scheduled": len(rows)}

    # ── Publishing (API Integration) ─────────────────────────

    def publish_post(self, post_id: int) -> dict[str, Any]:
        """Publish a scheduled post via the platform's API."""
        rows = self.db.execute(
            "SELECT p.*, a.credentials FROM brand_social_posts p "
            "JOIN brand_social_accounts a ON p.brand = a.brand AND p.platform = a.platform "
            "WHERE p.id = ?",
            (post_id,),
        )
        if not rows:
            return {"status": "not_found"}

        post = dict(rows[0])
        if post["status"] != "scheduled":
            return {"status": "not_scheduled", "current_status": post["status"]}

        creds_raw = post.get("credentials")
        if not creds_raw:
            self.db.execute(
                "UPDATE brand_social_posts SET status = 'failed' WHERE id = ?",
                (post_id,),
            )
            return {"status": "no_credentials", "post_id": post_id}

        credentials = json.loads(creds_raw)

        try:
            client = create_platform_client(
                post["platform"], self.config, credentials
            )
            result = client.post(post["content"])

            self.db.execute(
                "UPDATE brand_social_posts SET status = 'posted', "
                "platform_post_id = ?, platform_url = ?, "
                "posted_at = CURRENT_TIMESTAMP WHERE id = ?",
                (result.get("post_id", ""), result.get("url", ""), post_id),
            )

            self.log_action(
                "publish_post",
                f"{post['brand']}/{post['platform']}: {(post.get('topic') or '')[:50]}",
                result.get("url", ""),
            )

            return {"status": "posted", "post_id": post_id,
                    "platform_post_id": result.get("post_id", ""),
                    "url": result.get("url", "")}

        except SocialAPIError as e:
            error_str = str(e)
            self.db.execute(
                "UPDATE brand_social_posts SET status = 'failed', "
                "rejection_reason = ? WHERE id = ?",
                (error_str[:500], post_id),
            )
            logger.error(f"Failed to publish post {post_id}: {e}")

            # Self-healing: if auth-related failure, deactivate the account
            # so we don't keep wasting API calls with bad credentials
            error_lower = error_str.lower()
            auth_signals = ("401", "403", "unauthorized", "authentication",
                            "invalid token", "expired", "access denied")
            if any(s in error_lower for s in auth_signals):
                logger.warning(
                    "Auth failure publishing to %s/%s — deactivating account",
                    post["brand"], post["platform"],
                )
                self.db.execute(
                    "UPDATE brand_social_accounts SET status = 'auth_failed', "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE brand = ? AND platform = ?",
                    (post["brand"], post["platform"]),
                )

            return {"status": "failed", "post_id": post_id, "error": error_str}

    def publish_all_scheduled(self) -> dict[str, Any]:
        """Publish all scheduled posts."""
        rows = self.db.execute(
            "SELECT id FROM brand_social_posts WHERE status = 'scheduled'"
        )
        results = {"posted": 0, "failed": 0, "total": len(rows)}
        for row in rows:
            result = self.publish_post(row["id"])
            if result.get("status") == "posted":
                results["posted"] += 1
            else:
                results["failed"] += 1
        return results

    # ── Analytics Pull ───────────────────────────────────────

    def pull_post_metrics(self, post_id: int) -> dict[str, Any]:
        """Fetch real engagement metrics from the platform API for a posted post."""
        rows = self.db.execute(
            "SELECT p.platform, p.platform_post_id, p.brand, a.credentials "
            "FROM brand_social_posts p "
            "JOIN brand_social_accounts a ON p.brand = a.brand AND p.platform = a.platform "
            "WHERE p.id = ? AND p.status = 'posted' AND p.platform_post_id IS NOT NULL",
            (post_id,),
        )
        if not rows:
            return {"status": "not_found_or_not_posted"}

        post = dict(rows[0])
        creds_raw = post.get("credentials")
        if not creds_raw:
            return {"status": "no_credentials"}

        credentials = json.loads(creds_raw)

        try:
            client = create_platform_client(
                post["platform"], self.config, credentials
            )
            metrics = client.get_post_metrics(post["platform_post_id"])

            self.update_metrics(
                post_id,
                likes=metrics.get("likes", 0),
                comments=metrics.get("comments", 0),
                shares=metrics.get("shares", 0),
                clicks=metrics.get("clicks", 0),
            )

            return {"status": "updated", "post_id": post_id, "metrics": metrics}

        except SocialAPIError as e:
            logger.error(f"Failed to pull metrics for post {post_id}: {e}")
            return {"status": "error", "error": str(e)}

    def pull_all_metrics(self) -> dict[str, Any]:
        """Pull metrics for all posted content that has a platform_post_id."""
        rows = self.db.execute(
            "SELECT id FROM brand_social_posts "
            "WHERE status = 'posted' AND platform_post_id IS NOT NULL"
        )
        results = {"updated": 0, "failed": 0, "total": len(rows)}
        for row in rows:
            result = self.pull_post_metrics(row["id"])
            if result.get("status") == "updated":
                results["updated"] += 1
            else:
                results["failed"] += 1
        return results

    def pull_follower_counts(self) -> dict[str, Any]:
        """Pull follower counts from platform APIs for all active accounts."""
        rows = self.db.execute(
            "SELECT brand, platform, credentials "
            "FROM brand_social_accounts "
            "WHERE status = 'active' AND credentials IS NOT NULL"
        )
        results = {"updated": 0, "failed": 0, "total": len(rows)}

        for row in rows:
            try:
                credentials = json.loads(row["credentials"])
                client = create_platform_client(
                    row["platform"], self.config, credentials
                )
                profile = client.get_profile_metrics()
                followers = profile.get("followers", 0)
                self.update_followers(row["brand"], row["platform"], followers)
                results["updated"] += 1
            except Exception as e:
                logger.error(
                    f"Failed to pull followers for {row['brand']}/{row['platform']}: {e}"
                )
                results["failed"] += 1

        return results
