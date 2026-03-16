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

    def _get_product_statuses(self) -> dict[str, int]:
        """Count products by status from JSON files."""
        statuses: dict[str, int] = {}
        for path in self.bots_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                s = data.get("status", "unknown")
                statuses[s] = statuses.get(s, 0) + 1
            except (json.JSONDecodeError, OSError):
                continue
        return statuses

    def plan(self) -> list[str]:
        statuses = self._get_product_statuses()

        # Deterministic progression — always advance the pipeline
        if not statuses:
            return ["research_bot_niches"]
        if statuses.get("researched", 0) > 0:
            return ["design_bot"]
        if statuses.get("designed", 0) > 0:
            return ["build_bot"]
        if statuses.get("built", 0) > 0:
            return ["review_product"]
        if statuses.get("reviewed", 0) > 0:
            return ["deploy_bot"]
        if statuses.get("deployed", 0) > 0:
            return ["promote_bot"]
        if statuses.get("build_failed", 0) > 0:
            return ["design_bot"]  # Redesign failed builds
        if statuses.get("deploy_failed", 0) > 0:
            return ["deploy_bot"]  # Retry failed deployments

        # All bots promoted — start a new product cycle
        return ["research_bot_niches"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.log_action("run_start", "Starting Telegram bots cycle")
        steps = self.plan()
        results = {}

        step_methods = {
            "research_bot_niches": self._research_niches,
            "design_bot": self._design_bot,
            "build_bot": self._build_bot,
            "review_product": self._review_product,
            "deploy_bot": self._deploy_bot,
            "promote_bot": self._promote_bot,
        }

        for step in steps:
            fn = step_methods.get(step)
            if fn:
                results[step] = self.run_step(step, fn)

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

        # Persist top niche as actionable product file for pipeline progression
        niches = analysis.get("niches", [])
        saved = 0
        for niche in niches[:2]:  # Save top 2 niches
            name = niche.get("niche", niche.get("bot_concept", "untitled"))
            safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip()
            if not safe_name:
                continue
            path = self.bots_dir / f"{safe_name}.json"
            if not path.exists():
                path.write_text(json.dumps({
                    "research": niche,
                    "status": "researched",
                }, indent=2))
                saved += 1

        self.log_action(
            "niches_researched",
            f"Scraped {len(all_data)} data points from real sources, "
            f"identified {len(niches)} niche opportunities, saved {saved} for design"
        )
        return {
            "market_data_collected": len(all_data),
            "niches": niches,
            "raw_market_data": all_data[:20],
        }

    def _design_bot(self) -> dict[str, Any]:
        """Design a bot from a researched niche. Uses prior research data."""
        # Find a researched product to advance
        for path in self.bots_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") != "researched":
                continue

            research = data.get("research", {})
            # Inject feedback from previous review rejection if available
            review_feedback = ""
            if data.get("review"):
                from monai.agents.product_reviewer import ProductReviewer
                review_feedback = ProductReviewer.format_feedback_for_prompt(data["review"])

            spec = self.think_json(
                f"Design a Telegram bot based on this market research:\n"
                f"Niche: {research.get('niche', 'unknown')}\n"
                f"Concept: {research.get('bot_concept', 'unknown')}\n"
                f"Target audience: {research.get('target_audience', 'unknown')}\n"
                f"Pricing model: {research.get('pricing_model', 'subscription')}\n"
                f"Competition: {research.get('existing_competition', 'unknown')}\n\n"
                "Design a specific bot product. Include:\n"
                "- Clear value proposition\n"
                "- Command list and interactions\n"
                "- What data it needs/stores\n"
                "- How users pay (subscription via Stripe, etc.)\n\n"
                "Return: {\"name\": str, \"tagline\": str, "
                "\"commands\": [{\"command\": str, \"description\": str}], "
                "\"features\": [str], \"data_stored\": [str], "
                f"\"monetization\": str, \"tech_requirements\": [str]}}"
                f"{review_feedback}"
            )

            data["design"] = spec
            data["status"] = "designed"
            path.write_text(json.dumps(data, indent=2))

            name = spec.get("name", path.stem)
            self.log_action("bot_designed", name)
            return spec

        return {"status": "no_bots_to_design"}

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

    def _review_product(self) -> dict[str, Any]:
        """Quality gate: review built bot before deployment."""
        for path in self.bots_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") != "built":
                continue

            design = data.get("design", {})
            name = design.get("name", path.stem)

            result = self.reviewer.review_product(
                strategy=self.name,
                product_name=name,
                product_data=data,
                product_type="bot",
            )

            if result.verdict == "approved":
                data["status"] = "reviewed"
                data["review"] = result.to_dict()
                # Apply any improved content if available
                if result.improved_content:
                    data["improved_content"] = result.improved_content
                path.write_text(json.dumps(data, indent=2))
                self.log_action("bot_reviewed", f"{name}: APPROVED (score={result.quality_score:.2f})")
            elif result.verdict == "rejected":
                data["status"] = "designed"  # Send back to redesign
                data["review"] = result.to_dict()
                path.write_text(json.dumps(data, indent=2))
                self.log_action("bot_review_rejected", f"{name}: REJECTED — {'; '.join(result.issues[:3])}")
            else:
                # needs_revision — actively revise content before proceeding
                revised = self.reviewer.revise_product(data, result, "bot")
                data["review"] = result.to_dict()
                data["revised_content"] = revised
                data["status"] = "reviewed"
                path.write_text(json.dumps(data, indent=2))
                self.log_action("bot_revised", f"{name}: REVISED and proceeding (score={result.quality_score:.2f})")

            return result.to_dict()

        return {"status": "no_bots_to_review"}

    def _deploy_bot(self) -> dict[str, Any]:
        """Deploy a built Telegram bot by registering with BotFather and hosting it."""
        # Try to set up Stripe for in-bot payments, but don't block deployment
        # if Stripe is unavailable (e.g. behind Tor). Bot can still be deployed
        # and monetized later via Telegram Stars or direct crypto payments.
        stripe_result = self.ensure_platform_account("stripe")
        has_payments = stripe_result.get("status") not in ("blocked", "error")

        deployed = 0

        for path in self.bots_dir.glob("*.json"):
            data = json.loads(path.read_text())
            # Deploy reviewed bots (passed quality gate).
            # Previously checked for "built" which skipped ALL reviewed bots.
            if data.get("status") != "reviewed":
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
                    data["status"] = "deploy_failed"
                    data["deploy_error"] = "No bot token from BotFather"
                    path.write_text(json.dumps(data, indent=2))
                    continue

                # Validate token format: "123456:ABC-DEF..." (number:alphanumeric)
                import re as _re
                if not _re.match(r'^\d+:[A-Za-z0-9_-]{30,}$', bot_token):
                    self.log_action(
                        "bot_registration_failed", name,
                        f"Invalid bot token format: {bot_token[:20]}..."
                    )
                    data["status"] = "deploy_failed"
                    data["deploy_error"] = f"Invalid token format: {bot_token[:20]}..."
                    path.write_text(json.dumps(data, indent=2))
                    continue

                # Verify token works via Telegram API
                try:
                    import httpx
                    verify_resp = httpx.get(
                        f"https://api.telegram.org/bot{bot_token}/getMe",
                        timeout=10,
                    )
                    if verify_resp.status_code != 200 or not verify_resp.json().get("ok"):
                        self.log_action(
                            "bot_registration_failed", name,
                            f"Bot token failed getMe verification"
                        )
                        data["status"] = "deploy_failed"
                        data["deploy_error"] = "Token failed getMe API check"
                        path.write_text(json.dumps(data, indent=2))
                        continue
                except Exception as verify_err:
                    self.log_action(
                        "bot_token_verify_warning", name,
                        f"Could not verify token: {verify_err}"
                    )

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
                    # Create payment link for premium features
                    premium_price = design.get("premium_price", 4.99)
                    checkout = self.create_checkout_link(
                        amount=premium_price,
                        product=f"{name} Premium",
                        provider="kofi",
                        metadata={"bot_username": bot_username},
                    )
                    data["status"] = "deployed"
                    data["deployment"] = {
                        "bot_token": bot_token,
                        "bot_username": bot_username,
                        "deployment_url": deployment_url,
                        "checkout_url": checkout.get("checkout_url", ""),
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

    def _promote_bot(self) -> dict[str, Any]:
        """Promote deployed bots to get real users.

        Posts to Telegram bot directories, relevant communities, and
        creates a landing page describing the bot's value proposition.
        """
        promoted = 0

        for path in self.bots_dir.glob("*.json"):
            data = json.loads(path.read_text())
            if data.get("status") != "deployed":
                continue

            deployment = data.get("deployment", {})
            design = data.get("design", {})
            name = design.get("name", "untitled")
            bot_username = deployment.get("bot_username", "")

            if not bot_username:
                continue

            # Submit to Telegram bot directories
            directories = [
                ("https://t.me/BotList", "Submit bot to @BotList channel"),
                ("https://botcatalog.com/submit", "Submit to BotCatalog directory"),
            ]

            for url, desc in directories:
                try:
                    self.platform_action(
                        "telegram",
                        f"{desc}.\n"
                        f"Bot: @{bot_username}\n"
                        f"Name: {name}\n"
                        f"Description: {design.get('tagline', '')}\n"
                        f"Category: {design.get('category', 'utility')}\n"
                        f"URL: {url}",
                        f"Promoting bot @{bot_username}",
                    )
                except Exception as e:
                    self.log_action("promote_failed", f"{name} → {url}: {e}")

            # Share in relevant communities
            niche = data.get("research", {}).get("niche", "")
            if niche:
                search_results = self.search_web(
                    f"Telegram group {niche} community",
                    "Find 2-3 active Telegram groups/channels related to this niche. "
                    "Return {\"groups\": [{\"name\": str, \"url\": str}]}",
                    num_results=3,
                )
                for group in search_results.get("groups", [])[:2]:
                    try:
                        self.platform_action(
                            "telegram",
                            f"Share bot in community group.\n"
                            f"Group: {group.get('url', '')}\n"
                            f"Message: Check out @{bot_username} — "
                            f"{design.get('tagline', 'a useful Telegram bot')}. "
                            f"Free to try!",
                            f"Sharing bot in {group.get('name', 'community')}",
                        )
                    except Exception:
                        pass

            data["status"] = "promoted"
            data["promotion"] = {
                "promoted_at": __import__("datetime").datetime.now().isoformat(),
                "directories_submitted": len(directories),
            }
            path.write_text(json.dumps(data, indent=2))
            promoted += 1
            self.log_action("bot_promoted", f"@{bot_username} submitted to directories")

        return {"bots_promoted": promoted}

    def apply_improvements(self) -> dict[str, Any]:
        """Apply pending improvements from ProductIterator to existing bots.

        Rebuilds bot code via the Coder agent based on improvement plans.
        """
        pending = self.product_iterator.get_pending_improvements(self.name)
        if not pending:
            return {"status": "no_pending_improvements"}

        applied = 0
        for improvement in pending[:2]:  # Max 2 per cycle
            product_name = improvement["product_name"]
            improvements_json = improvement.get("improvements", "[]")
            try:
                plan = json.loads(improvements_json) if isinstance(improvements_json, str) else improvements_json
            except (json.JSONDecodeError, TypeError):
                plan = {}

            improvement_items = plan.get("improvements", []) if isinstance(plan, dict) else []

            # Find the bot file
            bot_path = None
            bot_data = None
            for path in self.bots_dir.glob("*.json"):
                data = json.loads(path.read_text())
                design = data.get("design", {})
                name = design.get("name", path.stem)
                if name == product_name or path.stem == product_name:
                    bot_path = path
                    bot_data = data
                    break

            if not bot_data or not bot_path:
                self.product_iterator.mark_applied(improvement["id"])
                continue

            design = bot_data.get("design", {})
            change_descriptions = [
                f"- {item.get('area', 'general')}: {item.get('specific_change', item.get('current_issue', ''))}"
                for item in improvement_items
            ]
            changes_text = "\n".join(change_descriptions) if change_descriptions else "General quality improvements"

            rebuild_spec = (
                f"Improve the Telegram bot: {product_name}\n"
                f"Original spec: {json.dumps(design, default=str)[:2000]}\n\n"
                f"REQUIRED IMPROVEMENTS:\n{changes_text}\n\n"
                f"Rebuild the bot with these improvements applied. "
                f"Keep existing commands and functionality working."
            )

            build_result = self.coder.generate_module(rebuild_spec)

            if build_result.get("status") == "success":
                bot_data["build"] = build_result
                bot_data["status"] = "built"  # Re-enter review pipeline
                bot_data.setdefault("iteration_history", []).append({
                    "iteration_id": improvement["id"],
                    "improvements_applied": change_descriptions,
                    "rebuild_status": "success",
                })
                bot_path.write_text(json.dumps(bot_data, indent=2))
                applied += 1
                self.log_action(
                    "bot_improved",
                    f"{product_name}: rebuilt with {len(improvement_items)} improvements",
                )

            self.product_iterator.mark_applied(improvement["id"])

        return {"applied": applied, "total_pending": len(pending)}
