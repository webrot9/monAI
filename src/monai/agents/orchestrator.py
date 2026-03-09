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
from monai.agents.browser_learner import BrowserLearner
from monai.agents.collaboration import CollaborationHub
from monai.agents.eng_team import EngineeringTeam
from monai.agents.ethics_test import EthicsTester
from monai.agents.finance_expert import FinanceExpert
from monai.agents.humanizer import Humanizer
from monai.agents.marketing_team import MarketingTeam
from monai.agents.research_team import ResearchTeam
from monai.agents.social_presence import SocialPresence
from monai.agents.web_presence import WebPresence
from monai.agents.identity import IdentityManager
from monai.agents.legal import LegalAdvisorFactory
from monai.agents.phone_provisioner import PhoneProvisioner
from monai.agents.provisioner import Provisioner
from monai.agents.self_improve import SelfImprover
from monai.agents.spawner import AgentSpawner
from monai.business.brand_payments import BrandPayments
from monai.payments.manager import UnifiedPaymentManager
from monai.business.commercialista import Commercialista
from monai.business.crm import CRM
from monai.business.email_marketing import EmailMarketing
from monai.business.finance import Finance
from monai.business.pipeline import Pipeline
from monai.business.risk import RiskManager
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM, get_cost_tracker
from monai.workflows.engine import WorkflowEngine
from monai.workflows.pipelines import get_pipeline, list_pipelines, PIPELINE_REGISTRY
from monai.workflows.router import TaskRouter
from monai.utils.privacy import get_anonymizer
from monai.utils.resources import check_resources
from monai.utils.sandbox import PROJECT_ROOT
from monai.utils.telegram import TelegramBot

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
        self.telegram = TelegramBot(config, db)
        self.ethics_tester = EthicsTester(config, db, llm)
        self.self_improver = SelfImprover(config, db, llm)
        self.legal = LegalAdvisorFactory(config, db, llm)
        self.collab = CollaborationHub(config, db)
        self.eng_team = EngineeringTeam(config, db, llm)
        self.humanizer = Humanizer(config, db, llm)
        self.browser_learner = BrowserLearner(config, db, llm)
        self.phone_provisioner = PhoneProvisioner(config, db, llm)
        self.finance_expert = FinanceExpert(config, db, llm, commercialista=self.commercialista)
        self.research_team = ResearchTeam(config, db, llm)
        self.marketing_team = MarketingTeam(config, db, llm)
        self.social_presence = SocialPresence(config, db, llm)
        self.web_presence = WebPresence(config, db, llm)
        self.pipeline = Pipeline(db)
        self.email_marketing = EmailMarketing(db)
        self.brand_payments = BrandPayments(db)
        self.payment_manager = UnifiedPaymentManager(config, db)
        self.workflow_engine = WorkflowEngine(config, db, llm)
        self.task_router = TaskRouter(config, db, llm)
        # Register utility agents with workflow engine
        self.workflow_engine.register_agent("humanizer", self.humanizer)
        self.workflow_engine.register_agent("finance_expert", self.finance_expert)
        self.workflow_engine.register_agent("research_team", self.research_team)
        self.workflow_engine.register_agent("marketing_team", self.marketing_team)
        self.workflow_engine.register_agent("social_presence", self.social_presence)
        self.workflow_engine.register_agent("web_presence", self.web_presence)
        self._strategy_agents: dict[str, BaseAgent] = {}

    def register_strategy(self, agent: BaseAgent):
        self._strategy_agents[agent.name] = agent
        self.workflow_engine.register_agent(agent.name, agent)
        # Auto-register brand for social presence and web presence
        self.social_presence.register_brand(agent.name)
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

        # Phase -1.5: Telegram — check for creator messages, provision if needed
        cycle_result["telegram"] = self._handle_telegram()

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

        # Phase 0.6: Process collaboration hub (help requests between agents)
        cycle_result["help_requests"] = self._process_help_requests()

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

        # Phase 5.5: Ethics check — test agents before letting them operate
        cycle_result["ethics"] = self._run_ethics_checks()

        # Phase 6: Run registered strategy agents
        cycle_result["strategy_results"] = self._run_strategies()

        # Phase 6.3: Run workflow pipelines (if any are scheduled)
        cycle_result["workflows"] = self._run_scheduled_workflows()

        # Phase 6.4: Process task queue (route unassigned tasks)
        cycle_result["task_routing"] = self._process_task_queue()

        # Phase 6.5: Self-improvement — analyze and improve agents
        cycle_result["self_improvement"] = self._run_self_improvement()

        # Phase 6.6: Finance + Research + Marketing + Social + Web teams
        cycle_result["finance_analysis"] = self._run_finance_expert()
        cycle_result["market_research"] = self._run_market_research()
        cycle_result["marketing"] = self._run_marketing_team()
        cycle_result["social_presence"] = self._run_social_presence()
        cycle_result["web_presence"] = self._run_web_presence()

        # Phase 6.7: Engineering team — self-healing bug fixes
        cycle_result["engineering"] = self._run_engineering_team()

        # Phase 6.8: Browser automation metrics
        cycle_result["browser_metrics"] = self._get_browser_metrics()

        # Phase 6.9: Payment processing — sweep profits to creator
        cycle_result["payments"] = self._run_payment_cycle()

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
        if not self.telegram.has_token:
            needs.append("telegram_bot")

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
            # Legal review for platform registrations
            legal_result = self.legal.assess_activity(
                activity_name=action[:30].lower().replace(" ", "_"),
                activity_type="registration",
                description=action,
            )
            if legal_result["status"] == "blocked":
                return f"BLOCKED by legal advisor: {action}"
            result = self.provisioner.run()
            return json.dumps(result, default=str)[:500]
        elif "marketing" in action.lower() or "outreach" in action.lower():
            return self._do_marketing(action)
        else:
            return f"Action '{action}' queued"

    def _start_new_strategy(self, opportunity: dict) -> str:
        """Automatically start a new strategy from a discovered opportunity.

        Every new strategy gets a Legal Advisor that reviews legality
        BEFORE the strategy is activated.
        """
        name = opportunity.get("name", "").lower().replace(" ", "_")[:30]
        existing = self.db.execute("SELECT id FROM strategies WHERE name = ?", (name,))
        if existing:
            return f"Strategy {name} already exists"

        # LEGAL REVIEW FIRST — every activity gets a legal advisor
        legal_result = self.legal.assess_activity(
            activity_name=name,
            activity_type="strategy",
            description=opportunity.get("description", name),
            requesting_agent="orchestrator",
        )

        if legal_result["status"] == "blocked":
            self.log_action("strategy_blocked_legal", name,
                            json.dumps(legal_result, default=str)[:500])
            return f"Strategy {name} BLOCKED by legal advisor: {legal_result.get('blockers_count', 0)} blockers"

        # Legal review passed or needs_review — proceed with caution
        self.db.execute_insert(
            "INSERT INTO strategies (name, category, description, allocated_budget) VALUES (?, ?, ?, ?)",
            (name, opportunity.get("category", "misc"),
             opportunity.get("description", ""),
             min(opportunity.get("startup_cost", 10), self.config.risk.max_monthly_spend_new_strategy)),
        )
        self.log_action("start_strategy", name,
                        json.dumps({**opportunity, "legal_status": legal_result["status"]}, default=str)[:500])
        return f"Started new strategy: {name} (legal: {legal_result['status']})"

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
        """Delegate marketing tasks to the marketing team."""
        try:
            result = self.marketing_team.run(target_strategy=action)
            self.log_action("marketing", action, json.dumps(result, default=str)[:500])
            return f"Marketing executed: {result.get('campaigns_planned', 0)} campaigns"
        except Exception as e:
            logger.error(f"Marketing execution failed: {e}")
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

    def _process_help_requests(self) -> dict[str, Any]:
        """Process open help requests from the collaboration hub."""
        open_requests = self.collab.get_open_requests()
        processed = []

        for req in open_requests:
            skill = req["skill_needed"]

            # Legal requests → auto-spawn legal advisor
            if skill == "legal":
                self.collab.claim_request(req["id"], "legal_advisor")
                self.collab.start_work(req["id"])

                context = json.loads(req.get("context", "{}"))
                legal_result = self.legal.assess_activity(
                    activity_name=context.get("activity_name", req["task_description"][:30]),
                    activity_type=context.get("activity_type", "strategy"),
                    description=req["task_description"],
                    requesting_agent=req["requesting_agent"],
                )

                self.collab.complete_request(req["id"], json.dumps(legal_result, default=str))
                processed.append({
                    "id": req["id"],
                    "skill": skill,
                    "handler": "legal_advisor",
                    "status": legal_result["status"],
                })

            else:
                # Route to appropriate sub-agent or queue for next cycle
                subagent_tasks = [{
                    "name": f"help_{skill}_{req['id']}",
                    "task": req["task_description"],
                }]
                self.collab.claim_request(req["id"], f"subagent_{skill}")
                self.collab.start_work(req["id"])

                try:
                    loop = asyncio.new_event_loop()
                    try:
                        results = loop.run_until_complete(
                            self.spawner.run_parallel(subagent_tasks, max_steps=20)
                        )
                    finally:
                        loop.close()

                    task_name = subagent_tasks[0]["name"]
                    result = results.get(task_name, {})
                    if result.get("status") == "completed":
                        self.collab.complete_request(
                            req["id"], json.dumps(result, default=str)[:2000]
                        )
                    else:
                        self.collab.fail_request(
                            req["id"], result.get("error", "Task did not complete")
                        )
                except Exception as e:
                    self.collab.fail_request(req["id"], str(e))

                processed.append({
                    "id": req["id"],
                    "skill": skill,
                    "handler": f"subagent_{skill}",
                })

        return {"processed": len(processed), "details": processed}

    def _handle_telegram(self) -> dict[str, Any]:
        """Process Telegram updates and handle creator communication."""
        if not self.config.telegram.enabled:
            return {"status": "disabled"}

        if not self.telegram.has_token:
            return {"status": "not_provisioned", "action": "needs_telegram_bot"}

        try:
            # Generate verification if not yet done
            if not self.telegram._verification_token:
                self.telegram.generate_verification()

            # Process any pending updates from creator
            updates = self.telegram.process_updates()

            result: dict[str, Any] = {"status": "ok", "updates": len(updates)}

            for update in updates:
                if update["type"] == "status_request":
                    # Creator asked for status — send report
                    budget = self.commercialista.get_budget()
                    health = self.risk.get_portfolio_health()
                    self.telegram.send_report("Status Report", {
                        "Budget": f"€{budget['balance']:.2f} remaining",
                        "Strategies": f"{health.get('active_strategies', 0)} active",
                        "Net Profit": f"€{self.finance.get_net_profit():.2f}",
                    })
                elif update["type"] == "report_request":
                    report = self.commercialista.get_full_report()
                    self.telegram.send_report("Full Report", {
                        "Budget": json.dumps(report.get("budget", {}), default=str),
                        "Costs by Agent": json.dumps(report.get("costs_by_agent", []), default=str),
                        "Recommendation": report.get("recommendation", "N/A"),
                    })

            return result

        except Exception as e:
            logger.error(f"Telegram handling failed: {e}")
            return {"status": "error", "error": str(e)}

    def ask_creator(self, question: str, timeout: int = 3600) -> str | None:
        """Ask the creator a question via Telegram. Returns their response or None."""
        if not self.telegram.is_configured:
            logger.warning("Cannot ask creator — Telegram not configured")
            return None
        return self.telegram.ask_creator(question, timeout=timeout)

    def notify_creator(self, message: str) -> bool:
        """Send a notification to the creator via Telegram."""
        if not self.telegram.is_configured:
            return False
        return self.telegram.notify_creator(message)

    def _run_ethics_checks(self) -> dict[str, Any]:
        """Run ethics tests on all registered agents before they operate."""
        results = {}

        for name in self._strategy_agents:
            # Skip if recently tested (within last 5 cycles)
            summary = self.ethics_tester.get_agent_ethics_summary(name)
            if summary.get("last_tested") and not summary.get("never_tested"):
                # Only retest every 5 cycles unless there were failures
                if summary.get("total_failures", 0) == 0 and self._cycle % 5 != 0:
                    results[name] = {"status": "skipped", "reason": "recently_passed"}
                    continue

            if self.ethics_tester.is_quarantined(name):
                results[name] = {"status": "quarantined"}
                self.log_action("QUARANTINED_AGENT", name)
                # Notify creator about quarantined agent
                self.notify_creator(
                    f"Agent `{name}` has been QUARANTINED due to repeated ethics failures. "
                    "Manual review required."
                )
                continue

            # Run ethics test
            test_result = self.ethics_tester.test_agent(name)
            results[name] = {
                "score": test_result["score"],
                "passed": test_result["all_passed"],
                "enforcement_level": test_result["enforcement_level"],
            }

            if not test_result["all_passed"]:
                failed_tests = [r["test"] for r in test_result["results"] if not r["passed"]]
                self.log_action("ETHICS_FAILURE", name, json.dumps(failed_tests))
                self.learn(
                    "ethics", f"Agent {name} failed ethics tests",
                    f"Failed: {failed_tests}. Enforcement escalated.",
                    rule=f"Monitor {name} closely — ethics violations detected",
                    severity="critical",
                )

        return results

    def _run_self_improvement(self) -> dict[str, Any]:
        """Analyze agent performance and apply improvements."""
        # Only run every 3 cycles to save API costs
        if self._cycle % 3 != 0:
            return {"status": "skipped", "reason": "not_improvement_cycle"}

        results = {}
        for name, agent in self._strategy_agents.items():
            # Record basic metrics
            self.self_improver.record_metric(name, self._cycle, "cycle_reached", self._cycle)

            # Generate improvements if enough data
            analysis = self.self_improver.analyze_performance(name)
            if analysis["data_richness"] == "good":
                improvements = self.self_improver.generate_improvements(name)
                results[name] = {
                    "analysis": "complete",
                    "improvements_proposed": len(improvements),
                }
            else:
                results[name] = {"analysis": "sparse_data"}

        return results

    def _run_engineering_team(self) -> dict[str, Any]:
        """Run the engineering team to fix bugs and improve the system."""
        # Only run every 2 cycles to save API costs
        if self._cycle % 2 != 0:
            return {"status": "skipped", "reason": "not_engineering_cycle"}

        try:
            result = self.eng_team.run()
            self.log_action("engineering_team", json.dumps(result, default=str)[:500])
            return result
        except Exception as e:
            logger.error(f"Engineering team failed: {e}")
            return {"status": "error", "error": str(e)}

    def _get_browser_metrics(self) -> dict[str, Any]:
        """Get browser automation success metrics."""
        try:
            success_rates = self.browser_learner.get_success_rate()
            failures = self.browser_learner.get_failure_breakdown()
            return {
                "success_rates": success_rates,
                "failure_breakdown": failures,
            }
        except Exception:
            return {"status": "no_data"}

    def humanize_content(self, content: str, style: str = "default",
                         context: str = "") -> str:
        """Humanize content before sending to clients. Available to all agents."""
        return self.humanizer.humanize(content, style, context)

    def get_phone_number(self, platform: str, requesting_agent: str) -> dict[str, Any]:
        """Get a virtual phone number for platform signup. Available to all agents."""
        return self.phone_provisioner.get_number(platform, requesting_agent)

    def run_pipeline(self, pipeline_name: str, context: dict | None = None) -> dict[str, Any]:
        """Run a named pipeline. Available to all agents and the orchestrator."""
        pipeline = get_pipeline(pipeline_name)
        if not pipeline:
            return {"status": "error", "reason": f"Pipeline '{pipeline_name}' not found"}
        return self.workflow_engine.execute(pipeline, context)

    def route_task(self, task: str, task_type: str = "",
                   priority: int = 5) -> dict[str, Any]:
        """Route a task to the best agent. Available to all agents."""
        return self.task_router.route(task, task_type, priority)

    def _run_scheduled_workflows(self) -> dict[str, Any]:
        """Run any scheduled workflow pipelines."""
        # Every 5 cycles, run the revenue diversification pipeline
        if self._cycle % 5 == 0:
            try:
                pipeline = get_pipeline("revenue_diversification")
                if pipeline:
                    result = self.workflow_engine.execute(pipeline)
                    self.log_action("workflow_pipeline", "revenue_diversification",
                                    json.dumps(result, default=str)[:500])
                    return {"revenue_diversification": result}
            except Exception as e:
                logger.error(f"Revenue diversification pipeline failed: {e}")
                return {"error": str(e)}
        return {"status": "skipped", "reason": "not_workflow_cycle"}

    def _process_task_queue(self) -> dict[str, Any]:
        """Process any unrouted tasks in the queue."""
        queued = self.task_router.get_queue("queued")
        routed = 0
        for task in queued[:10]:  # Process up to 10 per cycle
            result = self.task_router.route(
                task["task_description"],
                task.get("task_type", ""),
                task.get("priority", 5),
            )
            if result.get("routed_to"):
                routed += 1
        return {"queued": len(queued), "routed": routed}

    def _run_finance_expert(self) -> dict[str, Any]:
        """Run finance expert analysis — every 2 cycles."""
        if self._cycle % 2 != 0:
            return {"status": "skipped", "reason": "not_finance_cycle"}
        try:
            result = self.finance_expert.run()
            self.log_action("finance_expert", json.dumps(result, default=str)[:500])
            return result
        except Exception as e:
            logger.error(f"Finance expert failed: {e}")
            return {"status": "error", "error": str(e)}

    def _run_market_research(self) -> dict[str, Any]:
        """Run market research — every 4 cycles."""
        if self._cycle % 4 != 0:
            return {"status": "skipped", "reason": "not_research_cycle"}
        try:
            result = self.research_team.run()
            self.log_action("market_research", json.dumps(result, default=str)[:500])
            return result
        except Exception as e:
            logger.error(f"Market research failed: {e}")
            return {"status": "error", "error": str(e)}

    def _run_marketing_team(self) -> dict[str, Any]:
        """Run marketing campaigns — every 3 cycles."""
        if self._cycle % 3 != 0:
            return {"status": "skipped", "reason": "not_marketing_cycle"}
        try:
            result = self.marketing_team.run()
            self.log_action("marketing_team", json.dumps(result, default=str)[:500])
            return result
        except Exception as e:
            logger.error(f"Marketing team failed: {e}")
            return {"status": "error", "error": str(e)}

    def _run_social_presence(self) -> dict[str, Any]:
        """Run social media presence — every 2 cycles."""
        if self._cycle % 2 != 0:
            return {"status": "skipped", "reason": "not_social_cycle"}
        try:
            result = self.social_presence.run()
            self.log_action("social_presence", json.dumps(result, default=str)[:500])
            return result
        except Exception as e:
            logger.error(f"Social presence failed: {e}")
            return {"status": "error", "error": str(e)}

    def _run_web_presence(self) -> dict[str, Any]:
        """Run web presence management — every 3 cycles."""
        if self._cycle % 3 != 0:
            return {"status": "skipped", "reason": "not_web_cycle"}
        try:
            result = self.web_presence.run()
            self.log_action("web_presence", json.dumps(result, default=str)[:500])
            return result
        except Exception as e:
            logger.error(f"Web presence failed: {e}")
            return {"status": "error", "error": str(e)}

    def request_research(self, topic: str) -> dict[str, Any]:
        """Request on-demand market research. Available to all agents."""
        return self.research_team.research_specific(topic)

    def launch_marketing_campaign(self, strategy_name: str,
                                  product_description: str,
                                  budget: float = 0) -> dict[str, Any]:
        """Launch a marketing campaign. Available to all agents."""
        return self.marketing_team.launch_campaign(strategy_name, product_description, budget)

    def get_investment_advice(self) -> list[dict[str, Any]]:
        """Get current investment recommendations from finance expert."""
        return self.finance_expert.get_latest_recommendations()

    def _run_payment_cycle(self) -> dict[str, Any]:
        """Run payment processing — check balances and sweep profits to creator.

        Runs every 3 cycles to avoid excessive RPC calls.
        Sweeps profits from brand crypto wallets to creator's Monero address.
        """
        if self._cycle % 3 != 0:
            return {"status": "skipped", "reason": "not_payment_cycle"}

        if not self.config.creator_wallet.xmr_address:
            return {"status": "skipped", "reason": "no_creator_wallet_configured"}

        try:
            loop = asyncio.new_event_loop()
            try:
                # Run sweep cycle
                sweep_result = loop.run_until_complete(
                    self.payment_manager.run_sweep_cycle()
                )

                # Get payment system health
                health = loop.run_until_complete(
                    self.payment_manager.health_check()
                )
            finally:
                loop.close()

            result = {
                "sweep": sweep_result,
                "health": health,
                "status": self.payment_manager.get_status(),
            }

            if sweep_result.get("sweeps_successful", 0) > 0:
                xmr_swept = sweep_result.get("total_xmr_swept", 0)
                self.log_action("payment_sweep",
                                f"Swept {xmr_swept:.8f} XMR to creator")
                self.notify_creator(
                    f"Profit sweep: {xmr_swept:.8f} XMR transferred to your wallet."
                )

            return result

        except Exception as e:
            logger.error(f"Payment cycle failed: {e}")
            return {"status": "error", "error": str(e)}

    def _run_strategies(self) -> dict[str, Any]:
        results = {}
        active = self.db.execute("SELECT name FROM strategies WHERE status = 'active'")
        active_names = {r["name"] for r in active}

        for name, agent in self._strategy_agents.items():
            if name in active_names:
                # Skip quarantined agents
                if self.ethics_tester.is_quarantined(name):
                    results[name] = {"status": "quarantined", "reason": "ethics_failure"}
                    continue
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
