"""Orchestrator agent — the brain of monAI.

Fully autonomous. Decides what to do, provisions its own infrastructure,
spawns sub-agents, discovers opportunities, runs strategies, and scales.
Zero human intervention required.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.agents.identity import IdentityManager
from monai.agents.provisioner import Provisioner
from monai.agents.spawner import AgentSpawner
from monai.business.commercialista import Commercialista
from monai.business.crm import CRM
from monai.business.finance import Finance
from monai.business.risk import RiskManager
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM, get_cost_tracker
from monai.utils.privacy import get_anonymizer
from monai.utils.resources import check_resources
from monai.utils.sandbox import PROJECT_ROOT

logger = logging.getLogger(__name__)


class Orchestrator(BaseAgent):
    name = "orchestrator"
    description = (
        "Fully autonomous master agent. Provisions its own infrastructure, "
        "discovers opportunities, spawns sub-agents, runs strategies, "
        "manages clients, and scales — all without human intervention."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self.crm = CRM(db)
        self.finance = Finance(db)
        self.risk = RiskManager(config, db)
        self.identity = IdentityManager(config, db, llm)
        self.provisioner = Provisioner(config, db, llm)
        self.spawner = AgentSpawner(config, db, llm)
        self.commercialista = Commercialista(config, db)
        self._strategy_agents: dict[str, BaseAgent] = {}

    def register_strategy(self, agent: BaseAgent):
        self._strategy_agents[agent.name] = agent
        self.log_action("register_strategy", f"Registered: {agent.name}")

    def plan(self) -> list[str]:
        """Generate the orchestrator's full action plan for this cycle."""
        health = self.risk.get_portfolio_health()
        identity = self.identity.get_identity()
        accounts = self.identity.get_all_accounts()
        resource_costs = self.identity.get_monthly_resource_costs()
        pipeline = self.crm.get_pipeline_summary()

        context = json.dumps({
            "portfolio_health": health,
            "identity": identity,
            "accounts_count": len(accounts),
            "accounts": [{"platform": a["platform"], "type": a["type"]} for a in accounts],
            "monthly_resource_costs": resource_costs,
            "client_pipeline": pipeline,
            "total_revenue": self.finance.get_total_revenue(),
            "total_expenses": self.finance.get_total_expenses(),
            "net_profit": self.finance.get_net_profit(),
        }, indent=2, default=str)

        plan_response = self.think_json(
            "You are a fully autonomous AI business. Plan your next cycle. "
            "Consider ALL of the following:\n"
            "1. INFRASTRUCTURE: Do I need accounts, emails, domains, API keys? Provision them.\n"
            "2. OPPORTUNITIES: What new ways to make money should I explore?\n"
            "3. CLIENT WORK: Who needs follow-up? What work needs delivering?\n"
            "4. MARKETING: Should I do outreach, post content, bid on jobs?\n"
            "5. OPTIMIZATION: Which strategies are working? Scale winners, cut losers.\n"
            "6. NEW STRATEGIES: Should I start something entirely new?\n\n"
            "Return: {\"actions\": [{\"action\": str, \"priority\": int (1=highest), "
            "\"reason\": str, \"delegate_to_subagent\": bool}]}",
            context=context,
        )
        actions = plan_response.get("actions", [])
        self.log_action("plan", f"Generated {len(actions)} actions", json.dumps(actions))
        return actions

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Execute one full autonomous orchestration cycle."""
        self._cycle += 1
        self.log_action("cycle_start", f"Cycle {self._cycle} at {datetime.now()}")
        self.journal("plan", f"Starting cycle {self._cycle}")
        cycle_result = {}

        # Phase -2: Anonymity check — creator must NEVER be traceable
        anonymizer = get_anonymizer(self.config)
        if self.config.privacy.verify_anonymity and self.config.privacy.proxy_type != "none":
            anon_status = anonymizer.startup_check()
            cycle_result["anonymity"] = anon_status
            if not anon_status.get("anonymous"):
                self.log_action("ANONYMITY_FAILED", json.dumps(anon_status))
                self.learn("alert", "Anonymity check failed",
                           f"Cannot verify anonymous proxy. Status: {anon_status}",
                           rule="NEVER operate without verified anonymity", severity="critical")
                return {"status": "aborted", "reason": "anonymity_check_failed", **cycle_result}
            self.log_action("anonymity_ok",
                            f"Visible IP: {anon_status.get('visible_ip', 'unknown')}")
        else:
            cycle_result["anonymity"] = {"status": "skipped"}

        # Phase -1: Resource check — don't damage the creator's computer
        resources = check_resources(PROJECT_ROOT, self.config.data_dir)
        cycle_result["resources"] = resources
        if not resources["all_ok"]:
            self.log_action("RESOURCE_LIMIT", json.dumps(resources))
            self.learn("alert", "Resource limit exceeded", json.dumps(resources),
                       rule="Reduce resource usage before next cycle", severity="critical")
            return {"status": "aborted", "reason": "resource_limits_exceeded", **cycle_result}

        # Phase -0.5: Budget check — can we afford to operate?
        budget = self.commercialista.get_budget()
        cycle_result["budget"] = budget
        if budget["balance"] <= 0:
            self.log_action("BUDGET_EXHAUSTED", json.dumps(budget))
            self.learn("alert", "Budget exhausted",
                       f"Balance: €{budget['balance']:.2f}. Cannot spend more.",
                       rule="Focus only on zero-cost revenue activities", severity="critical")
            # Don't abort entirely — still allow zero-cost operations
        self.log_action("budget_check",
                        f"€{budget['balance']:.2f} remaining, "
                        f"burn: €{budget['burn_rate_daily']:.4f}/day")

        # Phase 0: Start cycle for all registered agents (process messages, load lessons)
        for agent in self._strategy_agents.values():
            agent.start_cycle(self._cycle)
        self.start_cycle(self._cycle)

        # Phase 0.5: Process inter-agent requests (help requests, handoffs)
        cycle_result["collaboration"] = self._process_agent_requests()

        # Phase 1: Self-check — do I have what I need?
        cycle_result["provisioning"] = self._ensure_infrastructure()

        # Phase 2: Health check
        health = self.risk.get_portfolio_health()
        cycle_result["health"] = health
        self.log_action("health_check", json.dumps(health, default=str)[:500])

        # Phase 3: Review active strategies
        cycle_result["reviews"] = self._review_strategies()

        # Phase 4: Plan and execute
        actions = self.plan()
        action_results = []
        subagent_tasks = []

        for action_item in sorted(actions, key=lambda x: x.get("priority", 99)):
            action = action_item["action"]
            if action_item.get("delegate_to_subagent"):
                subagent_tasks.append({"name": action.replace(" ", "_")[:30], "task": action})
            else:
                result = self._execute_action(action)
                action_results.append({"action": action, "result": result})

        cycle_result["direct_actions"] = action_results

        # Phase 5: Run delegated tasks via sub-agents (parallel)
        if subagent_tasks:
            loop = asyncio.new_event_loop()
            try:
                subagent_results = loop.run_until_complete(
                    self.spawner.run_parallel(subagent_tasks)
                )
                cycle_result["subagent_results"] = {
                    k: {"status": v.get("status")} for k, v in subagent_results.items()
                }
            except Exception as e:
                logger.error(f"Sub-agent execution failed: {e}")
                cycle_result["subagent_results"] = {"error": str(e)}
            finally:
                loop.close()

        # Phase 6: Run registered strategy agents
        cycle_result["strategy_results"] = self._run_strategies()

        # Phase 7: Commercialista report
        cycle_result["timestamp"] = datetime.now().isoformat()
        cycle_result["net_profit"] = self.finance.get_net_profit()
        api_costs = get_cost_tracker().get_summary()
        cycle_result["api_costs_session"] = api_costs
        updated_budget = self.commercialista.get_budget()
        cycle_result["budget_after"] = updated_budget
        self.log_action("commercialista",
                        f"API calls: {api_costs['total_calls']}, "
                        f"cost: €{api_costs['total_cost_eur']:.4f}, "
                        f"budget: €{updated_budget['balance']:.2f}")

        # Phase 8: Reflect and learn
        self._reflect_on_cycle(cycle_result)

        # Phase 9: Share cycle summary with all agents
        self.broadcast(
            "info",
            f"Cycle {self._cycle} complete",
            json.dumps({
                "net_profit": cycle_result["net_profit"],
                "active_strategies": cycle_result.get("health", {}).get("active_strategies", 0),
                "actions_taken": len(action_results),
            }, default=str),
        )

        self.journal("execute", f"Cycle {self._cycle} complete",
                     {"net_profit": cycle_result["net_profit"]},
                     outcome="success")
        self.log_action("cycle_complete", json.dumps(cycle_result, default=str)[:1000])
        return cycle_result

    def _ensure_infrastructure(self) -> dict[str, Any]:
        """Check and provision any missing infrastructure."""
        identity = self.identity.get_identity()
        accounts = self.identity.get_all_accounts()
        account_types = {a["platform"] for a in accounts}

        needs = []
        if "email" not in account_types:
            needs.append("email")
        if not identity:
            needs.append("identity")

        if needs:
            self.log_action("provisioning", f"Need to set up: {needs}")
            # Run provisioner
            result = self.provisioner.run()
            return {"provisioned": needs, "result": result}

        return {"status": "infrastructure_ok", "accounts": len(accounts)}

    def discover_opportunities(self) -> list[dict[str, Any]]:
        """Discover new money-making opportunities."""
        current = self.db.execute("SELECT name, category, status FROM strategies")
        current_list = [dict(r) for r in current]
        identity = self.identity.get_identity()

        response = self.think_json(
            "Brainstorm 5 NEW money-making opportunities. Think creatively. "
            "Consider ANYTHING legal: services, products, trading, content, "
            "affiliate marketing, SaaS, automation, consulting, reselling, "
            "domain flipping, social media, courses, newsletter monetization, "
            "API services, data products, and anything else. "
            "For each: {\"opportunities\": [{\"name\": str, \"category\": str, "
            "\"description\": str, \"how_to_start\": str, "
            "\"estimated_monthly_revenue\": float, \"startup_cost\": float, "
            "\"risk_level\": str, \"time_to_first_revenue_days\": int, "
            "\"platforms_needed\": [str], \"can_automate\": bool}]}",
            context=f"Current strategies: {json.dumps(current_list)}\nIdentity: {json.dumps(identity, default=str)}",
        )
        opportunities = response.get("opportunities", [])
        self.log_action("discover", f"Found {len(opportunities)} opportunities")
        return opportunities

    def _review_strategies(self) -> list[dict[str, Any]]:
        reviews = []
        strategies = self.db.execute("SELECT * FROM strategies WHERE status = 'active'")
        for strategy in strategies:
            s = dict(strategy)
            pause_check = self.risk.should_pause_strategy(s["id"])
            roi = self.finance.get_roi(s["id"])
            review = {
                "strategy": s["name"],
                "roi": roi,
                "should_pause": pause_check["should_pause"],
                "reasons": pause_check["reasons"],
            }
            if pause_check["should_pause"]:
                self.log_action("pause_strategy", s["name"], json.dumps(pause_check["reasons"]))
                self.db.execute(
                    "UPDATE strategies SET status = 'paused', updated_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), s["id"]),
                )
            reviews.append(review)
        return reviews

    def _execute_action(self, action: str) -> str:
        if action == "discover_opportunities":
            opportunities = self.discover_opportunities()
            # Auto-evaluate and start promising ones
            for opp in opportunities:
                if opp.get("startup_cost", 999) <= self.config.risk.max_monthly_spend_new_strategy:
                    self._start_new_strategy(opp)
            return f"Discovered {len(opportunities)} opportunities"
        elif action == "rebalance":
            return self._rebalance()
        elif action == "follow_up_clients":
            return self._follow_up_clients()
        elif "provision" in action.lower() or "register" in action.lower():
            result = self.provisioner.run()
            return json.dumps(result, default=str)[:500]
        elif "marketing" in action.lower() or "outreach" in action.lower():
            return self._do_marketing(action)
        else:
            return f"Action '{action}' queued"

    def _start_new_strategy(self, opportunity: dict) -> str:
        """Automatically start a new strategy from a discovered opportunity."""
        name = opportunity.get("name", "").lower().replace(" ", "_")[:30]
        existing = self.db.execute("SELECT id FROM strategies WHERE name = ?", (name,))
        if existing:
            return f"Strategy {name} already exists"

        self.db.execute_insert(
            "INSERT INTO strategies (name, category, description, allocated_budget) VALUES (?, ?, ?, ?)",
            (name, opportunity.get("category", "misc"),
             opportunity.get("description", ""),
             min(opportunity.get("startup_cost", 10), self.config.risk.max_monthly_spend_new_strategy)),
        )
        self.log_action("start_strategy", name, json.dumps(opportunity, default=str)[:500])
        return f"Started new strategy: {name}"

    def _rebalance(self) -> str:
        pnl = self.finance.get_strategy_pnl()
        winners = [s for s in pnl if s["net"] > 0]
        losers = [s for s in pnl if s["net"] < 0]
        if winners:
            self.log_action("rebalance",
                            f"{len(winners)} winners, {len(losers)} losers")
        return f"Winners: {len(winners)}, Losers: {len(losers)}"

    def _follow_up_clients(self) -> str:
        pipeline = self.crm.get_pipeline_summary()
        # Delegate follow-ups to a sub-agent
        contacted = pipeline.get("contacted", 0)
        negotiating = pipeline.get("negotiating", 0)
        if contacted + negotiating > 0:
            self.log_action("follow_up", f"{contacted} contacted, {negotiating} negotiating")
        return f"Pipeline: {contacted} contacted, {negotiating} negotiating"

    def _do_marketing(self, action: str) -> str:
        """Delegate marketing tasks to sub-agents."""
        self.log_action("marketing", action)
        return f"Marketing action queued: {action}"

    def _process_agent_requests(self) -> dict[str, Any]:
        """Process help requests, handoffs, and alerts from other agents."""
        messages = self.check_messages()
        processed = []

        for msg in messages:
            if msg["msg_type"] == "request":
                # Another agent needs help — delegate to a sub-agent or handle directly
                self.journal("collaborate", f"Processing request from {msg['from_agent']}: {msg['subject']}")
                processed.append({
                    "from": msg["from_agent"],
                    "type": msg["msg_type"],
                    "subject": msg["subject"],
                    "action": "acknowledged",
                })
                self.memory.mark_message_acted_on(msg["id"])

            elif msg["msg_type"] == "handoff":
                # Task being handed off — route to appropriate agent
                self.journal("collaborate", f"Received handoff from {msg['from_agent']}")
                processed.append({
                    "from": msg["from_agent"],
                    "type": "handoff",
                    "subject": msg["subject"],
                    "action": "routed",
                })
                self.memory.mark_message_acted_on(msg["id"])

            elif msg["msg_type"] == "alert":
                # Urgent — log and potentially pause affected strategy
                self.journal("collaborate", f"ALERT from {msg['from_agent']}: {msg['subject']}")
                self.learn(
                    "alert", msg["subject"], msg["body"][:200],
                    severity="high",
                )
                processed.append({
                    "from": msg["from_agent"],
                    "type": "alert",
                    "subject": msg["subject"],
                    "action": "escalated",
                })
                self.memory.mark_message_acted_on(msg["id"])

        return {"processed": len(processed), "details": processed}

    def _reflect_on_cycle(self, cycle_result: dict):
        """Reflect on the cycle and extract lessons."""
        # Check for errors in strategy results
        strategy_results = cycle_result.get("strategy_results", {})
        for name, result in strategy_results.items():
            if result.get("status") == "error":
                self.learn(
                    category="mistake",
                    situation=f"Strategy {name} failed: {result.get('error', 'unknown')}",
                    lesson=f"Strategy {name} encountered an error during execution",
                    rule=f"Monitor {name} closely and check prerequisites before running",
                    severity="high",
                )

        # Share performance insights
        health = cycle_result.get("health", {})
        if health.get("losing_strategies", 0) > health.get("profitable_strategies", 0):
            self.share_knowledge(
                category="warning",
                topic="portfolio_imbalance",
                content=f"More losing ({health.get('losing_strategies')}) than profitable "
                        f"({health.get('profitable_strategies')}) strategies. Need rebalancing.",
                tags=["performance", "risk"],
            )

        # Log knowledge base stats
        kb_stats = self.memory.get_knowledge_summary()
        if kb_stats:
            self.journal("learn", f"Knowledge base: {json.dumps(kb_stats)}")

    def _run_strategies(self) -> dict[str, Any]:
        results = {}
        active = self.db.execute("SELECT name FROM strategies WHERE status = 'active'")
        active_names = {r["name"] for r in active}

        for name, agent in self._strategy_agents.items():
            if name in active_names:
                try:
                    result = agent.run()
                    results[name] = {"status": "ok", "result": result}
                    # Share success insights
                    agent.share_knowledge(
                        category="insight",
                        topic=f"{name}_cycle_result",
                        content=json.dumps(result, default=str)[:500],
                        tags=[name, "strategy_result"],
                    )
                except Exception as e:
                    logger.error(f"Strategy {name} failed: {e}")
                    results[name] = {"status": "error", "error": str(e)}
                    # Auto-learn from failure
                    agent.learn_from_error(e, context=f"Running strategy {name}")
        return results
