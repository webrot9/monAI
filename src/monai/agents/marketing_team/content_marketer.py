"""ContentMarketer — creates SEO content, blog posts, social posts.

Executes content campaigns: writes, optimizes for SEO, publishes.
Uses the Humanizer (via orchestrator) to ensure content passes AI detection.
"""

from __future__ import annotations

from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class ContentMarketer(BaseAgent):
    """Creates and publishes marketing content across channels."""

    name = "content_marketer"
    description = "Writes SEO-optimized blog posts, social content, and email sequences."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return [
            "Analyze campaign brief and target audience",
            "Research keywords and content angles",
            "Create content pieces (blog, social, email)",
            "Optimize for SEO and engagement",
        ]

    def run(self, campaign: dict | None = None, strategy: str = "",
            **kwargs: Any) -> dict[str, Any]:
        """Create content for a marketing campaign."""
        if not campaign:
            return {"pieces_created": 0}

        content_plan = self.think_json(
            f"Plan content pieces for this campaign:\n"
            f"Campaign: {campaign.get('name', '')}\n"
            f"Strategy: {strategy}\n"
            f"Target audience: {campaign.get('target_audience', '')}\n"
            f"Channel: {campaign.get('channel', '')}\n"
            f"Key message: {campaign.get('key_message', '')}\n\n"
            "Return JSON: {{\"pieces\": [{{\"type\": \"blog_post\"|\"social_post\"|"
            "\"email_sequence\"|\"landing_page\", \"title\": str, "
            "\"outline\": str, \"platform\": str, \"seo_keywords\": [str]}}]}}"
        )

        pieces = content_plan.get("pieces", [])
        created = 0

        for piece in pieces:
            body = self.think(
                f"Write a {piece.get('type', 'blog_post')} with this outline:\n"
                f"Title: {piece.get('title', '')}\n"
                f"Outline: {piece.get('outline', '')}\n"
                f"SEO keywords: {', '.join(piece.get('seo_keywords', []))}\n"
                f"Target audience: {campaign.get('target_audience', '')}\n\n"
                "Write engaging, high-quality content. NO AI slop. "
                "Sound like an expert, not a robot."
            )

            # Store the content
            campaign_id = campaign.get("id")
            self.db.execute_insert(
                "INSERT INTO marketing_content "
                "(campaign_id, content_type, title, body, platform, status) "
                "VALUES (?, ?, ?, ?, ?, 'draft')",
                (campaign_id, piece.get("type", "blog_post"),
                 piece.get("title", ""), body,
                 piece.get("platform", "")),
            )
            created += 1

        self.log_action("create_content", f"Created {created} pieces for {strategy}")
        return {"pieces_created": created, "pieces": pieces}
