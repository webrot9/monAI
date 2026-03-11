"""ContentMarketer — creates SEO content, blog posts, social posts.

Real autonomous capabilities:
- Performs real SEO keyword research via web search (Google autocomplete, related searches)
- Analyzes top-ranking content for target keywords (real SERP scraping)
- Publishes content to real platforms via platform_action
- Tracks content performance in the DB
- Writes content informed by real competitor content analysis
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


class ContentMarketer(BaseAgent):
    """Creates and publishes marketing content across channels.

    Uses real SEO research and platform publishing, not just LLM content generation.
    """

    name = "content_marketer"
    description = "Writes SEO-optimized blog posts, social content, and email sequences."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return [
            "Research real SEO keywords via web search",
            "Analyze top-ranking content for target keywords",
            "Plan content pieces based on real keyword data",
            "Create content optimized for real search intent",
            "Publish content to platforms via platform_action",
        ]

    def run(self, campaign: dict | None = None, strategy: str = "",
            **kwargs: Any) -> dict[str, Any]:
        """Create content for a marketing campaign with real SEO research.

        Layer 1: Real keyword research via web scraping
        Layer 2: Real SERP analysis of top-ranking content
        Layer 3: LLM content creation informed by real data
        Layer 4: Real publishing via platform_action
        """
        if not campaign:
            return {"pieces_created": 0}

        # ── Layer 1: Real SEO keyword research ─────────────────────
        niche = campaign.get("key_message", strategy)
        keyword_data = self._research_keywords(niche)

        # ── Layer 2: Real SERP analysis ────────────────────────────
        top_keywords = keyword_data.get("keywords", [])[:3]
        serp_analysis = {}
        for kw in top_keywords:
            kw_text = kw if isinstance(kw, str) else kw.get("keyword", "")
            if kw_text:
                serp_analysis[kw_text] = self._analyze_serp(kw_text)

        # ── Layer 3: Content planning with real data ───────────────
        keyword_context = json.dumps(keyword_data, default=str)[:800]
        serp_context = json.dumps(serp_analysis, default=str)[:800]

        content_plan = self.think_json(
            f"Plan content pieces for this campaign:\n"
            f"Campaign: {campaign.get('name', '')}\n"
            f"Strategy: {strategy}\n"
            f"Target audience: {campaign.get('target_audience', '')}\n"
            f"Channel: {campaign.get('channel', '')}\n"
            f"Key message: {campaign.get('key_message', '')}\n\n"
            f"REAL KEYWORD DATA:\n{keyword_context}\n\n"
            f"REAL SERP ANALYSIS (what's ranking):\n{serp_context}\n\n"
            "Plan content that targets REAL keywords and fills gaps in existing content.\n\n"
            "Return JSON: {{\"pieces\": [{{\"type\": \"blog_post\"|\"social_post\"|"
            "\"email_sequence\"|\"landing_page\", \"title\": str, "
            "\"outline\": str, \"platform\": str, \"seo_keywords\": [str], "
            "\"target_search_intent\": str, \"content_gap_addressed\": str}}]}}"
        )

        pieces = content_plan.get("pieces", [])
        created = 0
        published = 0

        for piece in pieces:
            # Write content informed by real SERP data
            body = self.think(
                f"Write a {piece.get('type', 'blog_post')} with this outline:\n"
                f"Title: {piece.get('title', '')}\n"
                f"Outline: {piece.get('outline', '')}\n"
                f"SEO keywords: {', '.join(piece.get('seo_keywords', []))}\n"
                f"Target audience: {campaign.get('target_audience', '')}\n"
                f"Search intent: {piece.get('target_search_intent', '')}\n"
                f"Content gap to fill: {piece.get('content_gap_addressed', '')}\n\n"
                "Write engaging, high-quality content. NO AI slop. "
                "Sound like an expert, not a robot. "
                "Naturally incorporate the SEO keywords without keyword stuffing."
            )

            # Store the content
            campaign_id = campaign.get("id")
            content_id = self.db.execute_insert(
                "INSERT INTO marketing_content "
                "(campaign_id, content_type, title, body, platform, status) "
                "VALUES (?, ?, ?, ?, ?, 'draft')",
                (campaign_id, piece.get("type", "blog_post"),
                 piece.get("title", ""), body,
                 piece.get("platform", "")),
            )
            created += 1

            # ── Layer 4: Real publishing via platform_action ───────
            platform = piece.get("platform", "")
            if platform and piece.get("type") in ("blog_post", "social_post"):
                publish_result = self._publish_content(
                    platform, piece.get("title", ""), body, piece.get("type", ""),
                )
                if publish_result.get("status") != "error":
                    published += 1
                    self.db.execute(
                        "UPDATE marketing_content SET status = 'published' WHERE id = ?",
                        (content_id,),
                    )

        self.log_action("create_content",
                        f"Created {created}, published {published} for {strategy}",
                        f"keywords_researched={len(top_keywords)}")
        return {
            "pieces_created": created,
            "pieces_published": published,
            "pieces": pieces,
            "keywords_researched": keyword_data.get("keywords", []),
        }

    # ── Real SEO research methods ──────────────────────────────────

    def _research_keywords(self, niche: str) -> dict[str, Any]:
        """Research real SEO keywords via web search."""
        try:
            return self.search_web(
                query=f"{niche} keywords search volume SEO 2025",
                extraction_prompt=(
                    f"Find SEO keyword data for the '{niche}' topic. "
                    "Extract: high-volume keywords, long-tail keywords, "
                    "related search queries, question-based keywords (what, how, why). "
                    "Return: {\"keywords\": [{\"keyword\": str, \"estimated_volume\": str, "
                    "\"difficulty\": str, \"intent\": str}], "
                    "\"long_tail\": [str], \"questions\": [str]}"
                ),
                num_results=3,
            )
        except Exception as e:
            logger.warning(f"Keyword research failed for '{niche}': {e}")
            return {"keywords": [], "error": str(e)}

    def _analyze_serp(self, keyword: str) -> dict[str, Any]:
        """Analyze what's currently ranking for a keyword."""
        try:
            return self.search_web(
                query=keyword,
                extraction_prompt=(
                    f"Analyze the top search results for '{keyword}'. "
                    "For each of the top 5 results: what's the title, "
                    "what angle do they take, what content format (listicle, guide, "
                    "comparison, tutorial)? What's missing that we could write about? "
                    "Return: {\"top_results\": [{\"title\": str, \"angle\": str, "
                    "\"format\": str}], \"content_gaps\": [str], "
                    "\"dominant_format\": str}"
                ),
                num_results=5,
            )
        except Exception as e:
            logger.warning(f"SERP analysis failed for '{keyword}': {e}")
            return {"error": str(e)}

    def _publish_content(self, platform: str, title: str, body: str,
                         content_type: str) -> dict[str, Any]:
        """Publish content to a real platform via platform_action."""
        try:
            action = (
                f"Publish a {content_type} titled '{title}'. "
                f"Content body (post this exactly, with formatting):\n\n{body[:3000]}"
            )
            result = self.platform_action(
                platform=platform,
                action_description=action,
                context=f"Content type: {content_type}",
            )
            self.log_action("publish_content", f"{platform}: {title[:80]}",
                            json.dumps(result, default=str)[:300])
            return result
        except Exception as e:
            logger.warning(f"Failed to publish to {platform}: {e}")
            return {"status": "error", "error": str(e)}
