"""Telegram Bots strategy agent — builds and sells bots as a service.

Creates Telegram bots for specific niches (productivity, crypto tracking,
content scheduling, etc.) and offers them as paid services.
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
            elif step == "deploy_bot":
                results["deploy"] = self._deploy_bot()

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _research_niches(self) -> dict[str, Any]:
        """Research REAL popular Telegram bots and find gaps in the market."""
        all_data = []

        # Browse Telegram bot directories to see what's popular
        botlist_data = self.browse_and_extract(
            "https://t.me/s/BotList",
            "Analyze the Telegram Bot List channel. For each bot listed extract:\n"
            "- bot_name: the bot's name\n"
            "- bot_username: the @username\n"
            "- category: what category/niche it serves\n"
            "- description: what the bot does\n"
            "- popularity_indicators: any subscriber/user counts visible\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"bots\": [...]}"
        )
        for bot in botlist_data.get("bots", []):
            bot["source"] = "telegram_botlist"
            all_data.append(bot)

        # Search for Telegram bot directories and rankings
        directory_data = self.search_web(
            "best Telegram bots 2025 2026 most popular categories ranking",
            "Find real rankings and lists of popular Telegram bots. For each extract:\n"
            "- bot_name: the bot's name\n"
            "- category: what niche it serves\n"
            "- user_count: how many users if mentioned\n"
            "- what_it_does: brief description\n"
            "- monetization: how it makes money if mentioned\n"
            "- source_url: where this info was found\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"bots\": [...]}"
        )
        for bot in directory_data.get("bots", []):
            bot["source"] = "web_rankings"
            all_data.append(bot)

        # Browse a Telegram bot store/catalog for market analysis
        store_data = self.browse_and_extract(
            "https://botcatalog.com/",
            "Analyze the bot catalog. Find which categories have the most bots "
            "and which have gaps. For each category extract:\n"
            "- category: the category name\n"
            "- bot_count: how many bots in this category\n"
            "- top_bots: names of the most popular bots\n"
            "- saturation_level: high/medium/low based on number of bots\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"categories\": [...]}"
        )
        for cat in store_data.get("categories", []):
            cat["source"] = "bot_catalog"
            all_data.append(cat)

        # Search for what people are requesting but can't find
        demand_data = self.search_web(
            "Telegram bot idea request needed 2025 site:reddit.com OR site:t.me",
            "Find real forum posts, discussions, or requests where people are "
            "looking for Telegram bots that don't exist yet. For each extract:\n"
            "- requested_bot: what bot people want\n"
            "- source_url: where the request was found\n"
            "- num_interested: how many people showed interest\n"
            "- use_case: what problem they want solved\n\n"
            "Only include REAL data visible on the page. Do NOT make up any information.\n"
            "Return as JSON: {\"requests\": [...]}"
        )
        for req in demand_data.get("requests", []):
            req["source"] = "user_requests"
            all_data.append(req)

        # Use LLM to ANALYZE the real data and identify the best gaps
        analysis = self.think_json(
            f"Based on this REAL market research on Telegram bots, identify the 5 "
            f"best bot niches to build for:\n\n"
            f"Market data: {json.dumps(all_data, default=str)[:3000]}\n\n"
            "Focus on gaps — categories with high demand but few good bots, "
            "or underserved user requests.\n\n"
            "Return: {\"niches\": [{\"niche\": str, \"bot_concept\": str, "
            "\"target_audience\": str, \"pricing_model\": str, "
            "\"monthly_price\": float, \"evidence\": str, "
            "\"existing_competition\": str, "
            "\"build_complexity\": \"low\"|\"medium\"|\"high\"}]}"
        )

        self.log_action(
            "niches_researched",
            f"Scraped {len(all_data)} data points from real sources, "
            f"identified {len(analysis.get('niches', []))} niche opportunities"
        )
        return {
            "market_data_collected": len(all_data),
            "niches": analysis.get("niches", []),
            "raw_market_data": all_data[:20],
        }

    def _design_bot(self) -> dict[str, Any]:
        """Design a specific Telegram bot product. LLM-based planning is legitimate."""
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

    def _deploy_bot(self) -> dict[str, Any]:
        """Deploy a built Telegram bot by registering with BotFather and hosting it."""
        deployed = 0

        for path in self.bots_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") != "built":
                continue

            design = data["design"]
            name = design.get("name", "untitled")
            build = data.get("build", {})

            # Step 1: Register the bot with BotFather via Telegram
            try:
                botfather_result = self.execute_task(
                    f"Register a new Telegram bot with @BotFather.\n"
                    f"Bot name: {name}\n"
                    f"Bot description: {design.get('tagline', '')}\n"
                    f"Commands to register:\n"
                    + "\n".join(
                        f"  {cmd['command']} - {cmd['description']}"
                        for cmd in design.get("commands", [])
                    )
                    + "\n\n"
                    "Steps:\n"
                    "1. Open Telegram and message @BotFather\n"
                    "2. Send /newbot\n"
                    "3. Provide the bot name and username\n"
                    "4. Save the API token returned\n"
                    "5. Send /setcommands to register the command list\n"
                    "6. Send /setdescription to set the bot description\n\n"
                    "Return the bot token and username.",
                    f"Registering Telegram bot: {name}"
                )

                bot_token = botfather_result.get("bot_token", "")
                bot_username = botfather_result.get("bot_username", "")

                if not bot_token:
                    self.log_action(
                        "bot_registration_failed", name,
                        "No bot token returned from BotFather registration"
                    )
                    continue

            except Exception as e:
                self.log_action(
                    "bot_registration_failed", name, str(e)[:300]
                )
                self.learn_from_error(e, f"Registering bot '{name}' with BotFather")
                continue

            # Step 2: Deploy the bot code to a hosting platform
            try:
                bot_code_path = build.get("output_path", "")
                deploy_result = self.execute_task(
                    f"Deploy a Telegram bot to a hosting platform.\n"
                    f"Bot name: {name}\n"
                    f"Bot token: {bot_token}\n"
                    f"Bot code location: {bot_code_path}\n\n"
                    "Steps:\n"
                    "1. Set up environment variables (BOT_TOKEN, DATABASE_URL)\n"
                    "2. Deploy to a suitable hosting platform (Railway, Render, or a VPS)\n"
                    "3. Ensure the bot process starts and stays running\n"
                    "4. Verify the bot responds to /start command\n"
                    "5. Set up a webhook or use polling mode\n\n"
                    "Return the deployment URL and status.",
                    f"Deploying Telegram bot: {name}"
                )

                deployment_url = deploy_result.get("url", "")
                deployment_status = deploy_result.get("status", "unknown")

                if deployment_status == "success":
                    data["status"] = "deployed"
                    data["deployment"] = {
                        "bot_token": bot_token,
                        "bot_username": bot_username,
                        "deployment_url": deployment_url,
                        "hosting_platform": deploy_result.get("platform", ""),
                    }
                    path.write_text(json.dumps(data, indent=2))
                    deployed += 1
                    self.log_action(
                        "bot_deployed", name,
                        f"username=@{bot_username} url={deployment_url}"
                    )
                else:
                    data["status"] = "deploy_failed"
                    data["deploy_error"] = deploy_result.get("error", "Unknown deployment error")
                    path.write_text(json.dumps(data, indent=2))
                    self.log_action(
                        "bot_deploy_failed", name,
                        f"status={deployment_status}"
                    )

            except Exception as e:
                data["status"] = "deploy_failed"
                data["deploy_error"] = str(e)[:500]
                path.write_text(json.dumps(data, indent=2))
                self.log_action(
                    "bot_deploy_failed", name, str(e)[:300]
                )
                self.learn_from_error(e, f"Deploying bot '{name}'")

        self.log_action("deploy_bots_complete", f"{deployed} bots deployed")
        return {"bots_deployed": deployed}
