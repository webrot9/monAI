"""OutreachSpecialist — cold outreach, partnerships, influencer collaboration.

Handles: cold email campaigns, partnership proposals, influencer outreach.
Every message must be personalized — no spray and pray.
"""

from __future__ import annotations

from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class OutreachSpecialist(BaseAgent):
    """Executes personalized outreach campaigns — cold email, partnerships, influencers."""

    name = "outreach_specialist"
    description = "Runs personalized cold outreach, partnership proposals, and influencer collaborations."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return [
            "Identify outreach targets from research briefs",
            "Craft personalized outreach messages",
            "Execute outreach sequences",
            "Track responses and follow up",
        ]

    def run(self, campaign: dict | None = None, strategy: str = "",
            **kwargs: Any) -> dict[str, Any]:
        """Execute outreach for a campaign."""
        if not campaign:
            return {"messages_sent": 0}

        outreach_plan = self.think_json(
            f"Plan outreach for:\n"
            f"Strategy: {strategy}\n"
            f"Campaign: {campaign.get('name', '')}\n"
            f"Target audience: {campaign.get('target_audience', '')}\n\n"
            "Design personalized outreach. NEVER spray and pray. "
            "Each message must show we understand the recipient.\n\n"
            "Return JSON: {{\"outreach_sequences\": [{{\"target_type\": str, "
            "\"channel\": \"email\"|\"linkedin\"|\"twitter\"|\"partnership\", "
            "\"message_template\": str, \"personalization_fields\": [str], "
            "\"follow_up_days\": int, \"expected_response_rate\": float}}]}}"
        )

        sequences = outreach_plan.get("outreach_sequences", [])
        total_messages = len(sequences)

        for seq in sequences:
            self.share_knowledge(
                "outreach_template", seq.get("target_type", ""),
                f"Channel: {seq.get('channel')}. "
                f"Expected response: {seq.get('expected_response_rate', 0):.0%}",
                confidence=0.5,
                tags=["outreach", strategy],
            )

        self.log_action("outreach_plan",
                        f"Planned {total_messages} sequences for {strategy}")

        return {
            "messages_sent": total_messages,
            "sequences": sequences,
        }
