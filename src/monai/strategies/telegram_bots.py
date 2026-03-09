"""Telegram Bots strategy agent — builds and sells bots as a service.

Creates Telegram bots for specific niches (productivity, crypto tracking,
content scheduling, etc.) and offers them as paid services.
"""

from __future__ import annotations

import json
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM


class TelegramBotAgent(BaseAgent):
    name = "telegram_bots"
    description = (
        "Creates and sells Telegram bots as paid services. "
        "Targets specific niches like productivity, crypto, notifications, "
        "content scheduling. Low overhead, recurring revenue potential."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.bots_dir = config.data_dir / "telegram_bots"
        self.bots_dir.mkdir(parents=True, exist_ok=True)

    def plan(self) -> list[str]:
        existing = list(self.bots_dir.glob("*.json"))
        plan = self.think_json(
            f"I have {len(existing)} Telegram bot products. Plan next actions.\n"
            "Return: {\"steps\": [str]}.\n"
            "Options: research_bot_niches, design_bot, build_bot, "
            "deploy_bot, market_bot, analyze_usage.",
        )
        return plan.get("steps", ["research_bot_niches"])

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting Telegram bots cycle")
        steps = self.plan()
        results = {}

        for step in steps:
            if step == "research_bot_niches":
                results["research"] = self._research_niches()
            elif step == "design_bot":
                results["design"] = self._design_bot()
            elif step == "build_bot":
                results["build"] = self._build_bot()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_niches(self) -> dict[str, Any]:
        """Research profitable Telegram bot niches."""
        niches = self.think_json(
            "Research 5 profitable Telegram bot niches. Focus on:\n"
            "- Bots people would pay for (subscription model)\n"
            "- Bots that solve real problems\n"
            "- Niches with existing demand (check what bots exist)\n"
            "- Bots an AI can build and maintain autonomously\n\n"
            "Categories: productivity, finance, crypto, notifications, "
            "content scheduling, social media, customer support, analytics.\n\n"
            "Return: {\"niches\": [{\"niche\": str, \"bot_concept\": str, "
            "\"target_audience\": str, \"pricing_model\": str, "
            "\"monthly_price\": float, \"estimated_users\": int, "
            "\"build_complexity\": \"low\"|\"medium\"|\"high\"}]}"
        )
        return niches

    def _design_bot(self) -> dict[str, Any]:
        """Design a specific Telegram bot product."""
        spec = self.think_json(
            "Design a Telegram bot product to build. Include:\n"
            "- Clear value proposition\n"
            "- Command list and interactions\n"
            "- What data it needs/stores\n"
            "- How users pay (subscription via Stripe, etc.)\n\n"
            "Return: {\"name\": str, \"tagline\": str, "
            "\"commands\": [{\"command\": str, \"description\": str}], "
            "\"features\": [str], \"data_stored\": [str], "
            "\"monetization\": str, \"tech_requirements\": [str]}"
        )

        name = spec.get("name", "untitled_bot")
        safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip()
        path = self.bots_dir / f"{safe_name}.json"
        path.write_text(json.dumps({"design": spec, "status": "designed"}, indent=2))

        self.log_action("bot_designed", name)
        return spec

    def _build_bot(self) -> dict[str, Any]:
        """Build a designed Telegram bot using the Coder agent."""
        for path in self.bots_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") == "designed":
                design = data["design"]
                name = design.get("name", "untitled")

                spec = (
                    f"Build a Telegram bot: {name}\n"
                    f"Tagline: {design.get('tagline', '')}\n"
                    f"Commands: {json.dumps(design.get('commands', []))}\n"
                    f"Features: {json.dumps(design.get('features', []))}\n\n"
                    "Use python-telegram-bot library. Include:\n"
                    "- Command handlers for all listed commands\n"
                    "- Proper error handling\n"
                    "- SQLite storage for user data\n"
                    "- /start and /help commands\n"
                    "- Graceful shutdown"
                )

                build_result = self.coder.generate_module(spec)
                data["build"] = build_result
                data["status"] = "built" if build_result.get("status") == "success" else "build_failed"
                path.write_text(json.dumps(data, indent=2))

                self.log_action("bot_built", name, build_result.get("status", "unknown"))
                return {"bot": name, "build_status": build_result.get("status")}

        return {"status": "no_bots_to_build"}
