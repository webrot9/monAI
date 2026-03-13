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
from monai.agents.fact_checker import FactChecker
from monai.agents.finance_expert import FinanceExpert
from monai.agents.humanizer import Humanizer
from monai.agents.marketing_team import MarketingTeam
from monai.agents.research_team import ResearchTeam
from monai.agents.social_presence import SocialPresence
from monai.agents.web_presence import WebPresence
from monai.agents.identity import IdentityManager
from monai.agents.legal import LegalAdvisorFactory
from monai.agents.api_provisioner import APIProvisioner
from monai.agents.llc_provisioner import LLCProvisioner
from monai.agents.phone_provisioner import PhoneProvisioner
from monai.agents.provisioner import Provisioner
from monai.agents.product_iterator import ProductIterator
from monai.agents.self_improve import SelfImprover
from monai.agents.spawner import AgentSpawner
from monai.business.brand_payments import BrandPayments
from monai.business.corporate import CorporateManager
from monai.payments.manager import UnifiedPaymentManager
from monai.business.commercialista import Commercialista
from monai.business.bootstrap import BootstrapWallet
from monai.business.crm import CRM
from monai.business.email_marketing import EmailMarketing
from monai.business.finance import Finance, GeneralLedger
from monai.business.kofi import KofiCampaignManager
from monai.business.pipeline import Pipeline
from monai.business.exchange_rates import ExchangeRateService
from monai.business.reconciliation import ReconciliationEngine
from monai.business.reporting import FinancialReporter
from monai.business.risk import RiskManager
from monai.business.strategy_lifecycle import StrategyLifecycle
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM, BudgetExceededError, get_cost_tracker
from monai.workflows.engine import WorkflowEngine
from monai.workflows.pipelines import get_pipeline, list_pipelines, PIPELINE_REGISTRY
from monai.workflows.router import TaskRouter
from monai.utils.privacy import get_anonymizer
from monai.utils.resources import check_resources
from monai.utils.sandbox import PROJECT_ROOT
from monai.business.alerting import AlertingEngine
from monai.business.audit import AuditTrail
from monai.business.backup import BackupManager
from monai.business.spending_guard import SpendingGuard
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
        self.ledger = GeneralLedger(db)
        self.risk = RiskManager(config, db)
        self.identity = IdentityManager(config, db, llm)
        self.provisioner = Provisioner(config, db, llm)
        self.spawner = AgentSpawner(config, db, llm)
        self.commercialista = Commercialista(config, db)
        self.telegram = TelegramBot(config, db)
        self.ethics_tester = EthicsTester(config, db, llm)
        self.self_improver = SelfImprover(config, db, llm, memory=self.memory)
        self.product_iterator = ProductIterator(config, db, llm)
        self.legal = LegalAdvisorFactory(config, db, llm)
        self.collab = CollaborationHub(config, db)
        self.eng_team = EngineeringTeam(config, db, llm)
        self.humanizer = Humanizer(config, db, llm)
        self.fact_checker = FactChecker(config, db, llm)
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
        self.payment_manager = UnifiedPaymentManager(config, db, ledger=self.ledger)
        self.spending_guard = SpendingGuard(db, config)
        self.corporate = CorporateManager(db)
        self.bootstrap_wallet = BootstrapWallet(config, db, ledger=self.ledger)
        self.kofi_manager = KofiCampaignManager(
            config, db, llm, bootstrap_wallet=self.bootstrap_wallet,
        )
        self.exchange_rates = ExchangeRateService(
            db, anonymizer=get_anonymizer(config),
        )
        self.reporter = FinancialReporter(
            db, self.ledger, self.finance, bootstrap=self.bootstrap_wallet,
        )
        self.reconciliation = ReconciliationEngine(db)
        self.llc_provisioner = LLCProvisioner(config, db, llm)
        self.api_provisioner = APIProvisioner(
            config, db, llm,
            payment_manager=self.payment_manager,
        )
        self.strategy_lifecycle = StrategyLifecycle(db)
        self.audit = AuditTrail(db)
        self.alerting = AlertingEngine(db)
        self.backup_manager = BackupManager(
            db, config.data_dir / "backups",
            max_backups=config.backup.max_backups,
        )
        self._ensure_llc_setup()
        # Load persisted cost tracker state
        cost_state_path = str(config.data_dir / "cost_tracker.json")
        get_cost_tracker().load_state(cost_state_path)
        self.workflow_engine = WorkflowEngine(config, db, llm)
        self.task_router = TaskRouter(config, db, llm)
        # Register utility agents with workflow engine
        self.workflow_engine.register_agent("humanizer", self.humanizer)
        self.workflow_engine.register_agent("fact_checker", self.fact_checker)
        self.workflow_engine.register_agent("finance_expert", self.finance_expert)
        self.workflow_engine.register_agent("research_team", self.research_team)
        self.workflow_engine.register_agent("marketing_team", self.marketing_team)
        self.workflow_engine.register_agent("social_presence", self.social_presence)
        self.workflow_engine.register_agent("web_presence", self.web_presence)
        self.workflow_engine.register_agent("api_provisioner", self.api_provisioner)
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
        self.audit.log("orchestrator", "system", "cycle_start",
                       details={"cycle": self._cycle})
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
                self.audit.log("orchestrator", "system", "anonymity_check_failed",
                               details=anon_status, success=False, risk_level="critical")
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
            self.audit.log("orchestrator", "system", "resource_limit_exceeded",
                           details=resources, success=False, risk_level="critical")
            return {"status": "aborted", "reason": "resource_limits_exceeded", **cycle_result}

        # Phase -0.5: Budget check — can we afford to operate?
        budget = self.commercialista.get_budget()
        cycle_result["budget"] = budget
        if budget["balance"] <= 0:
            self.log_action("BUDGET_EXHAUSTED", json.dumps(budget))
            self.audit.log("orchestrator", "system", "budget_exhausted",
                           details=budget, success=False, risk_level="high")
            self.learn("alert", "Budget exhausted",
                       f"Balance: €{budget['balance']:.2f}. Cannot spend more.",
                       rule="Focus only on zero-cost revenue activities", severity="critical")
            # Don't abort entirely — still allow zero-cost operations
        self.log_action("budget_check",
                        f"€{budget['balance']:.2f} remaining, "
                        f"burn: €{budget['burn_rate_daily']:.4f}/day")

        # Set per-cycle budget limits based on remaining balance
        tracker = get_cost_tracker()
        tracker.reset_cycle()
        max_cycle_cost = min(
            self.config.budget.max_cycle_cost,
            budget["balance"] * self.config.budget.budget_fraction_per_cycle,
        )
        tracker.set_cycle_limits(
            max_cost=max_cycle_cost,
            max_calls=self.config.budget.max_cycle_calls,
        )
        self.log_action("cycle_budget",
                        f"Cycle limits: €{max_cycle_cost:.2f} cost, "
                        f"{self.config.budget.max_cycle_calls} calls")

        try:
            return self._execute_cycle(cycle_result, budget)
        except BudgetExceededError as exc:
            self.log_action("CYCLE_BUDGET_EXCEEDED", str(exc))
            self.audit.log("orchestrator", "system", "cycle_budget_exceeded",
                           details={"error": str(exc), "cycle": self._cycle},
                           success=False, risk_level="high")
            self.learn(
                "alert", "Cycle budget exceeded",
                str(exc),
                rule="Reduce LLM calls per cycle — use cheaper models or fewer calls",
                severity="warning",
            )
            cycle_result["status"] = "budget_exceeded"
            cycle_result["budget_error"] = str(exc)
            # Still run the commercialista report so we have financial data
            cycle_result["timestamp"] = datetime.now().isoformat()
            api_costs = get_cost_tracker().get_summary()
            cycle_result["api_costs_session"] = api_costs
            cycle_result["budget_after"] = self.commercialista.get_budget()
            self.journal("execute", f"Cycle {self._cycle} ended early (budget exceeded)",
                         {"error": str(exc)}, outcome="budget_exceeded")
            return cycle_result

    def _execute_cycle(self, cycle_result: dict, budget: dict) -> dict[str, Any]:
        """Execute the main cycle phases. Separated to allow BudgetExceededError handling."""
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
            try:
                subagent_results = self._run_async(
                    self.spawner.run_parallel(subagent_tasks)
                )
                cycle_result["subagent_results"] = {
                    k: {"status": v.get("status")} for k, v in subagent_results.items()
                }
            except Exception as e:
                logger.error(f"Sub-agent execution failed: {e}")
                cycle_result["subagent_results"] = {"error": str(e)}

        # Audit direct actions
        for ar in action_results:
            self.audit.log("orchestrator", "system", "execute_action",
                           details={"action": ar["action"]},
                           result=str(ar.get("result", ""))[:200])

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

        # Phase 6.55: Evaluate running A/B experiments
        try:
            experiment_results = self.self_improver.tick_experiments()
            if experiment_results:
                cycle_result["experiments"] = experiment_results
                self.log_action(
                    "experiments_evaluated",
                    json.dumps(experiment_results, default=str)[:500],
                )
        except Exception as e:
            logger.warning(f"Experiment evaluation failed: {e}")

        # Phase 6.58: Product iteration — improve underperforming products
        cycle_result["product_iteration"] = self._run_product_iteration()

        # Phase 6.6: Finance + Research + Marketing + Social + Web teams (parallelized)
        team_results = self._run_teams_parallel()
        cycle_result["finance_analysis"] = team_results.get("finance", {})
        cycle_result["market_research"] = team_results.get("research", {})
        cycle_result["marketing"] = team_results.get("marketing", {})
        cycle_result["social_presence"] = team_results.get("social", {})
        cycle_result["web_presence"] = team_results.get("web", {})

        # Phase 6.7: Engineering team — self-healing bug fixes
        cycle_result["engineering"] = self._run_engineering_team()

        # Phase 6.8: Browser automation metrics
        cycle_result["browser_metrics"] = self._get_browser_metrics()

        # Phase 6.85: Refresh exchange rates (every 6 cycles ≈ hourly)
        if self._cycle % 6 == 0:
            try:
                import asyncio
                fetched = asyncio.get_event_loop().run_until_complete(
                    self.exchange_rates.fetch_live_rates()
                )
                cycle_result["exchange_rates_refreshed"] = len(fetched)
            except Exception as e:
                logger.warning(f"Exchange rate refresh failed: {e}")

        # Phase 6.85b: Mid-cycle anonymity re-verification
        if self.config.privacy.verify_anonymity and self.config.privacy.proxy_type != "none":
            anon_recheck = get_anonymizer(self.config).startup_check()
            cycle_result["anonymity_recheck"] = anon_recheck
            if not anon_recheck.get("anonymous"):
                logger.critical(
                    f"MID-CYCLE ANONYMITY LOST: {anon_recheck}. "
                    "Aborting remaining phases to protect creator."
                )
                self.audit.log("orchestrator", "system", "anonymity_lost_mid_cycle",
                               details=anon_recheck, success=False, risk_level="critical")
                cycle_result["status"] = "partial"
                cycle_result["reason"] = "anonymity_lost_mid_cycle"
                return cycle_result

        # Phase 6.9: Payment processing — sweep profits to creator
        cycle_result["payments"] = self._run_payment_cycle()

        # Phase 6.95: Ledger integrity check
        try:
            integrity = self.ledger.verify_integrity()
            cycle_result["ledger_integrity"] = integrity
            if not integrity["balanced"]:
                logger.critical(
                    f"LEDGER IMBALANCE: {len(integrity['unbalanced_entries'])} "
                    f"unbalanced entries detected"
                )
                self.audit.log("orchestrator", "system", "ledger_imbalance",
                               details=integrity, success=False, risk_level="critical")
        except Exception as e:
            logger.error(f"Ledger integrity check failed: {e}")
            cycle_result["ledger_integrity"] = {"error": str(e)}

        # Phase 6.97: Strategy performance evaluation + auto-actions
        try:
            perf = self.reporter.get_strategy_performance()
            cycle_result["strategy_performance"] = {
                "total_revenue": perf["total_revenue"],
                "total_expenses": perf["total_expenses"],
                "overall_roi_pct": perf["overall_roi_pct"],
                "to_review": len(perf["strategies_to_review"]),
                "to_pause": len(perf["strategies_to_pause"]),
                "to_scale": len(perf["strategies_to_scale"]),
            }

            paused = []
            for s in perf["strategies_to_pause"]:
                sid = s["id"]
                if self.strategy_lifecycle.can_transition(sid, "paused"):
                    self.strategy_lifecycle.pause(
                        sid,
                        reason=f"Auto-pause: net={s['net']:.2f}, "
                               f"ROI={s['roi_pct']}%, budget_used={s['budget_used_pct']}%",
                    )
                    paused.append(s["name"])
                    self.log_action("strategy_auto_pause",
                                    f"Paused '{s['name']}': net=€{s['net']:.2f}, "
                                    f"ROI={s['roi_pct']}%")
                    self.audit.log(
                        "orchestrator", "system", "strategy_auto_pause",
                        details={"strategy": s["name"], "net": s["net"],
                                 "roi_pct": s["roi_pct"]},
                        brand=s.get("name", ""), risk_level="medium",
                    )

            if paused:
                # Run post-mortem analysis on each paused strategy
                for s in perf["strategies_to_pause"]:
                    if s["name"] in paused:
                        self._run_failure_postmortem(s)

                self.notify_creator(
                    f"*Auto-paused {len(paused)} underperforming "
                    f"{'strategy' if len(paused) == 1 else 'strategies'}:*\n"
                    + "\n".join(f"- {name}" for name in paused)
                    + "\n\nUse /resume <name> to re-activate."
                )

            for s in perf["strategies_to_review"]:
                self.log_action("strategy_review",
                                f"Strategy '{s['name']}' needs review: "
                                f"net=€{s['net']:.2f}, 7d_net=€{s['trend_7d']['net']:.2f}")

            # Auto-scale: boost budget for growing strategies
            scaled = self._auto_scale_strategies(perf["strategies_to_scale"])
            cycle_result["strategy_performance"]["scaled"] = len(scaled)
        except Exception as e:
            logger.error(f"Strategy performance eval failed: {e}")

        # Phase 6.99: Automatic reinvestment — scale winners, cut losers
        try:
            reinvest_result = self.commercialista.compute_reinvestment()
            cycle_result["reinvestment"] = reinvest_result

            if reinvest_result.get("reinvest", 0) > 0:
                # Get strategy performance for allocation
                strat_perf = []
                for s in self.finance.get_strategy_pnl():
                    rev = s.get("revenue", 0)
                    exp = s.get("expenses", 0)
                    roi = rev / exp if exp > 0 else 0
                    strat_perf.append({
                        "name": s["name"],
                        "revenue": rev,
                        "expenses": exp,
                        "roi": roi,
                    })

                allocations = self.commercialista.allocate_to_strategies(
                    reinvest_result["reinvest"], strat_perf,
                )

                # Apply allocations: update strategy budgets in DB
                for alloc in allocations:
                    if alloc.get("amount", 0) > 0:
                        self.db.execute(
                            "UPDATE strategies SET allocated_budget = allocated_budget + ? "
                            "WHERE name = ?",
                            (alloc["amount"], alloc["strategy"]),
                        )
                    elif alloc.get("action") == "reduce":
                        self.db.execute(
                            "UPDATE strategies SET allocated_budget = MAX(0, allocated_budget * 0.5) "
                            "WHERE name = ?",
                            (alloc["strategy"],),
                        )

                self.commercialista.record_reinvestment(
                    reinvest=reinvest_result["reinvest"],
                    reserve=reinvest_result["reserve"],
                    creator=reinvest_result["creator_sweep"],
                    allocations=allocations,
                )

                logger.info(
                    f"Reinvestment: €{reinvest_result['reinvest']:.2f} allocated to "
                    f"{len([a for a in allocations if a.get('amount', 0) > 0])} strategies, "
                    f"€{reinvest_result['reserve']:.2f} reserved, "
                    f"€{reinvest_result['creator_sweep']:.2f} for creator sweep"
                )
        except Exception as e:
            logger.error(f"Reinvestment cycle failed: {e}")

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

        # Phase 7.5: Automated financial reports
        self._send_financial_reports()

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
        self.audit.log("orchestrator", "system", "cycle_complete",
                       details={"cycle": self._cycle,
                                "net_profit": cycle_result["net_profit"]})

        # Persist cost tracker state for session continuity
        get_cost_tracker().save_state(str(self.config.data_dir / "cost_tracker.json"))

        # Phase 9.5: Automated backups (DB every cycle, config every 7 cycles)
        self._run_scheduled_backups()

        # Phase 10: Auto-alerts to creator via Telegram
        self._send_auto_alerts(cycle_result, updated_budget)

        return cycle_result

    def _send_auto_alerts(self, cycle_result: dict, budget: dict):
        """Send automatic Telegram alerts for critical events.

        Uses the AlertingEngine for configurable threshold-based alerts,
        plus hardcoded milestone alerts that don't fit a threshold model.
        """
        if not self.config.telegram.enabled or not self.config.telegram.bot_token:
            return

        alerts = []

        # Evaluate configurable alert rules
        try:
            today = self.finance.get_daily_summary()
            dashboard_data = {"budget": budget, "today": today}
            fired = self.alerting.evaluate(dashboard_data)
            severity_icons = {
                "critical": "🚨", "warning": "⚠️", "info": "💡",
            }
            for alert in fired:
                icon = severity_icons.get(alert["severity"], "📌")
                alerts.append(f"{icon} {alert['message']}")
        except Exception as e:
            logger.debug(f"Alerting engine error: {e}")

        # Hardcoded milestone alerts (not threshold-based)
        if budget.get("self_sustaining") and self._cycle <= 5:
            alerts.append("🎉 MILESTONE: monAI is self-sustaining! Revenue >= Expenses")

        reviews = cycle_result.get("reviews", {})
        if isinstance(reviews, dict):
            for strat_name, review in reviews.items():
                if isinstance(review, dict) and review.get("paused"):
                    alerts.append(f"⏸ Strategy paused: {strat_name} "
                                  f"({review.get('reason', 'low performance')})")

        # Send condensed report
        if alerts:
            msg = "📊 monAI Cycle Report\n\n" + "\n".join(alerts)
            msg += f"\n\n💶 Balance: €{budget.get('balance', 0):.2f}"
            try:
                self.telegram.send_message(msg)
            except Exception as e:
                logger.debug(f"Telegram alert failed: {e}")

    def _run_scheduled_backups(self) -> dict[str, Any]:
        """Run automated backups on schedule (intervals from config.backup)."""
        bcfg = self.config.backup
        if not bcfg.enabled:
            return {"status": "disabled"}

        results: dict[str, Any] = {}
        try:
            # Database backup on configured interval
            if self._cycle % bcfg.db_interval_cycles == 0:
                db_result = self.backup_manager.backup_database()
                results["database"] = {
                    "path": db_result["path"],
                    "size_bytes": db_result["size_bytes"],
                    "verified": db_result["verified"],
                }
                self.audit.log("orchestrator", "system", "backup_database",
                               details=results["database"])

            # Config backup on configured interval
            if self._cycle % bcfg.config_interval_cycles == 0:
                config_path = self.config.data_dir / "config.json"
                if config_path.exists():
                    cfg_result = self.backup_manager.backup_config(config_path)
                    results["config"] = cfg_result
                    self.audit.log("orchestrator", "config", "backup_config",
                                   details={"path": cfg_result.get("path", "")})

        except Exception as e:
            logger.error(f"Scheduled backup failed: {e}")
            results["error"] = str(e)
            self.audit.log("orchestrator", "system", "backup_failed",
                           details={"error": str(e)}, success=False,
                           risk_level="high")

        return results

    def _ensure_llc_setup(self) -> None:
        """Auto-provision LLC entity and contractor from config if not in DB."""
        if not self.config.llc.enabled:
            return

        entity = self.corporate.get_primary_entity()
        if not entity:
            entity_id = self.corporate.create_entity(
                name=self.config.llc.entity_name or "Holdings LLC",
                entity_type=self.config.llc.entity_type,
                jurisdiction=self.config.llc.jurisdiction,
            )
            entity = self.corporate.get_entity(entity_id)

        if entity and not self.corporate.get_active_contractor(entity["id"]):
            if self.config.llc.contractor_alias:
                self.corporate.create_contractor(
                    alias=self.config.llc.contractor_alias,
                    entity_id=entity["id"],
                    service_description=self.config.llc.contractor_service,
                    rate_type=self.config.llc.contractor_rate_type,
                    rate_amount=self.config.llc.contractor_rate_amount,
                    rate_percentage=self.config.llc.contractor_rate_percentage,
                    payment_method=self.config.llc.contractor_payment_method,
                )

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

        result: dict[str, Any] = {}

        if needs:
            self.log_action("provisioning", f"Need to set up: {needs}")
            result = self.provisioner.run()

        # Bootstrap funding phase check
        bootstrap_phase = self.bootstrap_wallet.get_funding_phase()
        result["bootstrap_phase"] = bootstrap_phase

        if bootstrap_phase == "pre_bootstrap":
            self.log_action("bootstrap",
                            "No funding source configured. Starting Ko-fi campaign.")
            # Auto-setup Ko-fi campaign as primary funding source
            kofi_result = self._setup_kofi_campaign()
            result["bootstrap"] = kofi_result
        else:
            # Campaign exists — sync donations periodically
            if self._cycle % 3 == 0:
                result["kofi_sync"] = self._sync_kofi_donations()

        # LLC provisioning — runs if enabled but not yet complete
        llc_status = self.llc_provisioner.get_provision_status()
        if (self.config.llc.enabled
                and llc_status.get("status") != "not_configured"
                and llc_status.get("progress_pct", 0) < 100):
            self.log_action("llc_provisioning",
                            f"LLC at {llc_status.get('progress_pct', 0)}% — "
                            f"current step: {llc_status.get('current_step', 'unknown')}")
            llc_result = self.llc_provisioner.run()
            result["llc"] = llc_result

            # Notify creator of progress
            if llc_result.get("status") == "completed":
                self.notify_creator(
                    f"LLC '{self.config.llc.entity_name}' fully provisioned! "
                    f"Entity formed, EIN obtained, bank opened, Stripe connected."
                )
            else:
                completed = sum(
                    1 for s in llc_result.get("steps", {}).values()
                    if s.get("status") == "completed"
                )
                self.notify_creator(
                    f"LLC provisioning in progress: {completed}/8 steps done. "
                    f"Will continue next cycle."
                )

        # API key provisioning — ensure brands have payment provider keys.
        # Force on first cycle so payment providers are set up at startup.
        api_prov_result = self._run_api_provisioning(force=(self._cycle <= 1))
        if api_prov_result.get("provisioned"):
            result["api_keys"] = api_prov_result

        # Proactively provision payment providers for active strategies.
        # Each strategy needs at least one payment provider to sell products.
        self._ensure_strategy_payment_providers(result)

        if not needs and "llc" not in result and "api_keys" not in result:
            return {"status": "infrastructure_ok", "accounts": len(accounts)}

        return {"provisioned": needs, "result": result}

    # Map strategy types to the payment providers they need
    STRATEGY_PAYMENT_PROVIDERS: dict[str, list[str]] = {
        "digital_products": ["gumroad"],
        "telegram_bots": ["stripe"],
        "micro_saas": ["stripe", "lemonsqueezy"],
        "saas": ["stripe"],
        "course_creation": ["gumroad", "stripe"],
        "print_on_demand": ["stripe"],
        "freelance_writing": [],  # Paid via platform (Upwork, etc.)
        "domain_flipping": [],  # Paid via marketplace (Sedo, etc.)
    }

    def _ensure_strategy_payment_providers(self, result: dict[str, Any]) -> None:
        """Proactively provision payment providers for active strategies.

        Each selling strategy needs at least one payment provider to collect
        money. This checks active strategies and ensures their required
        providers are set up BEFORE the strategy tries to list/deploy.
        """
        try:
            active_strategies = self.db.execute(
                "SELECT DISTINCT strategy FROM strategy_lifecycle WHERE status = 'active'"
            )
            if not active_strategies:
                return

            provisioned = []
            for row in active_strategies:
                strategy_name = row["strategy"]
                needed_providers = self.STRATEGY_PAYMENT_PROVIDERS.get(strategy_name, [])
                for provider in needed_providers:
                    # Check if this provider is already set up for any brand
                    existing = self.db.execute(
                        "SELECT 1 FROM brand_api_keys WHERE provider = ? AND status = 'active' LIMIT 1",
                        (provider,),
                    )
                    if not existing:
                        self.log_action(
                            "auto_provision_payment",
                            f"Strategy '{strategy_name}' needs {provider} — provisioning now",
                        )
                        try:
                            prov_result = self.api_provisioner._dispatch_provision(
                                provider, strategy_name,
                            )
                            provisioned.append(f"{provider}:{strategy_name}")
                            self.log_action(
                                "auto_provision_result",
                                f"{provider} for {strategy_name}: {prov_result.get('status')}",
                            )
                        except Exception as e:
                            logger.warning(
                                f"Auto-provision {provider} for {strategy_name} failed: {e}"
                            )

            if provisioned:
                result["auto_provisioned_providers"] = provisioned
        except Exception as e:
            logger.warning(f"Strategy payment provider check failed: {e}")

    def _setup_kofi_campaign(self) -> dict[str, Any]:
        """Set up Ko-fi crowdfunding campaign for bootstrap funding."""
        try:
            result = self.kofi_manager.run()
            if result.get("status") == "live":
                self.notify_creator(
                    f"Ko-fi campaign is live! {result.get('kofi_url', '')} — "
                    f"Goal: €500. Share it to get funded!"
                )
            self.log_action("kofi_setup", json.dumps(result, default=str)[:500])
            return result
        except Exception as e:
            logger.error(f"Ko-fi campaign setup failed: {e}")
            return {"status": "error", "error": str(e)}

    def _sync_kofi_donations(self) -> dict[str, Any]:
        """Sync Ko-fi donations into bootstrap system."""
        try:
            return self.kofi_manager.run()
        except Exception as e:
            logger.error(f"Ko-fi sync failed: {e}")
            return {"status": "error", "error": str(e)}

    def _run_failure_postmortem(self, strategy_data: dict) -> None:
        """Analyze why a strategy failed and record prevention rules.

        Runs after auto-pause to extract root causes and store them as
        high-severity lessons so the same mistakes aren't repeated.
        """
        try:
            name = strategy_data.get("name", "unknown")
            analysis = self.think_json(
                f"A monAI strategy was auto-paused due to poor performance.\n\n"
                f"Strategy: {name}\n"
                f"Net profit: €{strategy_data.get('net', 0):.2f}\n"
                f"ROI: {strategy_data.get('roi_pct', 0)}%\n"
                f"Revenue (7d): €{strategy_data.get('trend_7d', {}).get('revenue', 0):.2f}\n"
                f"Expenses (7d): €{strategy_data.get('trend_7d', {}).get('expenses', 0):.2f}\n"
                f"Budget used: {strategy_data.get('budget_used_pct', 0)}%\n\n"
                "Analyze WHY this strategy failed. Consider:\n"
                "1. Was the market too small or saturated?\n"
                "2. Was the product/service quality too low?\n"
                "3. Were costs too high relative to revenue potential?\n"
                "4. Was the execution flawed (wrong platform, bad timing)?\n"
                "5. Was there a technical failure?\n\n"
                "Return: {\"root_causes\": [str], \"prevention_rules\": [str], "
                "\"should_retry\": bool, \"retry_conditions\": str}"
            )

            # Store each prevention rule as a high-severity lesson
            for rule in analysis.get("prevention_rules", []):
                self.memory.store_lesson(
                    agent=self.name,
                    category="strategy_failure",
                    situation=f"Strategy '{name}' auto-paused (net=€{strategy_data.get('net', 0):.2f})",
                    lesson=rule,
                    rule=rule,
                    severity="high",
                )

            self.log_action(
                "failure_postmortem",
                f"{name}: root_causes={analysis.get('root_causes', [])}, "
                f"should_retry={analysis.get('should_retry', False)}",
            )
        except Exception as e:
            logger.warning(f"Post-mortem analysis failed for {strategy_data.get('name', '?')}: {e}")

    def _auto_scale_strategies(self, to_scale: list[dict]) -> list[str]:
        """Increase budget for strategies with strong growth trends.

        Rules:
        - Only scale if ROI > 0 and 7-day revenue trend is positive
        - Boost = 20% of current budget (capped at max_strategy_boost from config)
        - Respects max_strategy_allocation_pct from risk config
        - Notifies creator of budget increases
        """
        if not to_scale:
            return []

        max_boost = getattr(
            getattr(self.config, "reinvestment", None), "max_strategy_boost", 50.0
        )
        max_alloc_pct = self.config.risk.max_strategy_allocation_pct

        total_active_budget = self.db.execute(
            "SELECT COALESCE(SUM(allocated_budget), 0) as total "
            "FROM strategies WHERE status = 'active'"
        )[0]["total"]

        scaled = []
        for s in to_scale:
            sid = s["id"]
            current_budget = s.get("budget", 0)

            # Skip if no budget set
            if current_budget <= 0:
                continue

            # Calculate boost (20% of current, capped)
            boost = min(current_budget * 0.2, max_boost)
            new_budget = current_budget + boost

            # Check spending guard before scaling
            guard_result = self.spending_guard.check_and_record(
                amount=boost,
                category="strategy_auto_scale",
                description=f"Auto-scale strategy '{s['name']}' by €{boost:.2f}",
            )
            if not guard_result["allowed"]:
                logger.info(
                    f"SpendingGuard blocked scaling '{s['name']}': {guard_result['reason']}"
                )
                continue

            # Check allocation limit
            if total_active_budget > 0:
                new_pct = (new_budget / (total_active_budget + boost)) * 100
                if new_pct > max_alloc_pct:
                    logger.info(
                        f"Skip scaling '{s['name']}': would exceed "
                        f"{max_alloc_pct}% allocation ({new_pct:.1f}%)"
                    )
                    continue

            # Update budget in DB
            self.db.execute(
                "UPDATE strategies SET allocated_budget = ?, updated_at = ? "
                "WHERE id = ?",
                (new_budget, datetime.now().isoformat(), sid),
            )

            total_active_budget += boost
            scaled.append(s["name"])
            self.log_action(
                "strategy_auto_scale",
                f"Scaled '{s['name']}': €{current_budget:.0f} → "
                f"€{new_budget:.0f} (+€{boost:.0f}), "
                f"ROI={s['roi_pct']}%, 7d_rev=€{s['trend_7d']['revenue']:.2f}"
            )

        if scaled:
            self.notify_creator(
                f"*Auto-scaled {len(scaled)} growing "
                f"{'strategy' if len(scaled) == 1 else 'strategies'}:*\n"
                + "\n".join(f"- {name}" for name in scaled)
            )

        return scaled

    def _send_financial_reports(self) -> None:
        """Send periodic financial reports to creator via Telegram."""
        try:
            # Monthly report: send on 1st of month
            if self.reporter.should_send_monthly_report():
                report = self.reporter.generate_monthly_report()
                msg = self.reporter.format_telegram_report(report)
                self.notify_creator(msg)
                self.log_action("monthly_report", f"Sent monthly P&L for {report['period']}")

            # Weekly strategy dashboard + reconciliation: send every Monday
            elif self.reporter.should_send_weekly_report():
                dashboard = self.reporter.generate_strategy_dashboard()
                self.notify_creator(dashboard)
                self.log_action("weekly_dashboard", "Sent strategy performance dashboard")

                # Run weekly reconciliation
                recon = self.reconciliation.run_reconciliation()
                if not recon.is_clean:
                    msg = self.reconciliation.format_telegram_report(recon)
                    self.notify_creator(msg)
                self.log_action("reconciliation",
                                f"Run #{recon.run_id}: {recon.matched} matched, "
                                f"{recon.discrepancy_count} discrepancies")

            # Daily snapshot: every cycle (creator can mute in Telegram)
            elif self._cycle % 10 == 0:
                snapshot = self.reporter.generate_daily_snapshot()
                self.notify_creator(snapshot)

        except Exception as e:
            logger.error(f"Financial reporting failed: {e}")

    def _run_api_provisioning(self, force: bool = False) -> dict[str, Any]:
        """Run API key provisioning for brands that need payment provider keys.

        Checks all active brands and provisions missing API keys
        (Stripe, Gumroad, LemonSqueezy, BTCPay) via the APIProvisioner agent.
        Always runs on first cycle; then every 5 cycles to avoid excess attempts.
        Pass force=True to skip the cycle gate (e.g. during bootstrap).
        """
        if not force and self._cycle > 1 and self._cycle % 5 != 1:
            return {"status": "skipped", "reason": "not_provisioning_cycle"}

        try:
            plan = self.api_provisioner.plan()
            if not plan:
                return {"status": "ok", "provisioned": []}

            result = self.api_provisioner.run()
            self.log_action("api_provisioning", json.dumps(result, default=str)[:500])
            return result
        except Exception as e:
            logger.error(f"API provisioning failed: {e}")
            return {"status": "error", "error": str(e)}

    def discover_opportunities(self) -> list[dict[str, Any]]:
        """Discover new money-making opportunities.

        Enriched with research team findings — if research briefs exist,
        they're included in the context so the LLM can prioritize validated niches.
        """
        current = self.db.execute("SELECT name, category, status FROM strategies")
        current_list = [dict(r) for r in current]
        identity = self.identity.get_identity()

        # Pull actionable research briefs to inform opportunity discovery
        research_briefs = self.research_team.get_pursue_briefs()
        research_context = ""
        if research_briefs:
            research_context = (
                "\n\nRESEARCH TEAM FINDINGS (validated opportunities):\n"
                + json.dumps(research_briefs[:5], default=str)[:1500]
            )

        response = self.think_json(
            "Brainstorm 5 NEW money-making opportunities. Think creatively. "
            "Consider ANYTHING legal: services, products, trading, content, "
            "affiliate marketing, SaaS, automation, consulting, reselling, "
            "domain flipping, social media, courses, newsletter monetization, "
            "API services, data products, and anything else. "
            "PRIORITIZE opportunities backed by research team findings if available. "
            "For each: {\"opportunities\": [{\"name\": str, \"category\": str, "
            "\"description\": str, \"how_to_start\": str, "
            "\"estimated_monthly_revenue\": float, \"startup_cost\": float, "
            "\"risk_level\": str, \"time_to_first_revenue_days\": int, "
            "\"platforms_needed\": [str], \"can_automate\": bool}]}",
            context=(
                f"Current strategies: {json.dumps(current_list)}\n"
                f"Identity: {json.dumps(identity, default=str)}"
                f"{research_context}"
            ),
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
                try:
                    self.strategy_lifecycle.pause(
                        s["id"],
                        reason="; ".join(pause_check["reasons"][:3]),
                    )
                except Exception as e:
                    logger.warning(f"Could not pause strategy {s['name']}: {e}")
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
                    results = self._run_async(
                        self.spawner.run_parallel(subagent_tasks, max_steps=20)
                    )

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

        # Record real metrics from strategy results (previous cycle)
        strategy_results = getattr(self, "_last_strategy_results", {})
        for name, agent in self._strategy_agents.items():
            # Record execution outcome from previous cycle
            sr = strategy_results.get(name, {})
            success = 1.0 if sr.get("status") == "ok" else 0.0
            self.self_improver.record_metric(name, self._cycle, "execution_success", success)

            # Record revenue metrics from finance data
            try:
                pnl = self.finance.get_strategy_pnl()
                for s in pnl:
                    if s.get("name") == name:
                        self.self_improver.record_metric(
                            name, self._cycle, "revenue", s.get("revenue", 0.0),
                        )
                        self.self_improver.record_metric(
                            name, self._cycle, "expenses", s.get("expenses", 0.0),
                        )
                        rev, exp = s.get("revenue", 0), s.get("expenses", 0)
                        roi = rev / exp if exp > 0 else 0.0
                        self.self_improver.record_metric(name, self._cycle, "roi", roi)
                        break
            except Exception:
                pass  # Finance data may not be available yet

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

        # Deploy any low-risk proposed improvements across all agents
        deployed = self.self_improver.deploy_improvements()
        results["_deployed"] = deployed
        if deployed:
            self.log_action(
                "self_improvement_deployed",
                json.dumps({"count": len(deployed), "items": deployed}, default=str)[:500],
            )

        return results

    def _run_product_iteration(self) -> dict[str, Any]:
        """Analyze product performance and trigger improvement cycles."""
        # Only run every 5 cycles to avoid excessive API costs
        if self._cycle % 5 != 0:
            return {"status": "skipped", "reason": "not_iteration_cycle"}

        try:
            result = self.product_iterator.run()

            # Apply pending improvements to actual products via strategy agents
            for name, agent in self._strategy_agents.items():
                pending = self.product_iterator.get_pending_improvements(name)
                if pending:
                    self.log_action(
                        "product_improvements_pending",
                        f"{name}: {len(pending)} improvement(s) queued",
                    )
                    # Call the strategy's apply_improvements() to rebuild code/content
                    if hasattr(agent, "apply_improvements"):
                        try:
                            apply_result = agent.apply_improvements()
                            self.log_action(
                                "product_improvements_applied",
                                f"{name}: {json.dumps(apply_result, default=str)[:300]}",
                            )
                        except Exception as e:
                            logger.error(f"Failed to apply improvements for {name}: {e}")
                            # Mark remaining pending as applied to prevent infinite retry
                            for p in pending:
                                self.product_iterator.mark_applied(p["id"])
                    else:
                        # Strategy doesn't support improvements yet — mark as applied
                        for p in pending:
                            self.product_iterator.mark_applied(p["id"])

            self.log_action("product_iteration", json.dumps(result, default=str)[:500])
            return result
        except Exception as e:
            logger.error(f"Product iteration failed: {e}")
            return {"status": "error", "error": str(e)}

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

    def _run_teams_parallel(self) -> dict[str, Any]:
        """Run finance, research, marketing, social, and web teams in parallel.

        Uses asyncio to run independent team operations concurrently,
        reducing total cycle time compared to sequential execution.
        """
        import concurrent.futures

        teams = {
            "finance": self._run_finance_expert,
            "research": self._run_market_research,
            "marketing": self._run_marketing_team,
            "social": self._run_social_presence,
            "web": self._run_web_presence,
        }

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(fn): name
                for name, fn in teams.items()
            }
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result(timeout=120)
                except Exception as e:
                    logger.error(f"Team {name} failed in parallel execution: {e}")
                    results[name] = {"status": "error", "error": str(e)}

        # Wire research findings into next discovery cycle
        research = results.get("research", {})
        if research.get("briefs"):
            pursue_briefs = [
                b for b in research["briefs"]
                if b.get("recommended_action") == "pursue"
            ]
            if pursue_briefs:
                self.share_knowledge(
                    "opportunity", "research_recommendations",
                    json.dumps(pursue_briefs, default=str)[:2000],
                    tags=["research", "actionable"],
                )
                self.log_action("research_wired",
                                f"{len(pursue_briefs)} actionable research briefs shared")

        return results

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
        """Run payment processing — track payouts and generate contractor invoices.

        Adapts to configured flow:
        - LLC+Contractor: track platform payouts, generate monthly invoices
        - Crypto: sweep XMR from brand wallets to creator
        Runs every 3 cycles.
        """
        if self._cycle % 3 != 0:
            return {"status": "skipped", "reason": "not_payment_cycle"}

        try:
            sweep_result = self._run_async(
                self.payment_manager.run_sweep_cycle()
            )
            health = self._run_async(
                self.payment_manager.health_check()
            )

            result = {
                "sweep": sweep_result,
                "health": health,
                "status": self.payment_manager.get_status(),
            }

            flow = sweep_result.get("flow", "")

            # Notify creator based on flow type
            if flow == "llc_contractor":
                invoice = sweep_result.get("invoice", {})
                if invoice.get("status") == "generated":
                    self.log_action("payment_invoice",
                                    f"Invoice {invoice['invoice_number']}: €{invoice['amount']:.2f}")
                    self.audit.log(
                        "orchestrator", "payment", "invoice_generated",
                        details={"invoice_number": invoice["invoice_number"],
                                 "amount": invoice["amount"]},
                        risk_level="high",
                    )
                    self.notify_creator(
                        f"Invoice generated: {invoice['invoice_number']} — "
                        f"€{invoice['amount']:.2f}. Ready for payment."
                    )
            elif flow == "crypto_xmr":
                if sweep_result.get("sweeps_successful", 0) > 0:
                    xmr = sweep_result.get("total_xmr_swept", 0)
                    self.log_action("payment_sweep", f"Swept {xmr:.8f} XMR")
                    self.audit.log(
                        "orchestrator", "payment", "xmr_sweep",
                        details={"total_xmr": xmr}, risk_level="high",
                    )
                    self.notify_creator(f"Swept {xmr:.8f} XMR to your wallet.")

            return result

        except Exception as e:
            logger.error(f"Payment cycle failed: {e}")
            self.audit.log("orchestrator", "payment", "payment_cycle_failed",
                           details={"error": str(e)}, success=False,
                           risk_level="high")
            return {"status": "error", "error": str(e)}

    def _run_strategies(self) -> dict[str, Any]:
        import concurrent.futures

        results = {}
        active = self.db.execute("SELECT name FROM strategies WHERE status = 'active'")
        active_names = {r["name"] for r in active}
        strategy_timeout = 300  # 5 minutes max per strategy

        for name, agent in self._strategy_agents.items():
            if name in active_names:
                # Skip quarantined agents
                if self.ethics_tester.is_quarantined(name):
                    results[name] = {"status": "quarantined", "reason": "ethics_failure"}
                    continue
                t0 = datetime.now()
                try:
                    # Run with timeout to prevent hung strategies from freezing daemon
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(agent.run)
                        result = future.result(timeout=strategy_timeout)
                    duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
                    results[name] = {"status": "ok", "result": result}
                    # Share success insights
                    agent.share_knowledge(
                        category="insight",
                        topic=f"{name}_cycle_result",
                        content=json.dumps(result, default=str)[:500],
                        tags=[name, "strategy_result"],
                    )
                    # Update task router with success feedback
                    self.task_router.update_performance(
                        name, "strategy_execution", True, duration_ms,
                    )
                except concurrent.futures.TimeoutError:
                    duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
                    logger.error(f"Strategy {name} timed out after {strategy_timeout}s")
                    results[name] = {"status": "timeout", "error": f"Exceeded {strategy_timeout}s"}
                    self.task_router.update_performance(
                        name, "strategy_execution", False, duration_ms,
                    )
                except Exception as e:
                    duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
                    logger.error(f"Strategy {name} failed: {e}")
                    results[name] = {"status": "error", "error": str(e)}
                    # Auto-learn from failure
                    agent.learn_from_error(e, context=f"Running strategy {name}")
                    # Update task router with failure feedback
                    self.task_router.update_performance(
                        name, "strategy_execution", False, duration_ms,
                    )

        # Store results for next cycle's metrics recording
        self._last_strategy_results = results
        return results
