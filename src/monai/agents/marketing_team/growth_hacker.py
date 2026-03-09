"""GrowthHacker — designs and runs growth experiments.

Viral loops, referral programs, product-led growth, conversion optimization.
Thinks in experiments: hypothesis → test → measure → iterate.
"""

from __future__ import annotations

from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class GrowthHacker(BaseAgent):
    """Runs growth experiments — viral loops, referrals, conversion optimization."""

    name = "growth_hacker"
    description = "Designs and executes growth experiments: viral loops, referrals, PLG, CRO."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

    def plan(self) -> list[str]:
        return [
            "Analyze current funnel metrics",
            "Design growth experiments (hypothesis-driven)",
            "Implement and launch experiments",
            "Measure results and iterate",
        ]

    def run(self, campaign: dict | None = None, strategy: str = "",
            **kwargs: Any) -> dict[str, Any]:
        """Design and launch growth experiments."""
        if not campaign:
            return {"experiments_launched": 0}

        experiments = self.think_json(
            f"Design growth experiments for:\n"
            f"Strategy: {strategy}\n"
            f"Campaign: {campaign.get('name', '')}\n"
            f"Target audience: {campaign.get('target_audience', '')}\n\n"
            "Think like a growth hacker. What experiments can we run with "
            "minimal budget to maximize user acquisition and activation?\n\n"
            "Return JSON: {{\"experiments\": [{{\"name\": str, "
            "\"hypothesis\": str, \"type\": \"viral_loop\"|\"referral\"|"
            "\"conversion_optimization\"|\"activation\"|\"retention\", "
            "\"implementation\": str, \"success_metric\": str, "
            "\"expected_impact\": str}}]}}"
        )

        launched = experiments.get("experiments", [])

        for exp in launched:
            self.share_knowledge(
                "growth_experiment", exp.get("name", ""),
                f"Hypothesis: {exp.get('hypothesis', '')}. "
                f"Implementation: {exp.get('implementation', '')}",
                confidence=0.5,
                tags=["growth", strategy],
            )

        self.log_action("growth_experiments",
                        f"Launched {len(launched)} experiments for {strategy}")

        return {
            "experiments_launched": len(launched),
            "experiments": launched,
        }
