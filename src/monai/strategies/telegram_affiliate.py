"""Telegram affiliate channel strategy.

Runs a Telegram channel that posts curated deals with affiliate links.
Model: Alien Sales, OfferteLampo, etc.

Pipeline:
  1. Source deals (Amazon, scraping deal sites)
  2. Filter & rank (discount %, relevance, commission potential)
  3. Format post (image, price comparison, affiliate link)
  4. Post to channel on schedule (5-10/day)
  5. Grow audience (cross-post social, viral mechanics, landing page)
  6. Track performance (clicks, conversions, revenue)
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.integrations.telegram_channel import TelegramChannelClient
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

# Amazon affiliate tag appended to all product URLs
# This is set via config or identity manager at runtime
DEFAULT_AFFILIATE_TAG = ""

DEAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS affiliate_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,            -- amazon, aliexpress, other
    product_name TEXT NOT NULL,
    product_url TEXT NOT NULL,
    affiliate_url TEXT,
    original_price REAL,
    deal_price REAL,
    discount_pct REAL,
    image_url TEXT,
    category TEXT,
    commission_pct REAL DEFAULT 0,
    score REAL DEFAULT 0,            -- ranking score
    status TEXT DEFAULT 'sourced',   -- sourced, approved, posted, expired
    posted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_url)
);

CREATE TABLE IF NOT EXISTS channel_growth_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,       -- social_post, cross_promo, landing_page, viral
    platform TEXT,
    details TEXT,
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ad_status ON affiliate_deals(status, score DESC);
"""

# Sites to scrape for deals
DEAL_SOURCES = [
    ("https://www.amazon.it/gp/goldbox", "Amazon IT Offerte del Giorno"),
    ("https://www.amazon.it/deals", "Amazon IT Offerte"),
    ("https://www.pepper.it/", "Pepper.it — community deal aggregator"),
]

# Amazon categories with good affiliate commissions (IT program)
HIGH_COMMISSION_CATEGORIES = [
    "fashion", "beauty", "home", "garden", "kitchen",
    "sport", "toys", "tools", "pet", "handmade",
]


class TelegramAffiliateAgent(BaseAgent):
    """Curates and posts affiliate deals to a Telegram channel."""

    name = "telegram_affiliate"
    description = (
        "Runs a Telegram deals channel — sources products with big discounts, "
        "posts them with affiliate links, grows audience through social media "
        "and viral mechanics. Revenue from affiliate commissions."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self._channel: TelegramChannelClient | None = None
        self._affiliate_tag = ""
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(DEAL_SCHEMA)

    @property
    def channel(self) -> TelegramChannelClient | None:
        """Lazy-load channel client."""
        if self._channel is None:
            bot_token = self.identity.get_api_key("telegram_channel_bot")
            channel_username = self.identity.get_api_key("telegram_channel_username")
            if bot_token and channel_username:
                self._channel = TelegramChannelClient(
                    self.config, self.db, bot_token, channel_username,
                )
        return self._channel

    def _ensure_channel(self) -> bool:
        """Ensure we have a Telegram channel set up."""
        if self.channel:
            return True

        # Try to set up channel via browser automation
        self.log_action("channel_setup", "Setting up Telegram channel and bot")

        # Step 1: Create bot via BotFather
        bot_result = self.execute_task(
            "Create a Telegram bot via @BotFather on Telegram:\n"
            "1. Send /newbot to @BotFather\n"
            "2. Name it something catchy for deals (e.g. 'OfferteFlash Bot')\n"
            "3. Get the API token\n"
            "Return the bot token in the result.",
        )
        bot_token = ""
        if bot_result.get("status") == "completed":
            result = bot_result.get("result", "")
            if isinstance(result, dict):
                bot_token = result.get("token", result.get("bot_token", ""))
            elif isinstance(result, str) and ":" in result:
                bot_token = result.strip()

        if not bot_token:
            self.log_action("channel_setup_failed", "Could not create bot")
            return False

        # Step 2: Create channel
        channel_result = self.execute_task(
            "Create a public Telegram channel for deal alerts:\n"
            "1. Open Telegram\n"
            "2. Create a new channel (public)\n"
            "3. Pick a catchy name and @username for deals/offers\n"
            "4. Add the bot as admin with 'Post Messages' permission\n"
            "Return the channel @username.",
        )
        channel_username = ""
        if channel_result.get("status") == "completed":
            result = channel_result.get("result", "")
            if isinstance(result, str) and result.startswith("@"):
                channel_username = result.strip()
            elif isinstance(result, dict):
                channel_username = result.get("username", "")
                if channel_username and not channel_username.startswith("@"):
                    channel_username = f"@{channel_username}"

        if not channel_username:
            self.log_action("channel_setup_failed", "Could not create channel")
            return False

        # Store credentials
        self.identity.store_api_key("telegram_channel_bot", "bot_token", bot_token)
        self.identity.store_api_key("telegram_channel_username", "username", channel_username)
        self._channel = None  # Reset to reload
        self.log_action("channel_setup", f"Channel ready: {channel_username}")
        return self.channel is not None

    def _get_affiliate_tag(self) -> str:
        """Get Amazon affiliate tag from identity or config."""
        if not self._affiliate_tag:
            self._affiliate_tag = (
                self.identity.get_api_key("amazon_affiliate_tag")
                or DEFAULT_AFFILIATE_TAG
            )
        return self._affiliate_tag

    # ── Pipeline ──────────────────────────────────────────────────

    def plan(self) -> list[str]:
        """Decide what to do this cycle."""
        steps = []

        # Always source deals
        pending = self.db.execute(
            "SELECT COUNT(*) as c FROM affiliate_deals WHERE status = 'approved'"
        )
        pending_count = dict(pending[0])["c"] if pending else 0

        if pending_count < 5:
            steps.append("source_deals")

        # Post if we have approved deals and channel exists
        if pending_count > 0:
            steps.append("post_deals")

        # Growth every 3rd cycle
        cycle_count = self._get_cycle_count()
        if cycle_count % 3 == 0:
            steps.append("grow_audience")

        if not steps:
            steps.append("source_deals")

        return steps

    def _get_cycle_count(self) -> int:
        rows = self.db.execute(
            "SELECT COUNT(*) as c FROM agent_log WHERE agent_name = ?",
            (self.name,),
        )
        return dict(rows[0])["c"] if rows else 0

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting affiliate channel cycle")
        steps = self.plan()
        results = {}

        step_methods = {
            "source_deals": self._source_deals,
            "post_deals": self._post_deals,
            "grow_audience": self._grow_audience,
        }
        for step in steps:
            fn = step_methods.get(step)
            if fn:
                results[step] = self.run_step(step, fn)

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    # ── Step 1: Source Deals ──────────────────────────────────────

    def _source_deals(self) -> dict[str, Any]:
        """Scrape deal sites for affiliate opportunities."""
        total_found = 0

        for url, source_name in DEAL_SOURCES:
            try:
                result = self.browse_and_extract(
                    url,
                    "Extract all deals/offers visible on this page. For each deal get:\n"
                    "- product_name: full name\n"
                    "- product_url: link to the product (full URL)\n"
                    "- original_price: price before discount (number only)\n"
                    "- deal_price: current discounted price (number only)\n"
                    "- discount_pct: discount percentage (number only)\n"
                    "- image_url: product image URL if visible\n"
                    "- category: product category\n"
                    "Return JSON: {\"deals\": [{...}]}",
                )
                deals = result.get("result", {})
                if isinstance(deals, dict):
                    deals = deals.get("deals", [])
                elif isinstance(deals, str):
                    try:
                        deals = json.loads(deals).get("deals", [])
                    except (json.JSONDecodeError, AttributeError):
                        deals = []

                for deal in deals:
                    if not deal.get("product_name") or not deal.get("product_url"):
                        continue
                    self._save_deal(deal, source_name)
                    total_found += 1
            except Exception as e:
                self.log_action("source_error", f"{source_name}: {e}")

        # Score and approve top deals
        approved = self._score_and_approve_deals()

        self.log_action("source_deals", f"Found {total_found}, approved {approved}")
        return {"deals_found": total_found, "deals_approved": approved}

    def _save_deal(self, deal: dict, source: str) -> None:
        """Save a deal to DB (skip duplicates)."""
        url = deal.get("product_url", "")
        if not url:
            return

        try:
            original = float(deal.get("original_price", 0) or 0)
            current = float(deal.get("deal_price", 0) or 0)
            discount = float(deal.get("discount_pct", 0) or 0)
            if original > 0 and current > 0 and discount == 0:
                discount = round((1 - current / original) * 100, 1)
        except (ValueError, TypeError):
            original, current, discount = 0, 0, 0

        try:
            self.db.execute_insert(
                "INSERT OR IGNORE INTO affiliate_deals "
                "(source, product_name, product_url, original_price, deal_price, "
                "discount_pct, image_url, category, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sourced')",
                (source, deal.get("product_name", "")[:200], url[:500],
                 original, current, discount,
                 deal.get("image_url", "")[:500],
                 deal.get("category", "")[:100]),
            )
        except Exception:
            pass  # Duplicate URL

    def _score_and_approve_deals(self) -> int:
        """Score sourced deals and approve the best ones."""
        sourced = self.db.execute(
            "SELECT * FROM affiliate_deals WHERE status = 'sourced' "
            "ORDER BY created_at DESC LIMIT 50",
        )

        approved = 0
        for row in sourced:
            deal = dict(row)
            score = self._calculate_deal_score(deal)

            self.db.execute(
                "UPDATE affiliate_deals SET score = ? WHERE id = ?",
                (score, deal["id"]),
            )

            # Approve deals with score > 50
            if score >= 50:
                affiliate_url = self._make_affiliate_url(deal["product_url"])
                self.db.execute(
                    "UPDATE affiliate_deals SET status = 'approved', "
                    "affiliate_url = ? WHERE id = ?",
                    (affiliate_url, deal["id"]),
                )
                approved += 1

        return approved

    def _calculate_deal_score(self, deal: dict) -> float:
        """Score a deal 0-100 based on quality signals."""
        score = 0.0

        # Discount percentage (max 40 points)
        discount = deal.get("discount_pct", 0) or 0
        if discount >= 50:
            score += 40
        elif discount >= 30:
            score += 30
        elif discount >= 20:
            score += 20
        elif discount >= 10:
            score += 10

        # Price sweet spot: €10-€100 (max 20 points)
        price = deal.get("deal_price", 0) or 0
        if 10 <= price <= 100:
            score += 20
        elif 5 <= price <= 200:
            score += 10

        # Category commission bonus (max 20 points)
        category = (deal.get("category", "") or "").lower()
        if any(c in category for c in HIGH_COMMISSION_CATEGORIES):
            score += 20

        # Has image (10 points)
        if deal.get("image_url"):
            score += 10

        # Source quality (10 points)
        source = (deal.get("source", "") or "").lower()
        if "amazon" in source:
            score += 10
        elif "pepper" in source:
            score += 8

        return min(score, 100)

    def _make_affiliate_url(self, product_url: str) -> str:
        """Add affiliate tag to product URL."""
        tag = self._get_affiliate_tag()
        if not tag:
            return product_url

        # Amazon URLs: add/replace tag parameter
        if "amazon" in product_url.lower():
            # Remove existing tag if present
            url = re.sub(r'[?&]tag=[^&]*', '', product_url)
            separator = "&" if "?" in url else "?"
            return f"{url}{separator}tag={tag}"

        return product_url

    # ── Step 2: Post Deals ────────────────────────────────────────

    def _post_deals(self) -> dict[str, Any]:
        """Post approved deals to the Telegram channel."""
        if not self._ensure_channel():
            return {"status": "channel_not_ready"}

        # Get top approved deals (limit per cycle)
        deals = self.db.execute(
            "SELECT * FROM affiliate_deals WHERE status = 'approved' "
            "ORDER BY score DESC LIMIT 5",
        )

        posted = 0
        for row in deals:
            deal = dict(row)
            try:
                message = self._format_deal_post(deal)
                if deal.get("image_url"):
                    self.channel.post_photo(
                        photo_url=deal["image_url"],
                        caption=message,
                    )
                else:
                    self.channel.post_message(message)

                self.db.execute(
                    "UPDATE affiliate_deals SET status = 'posted', "
                    "posted_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (deal["id"],),
                )
                posted += 1
            except Exception as e:
                self.log_action("post_error", f"Failed to post deal {deal['id']}: {e}")

        if posted > 0:
            self.log_action("post_deals", f"Posted {posted} deals")

        return {"deals_posted": posted}

    def _format_deal_post(self, deal: dict) -> str:
        """Format a deal as an attractive Telegram message."""
        name = deal.get("product_name", "Prodotto")
        original = deal.get("original_price", 0)
        current = deal.get("deal_price", 0)
        discount = deal.get("discount_pct", 0)
        url = deal.get("affiliate_url") or deal.get("product_url", "")

        parts = []

        # Fire emoji + title
        parts.append(f"🔥 <b>{name}</b>")

        # Price comparison
        if original and current and original > current:
            parts.append(
                f"\n💰 <s>€{original:.2f}</s> → <b>€{current:.2f}</b>"
            )
            if discount:
                parts.append(f"  (-{discount:.0f}%)")
        elif current:
            parts.append(f"\n💰 <b>€{current:.2f}</b>")

        # Category tag
        category = deal.get("category", "")
        if category:
            parts.append(f"\n📦 {category}")

        # Link
        parts.append(f"\n\n👉 <a href=\"{url}\">Vai all'offerta</a>")

        return "".join(parts)

    # ── Step 3: Grow Audience ─────────────────────────────────────

    def _grow_audience(self) -> dict[str, Any]:
        """Execute audience growth actions."""
        results = {}

        # Strategy 1: Generate social media content promoting the channel
        results["social"] = self._cross_post_social()

        # Strategy 2: Create/update landing page
        results["landing"] = self._update_landing_page()

        # Strategy 3: Analyze what's working
        results["analytics"] = self._analyze_growth()

        return results

    def _cross_post_social(self) -> dict[str, Any]:
        """Post channel promotion content on social platforms."""
        channel_username = self.identity.get_api_key("telegram_channel_username") or "@channel"

        # Get recent best deals for social proof
        top_deals = self.db.execute(
            "SELECT product_name, discount_pct FROM affiliate_deals "
            "WHERE status = 'posted' ORDER BY score DESC LIMIT 3",
        )
        deal_highlights = [
            f"• {dict(d)['product_name'][:50]} (-{dict(d).get('discount_pct', 0):.0f}%)"
            for d in top_deals
        ]

        # Generate social post via LLM
        social_content = self.think(
            f"Write a short, catchy social media post (max 280 chars for Twitter, "
            f"longer version for Reddit/LinkedIn) promoting this Telegram deals channel.\n\n"
            f"Channel: {channel_username}\n"
            f"What it does: Curated daily deals with the biggest discounts\n"
            f"Recent deals posted:\n" + "\n".join(deal_highlights[:3]) + "\n\n"
            "Write in Italian. Be authentic, not salesy. Include the channel link.\n"
            "Return the post text only, no JSON.",
        )

        # Post to available social platforms
        posted_to = []
        for platform in ("twitter", "reddit", "linkedin"):
            try:
                creds = self.identity.get_platform_credentials(platform)
                if creds:
                    self.platform_action(
                        platform,
                        f"Post this content: {social_content}",
                        context=social_content,
                    )
                    posted_to.append(platform)
            except Exception as e:
                logger.debug(f"Social post to {platform} failed: {e}")

        if posted_to:
            self._record_growth_action("social_post", ",".join(posted_to), social_content)

        return {"platforms": posted_to, "content": social_content[:100]}

    def _update_landing_page(self) -> dict[str, Any]:
        """Create/update a simple landing page for the channel."""
        channel_username = self.identity.get_api_key("telegram_channel_username")
        if not channel_username:
            return {"status": "no_channel"}

        # Get channel stats
        stats = {}
        if self.channel:
            stats = self.channel.get_post_stats(days=30)

        page_data = {
            "channel": channel_username,
            "subscriber_count": stats.get("subscriber_count", 0),
            "deals_posted": stats.get("total_posts", 0),
            "description": (
                "Le migliori offerte ogni giorno. Sconti fino al 70% "
                "su Amazon, tecnologia, casa e molto altro."
            ),
        }

        # Store for the web landing generator to pick up
        landing_path = self.config.data_dir / "telegram_landing.json"
        landing_path.write_text(json.dumps(page_data, indent=2))

        self._record_growth_action("landing_page", "update", json.dumps(page_data))
        return {"status": "updated", "data": page_data}

    def _analyze_growth(self) -> dict[str, Any]:
        """Analyze channel growth and optimize strategy."""
        if not self.channel:
            return {"status": "no_channel"}

        stats = self.channel.get_post_stats(days=7)

        # Get deal performance by category
        category_perf = self.db.execute(
            "SELECT category, COUNT(*) as posts, AVG(score) as avg_score "
            "FROM affiliate_deals WHERE status = 'posted' "
            "AND category IS NOT NULL AND category != '' "
            "GROUP BY category ORDER BY posts DESC LIMIT 10",
        )

        analysis = {
            "weekly_stats": stats,
            "top_categories": [dict(r) for r in category_perf],
        }

        # Use LLM to generate growth recommendations
        if stats.get("subscriber_count", 0) > 0 or stats.get("total_posts", 0) > 0:
            recommendations = self.think_json(
                f"Analyze this Telegram deals channel performance and suggest "
                f"3 specific growth actions:\n\n"
                f"Stats: {json.dumps(analysis)}\n\n"
                "Return: {{\"recommendations\": [{{\"action\": str, \"expected_impact\": str}}]}}",
            )
            analysis["recommendations"] = recommendations.get("recommendations", [])

        self.log_action("growth_analysis", json.dumps(analysis, default=str)[:500])
        return analysis

    def _record_growth_action(self, action_type: str, platform: str,
                              details: str) -> None:
        self.db.execute_insert(
            "INSERT INTO channel_growth_actions (action_type, platform, details) "
            "VALUES (?, ?, ?)",
            (action_type, platform, details[:1000]),
        )
