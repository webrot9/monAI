"""Orchestrator agent — thin coordinator.

Wires together InfrastructureManager, FinanceController, StrategyRunner,
and OperationsManager. Each cycle delegates to focused subsystems.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.agents.browser_learner import BrowserLearner
from monai.agents.collaboration import CollaborationHub
from monai.agents.fact_checker import FactChecker
from monai.agents.finance_controller import FinanceController
from monai.agents.finance_expert import FinanceExpert
from monai.agents.humanizer import Humanizer
from monai.agents.identity import IdentityManager
from monai.agents.infrastructure import InfrastructureManager
from monai.agents.legal import LegalAdvisorFactory
from monai.agents.operations import OperationsManager
from monai.agents.product_iterator import ProductIterator
from monai.agents.self_improve import SelfImprover
from monai.agents.spawner import AgentSpawner
from monai.agents.strategy_runner import StrategyRunner
from monai.business.bootstrap import BootstrapWallet
from monai.business.brand_payments import BrandPayments
from monai.business.crm import CRM
from monai.business.kofi import KofiCampaignManager
from monai.business.risk import RiskManager
from monai.config import Config
from monai.db.database import Database
from monai.payments.manager import UnifiedPaymentManager
from monai.utils.llm import LLM, BudgetExceededError, get_cost_tracker
from monai.utils.privacy import get_anonymizer
from monai.workflows.engine import WorkflowEngine
from monai.workflows.pipelines import get_pipeline
from monai.workflows.router import TaskRouter

logger = logging.getLogger(__name__)


class Orchestrator(BaseAgent):
    name = "orchestrator"
    description = (
        "Fully autonomous master agent. Thin coordinator that delegates to "
        "InfrastructureManager, FinanceController, StrategyRunner, OperationsManager."
    )

    # Expose constants for backward compat (tests set these on instances)
    SELF_HEALING_CONFIG = StrategyRunner.SELF_HEALING_CONFIG
    STRATEGY_PAYMENT_PROVIDERS = InfrastructureManager.STRATEGY_PAYMENT_PROVIDERS
    TOR_BLOCKED_PROVIDERS = InfrastructureManager.TOR_BLOCKED_PROVIDERS

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)

        # ── Core subsystems ──────────────────────────────────
        self.ops = OperationsManager(config, db, llm)
        self.fc = FinanceController(config, db)
        self.strategy_runner = StrategyRunner(config, db)
        self.risk = RiskManager(config, db)

        # ── Shared services ──────────────────────────────────
        self.identity = IdentityManager(config, db, llm)
        self.crm = CRM(db)
        self.spawner = AgentSpawner(config, db, llm)
        self.collab = CollaborationHub(config, db)
        self.legal = LegalAdvisorFactory(config, db, llm)
        self.humanizer = Humanizer(config, db, llm)
        self.fact_checker = FactChecker(config, db, llm)
        self.browser_learner = BrowserLearner(config, db, llm)
        self.self_improver = SelfImprover(config, db, llm, memory=self.memory)
        self.product_iterator = ProductIterator(config, db, llm)

        # ── Payment ──────────────────────────────────────────
        self.brand_payments = BrandPayments(db)
        self._payment_manager_obj = UnifiedPaymentManager(
            config, db, ledger=self.fc.ledger,
        )

        # ── Bootstrap ────────────────────────────────────────
        self._bootstrap_wallet_obj = BootstrapWallet(
            config, db, ledger=self.fc.ledger,
        )
        self.fc.set_bootstrap_wallet(self._bootstrap_wallet_obj)
        self.kofi_manager = KofiCampaignManager(
            config, db, llm, bootstrap_wallet=self._bootstrap_wallet_obj,
        )

        # ── Infrastructure ───────────────────────────────────
        self.infra = InfrastructureManager(
            config, db, llm,
            identity=self.identity,
            payment_manager=self._payment_manager_obj,
            ledger=self.fc.ledger,
            bootstrap_wallet=self._bootstrap_wallet_obj,
            telegram=self.ops.telegram,
            audit=self.ops.audit,
        )

        # ── Workflows ────────────────────────────────────────
        self.workflow_engine = WorkflowEngine(config, db, llm)
        self.task_router = TaskRouter(config, db, llm)
        self.finance_expert = FinanceExpert(
            config, db, llm, commercialista=self.fc.commercialista,
        )

        # Register utility agents with workflow engine
        for agent_name, agent_obj in [
            ("humanizer", self.humanizer),
            ("fact_checker", self.fact_checker),
            ("finance_expert", self.finance_expert),
            ("api_provisioner", self.infra.api_provisioner),
        ]:
            self.workflow_engine.register_agent(agent_name, agent_obj)

        # Load cost tracker state
        cost_state_path = str(config.data_dir / "cost_tracker.json")
        get_cost_tracker().load_state(cost_state_path)

        # Startup anonymity check
        if config.privacy.proxy_type != "none" and getattr(config.privacy, "verify_anonymity", True):
            anon = self.ops.check_anonymity()
            if anon.get("anonymous"):
                logger.info("Startup anonymity verified: %s", anon.get("visible_ip", "unknown"))

    def register_strategy(self, agent: BaseAgent):
        self.strategy_runner.register(
            agent,
            payment_manager=self.payment_manager,
            workflow_engine=self.workflow_engine,
            log_action=self.log_action,
        )

    def plan(self) -> list[str]:
        """Generate the orchestrator's action plan for this cycle."""
        health = self.risk.get_portfolio_health()
        identity = self.identity.get_identity()
        accounts = self.identity.get_all_accounts()
        resource_costs = self.identity.get_monthly_resource_costs()
        pipeline = self.crm.get_pipeline_summary()
        failure_lessons = self._get_strategy_failure_context()

        account_list = [{"platform": a["platform"], "type": a["type"]} for a in accounts]
        platforms_available = list({a["platform"] for a in accounts})

        context = json.dumps({
            "portfolio_health": health,
            "identity": identity,
            "accounts_count": len(accounts),
            "accounts": account_list,
            "asset_summary": {
                "platforms": platforms_available,
                "has_domain": any(a["type"] == "domain" for a in accounts),
                "has_payment_method": any(a["type"] in ("payment", "stripe", "paypal", "crypto") for a in accounts),
                "has_email": any(a["type"] == "email" or a["platform"] == "email" for a in accounts),
            },
            "monthly_resource_costs": resource_costs,
            "client_pipeline": pipeline,
            "total_revenue": self.fc.finance.get_total_revenue(),
            "total_expenses": self.fc.finance.get_total_expenses(),
            "net_profit": self.fc.finance.get_net_profit(),
            "past_failures_and_lessons": failure_lessons,
        }, indent=2, default=str)

        plan_response = self.think_json(
            "You are a fully autonomous AI business. Plan your next cycle.\n\n"
            "RULES:\n"
            "- Infrastructure provisioning is handled SEPARATELY.\n"
            "- Each action MUST be specific and actionable.\n"
            "- ONLY plan actions executable with current assets.\n"
            "- Max 5 actions per cycle.\n\n"
            "Return: {\"actions\": [{\"action\": str, \"priority\": int, "
            "\"reason\": str, \"delegate_to_subagent\": bool}]}",
            context=context,
        )
        actions = plan_response.get("actions", [])
        self.log_action("plan", f"Generated {len(actions)} actions", json.dumps(actions))
        return actions

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Execute one full autonomous orchestration cycle."""
        self._cycle += 1
        from monai.agents.executor import AutonomousExecutor
        AutonomousExecutor.reset_cycle()

        self.log_action("cycle_start", f"Cycle {self._cycle} at {datetime.now()}")
        self.ops.audit.log("orchestrator", "system", "cycle_start",
                           details={"cycle": self._cycle})
        cycle_result: dict[str, Any] = {}

        # ── Pre-flight checks ────────────────────────────────
        # Anonymity
        anon = self.ops.check_anonymity()
        cycle_result["anonymity"] = anon
        if self.config.privacy.verify_anonymity and self.config.privacy.proxy_type != "none":
            if not anon.get("anonymous"):
                self.ops.audit.log("orchestrator", "system", "anonymity_check_failed",
                                   details=anon, success=False, risk_level="critical")
                return {"status": "aborted", "reason": "anonymity_check_failed", **cycle_result}

        # Telegram
        cycle_result["telegram"] = self.ops.handle_telegram(
            finance=self.fc, risk=self.risk,
        )

        # Resources
        resources = self.ops.check_resources()
        cycle_result["resources"] = resources
        if not resources["all_ok"]:
            return {"status": "aborted", "reason": "resource_limits_exceeded", **cycle_result}

        # Budget
        budget = self.fc.get_budget()
        cycle_result["budget"] = budget
        if budget["balance"] <= 0:
            self.ops.audit.log("orchestrator", "system", "budget_exhausted",
                               details=budget, success=False, risk_level="high")
        self.log_action("budget_check",
                        f"€{budget['balance']:.2f} remaining, "
                        f"burn: €{budget['burn_rate_daily']:.4f}/day")

        # Set per-cycle limits
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

        # LLM health check
        llm_health = self.llm.health_check()
        if not llm_health["available"]:
            reason = "llm_quota_exhausted" if llm_health["quota_exhausted"] else "llm_unavailable"
            cycle_result["status"] = reason
            return cycle_result

        try:
            return self._execute_cycle(cycle_result, budget)
        except BudgetExceededError as exc:
            self.ops.audit.log("orchestrator", "system", "cycle_budget_exceeded",
                               details={"error": str(exc)}, success=False, risk_level="high")
            cycle_result["status"] = "budget_exceeded"
            cycle_result["timestamp"] = datetime.now().isoformat()
            cycle_result["api_costs_session"] = get_cost_tracker().get_summary()
            cycle_result["budget_after"] = self.fc.get_budget()
            return cycle_result

    def _execute_cycle(self, cycle_result: dict, budget: dict) -> dict[str, Any]:
        """Execute the main cycle phases."""
        # Phase 0: Start cycle for all agents
        for name, agent in self.strategy_runner.agents.items():
            if not self.ops.ethics_tester.is_quarantined(name):
                try:
                    agent.start_cycle(self._cycle)
                except RuntimeError as e:
                    if "quarantined" not in str(e).lower():
                        raise
        self.start_cycle(self._cycle)

        # Phase 0.5: Collaboration
        cycle_result["collaboration"] = self._process_agent_requests()

        # Phase 0.9: Asset verification
        cycle_result["asset_verification"] = self.infra.verify_stored_assets(
            log_action=self.log_action, learn=self.learn,
        )

        # Phase 0.95: Strategy validation
        cycle_result["strategy_validation"] = self.strategy_runner.validate()

        # Phase 1: Infrastructure
        cycle_result["provisioning"] = self.infra.ensure_infrastructure(
            self._cycle,
            log_action=self.log_action,
            learn=self.learn,
            notify_creator=self.ops.notify_creator,
            kofi_manager=self.kofi_manager,
        )

        # Phase 2: Health check
        health = self.risk.get_portfolio_health()
        cycle_result["health"] = health
        self.log_action("health_check", json.dumps(health, default=str)[:500])

        # Phase 3: Review strategies
        cycle_result["reviews"] = self._review_strategies()

        # Phase 4: Plan and execute
        actions = self.plan()
        action_results = []
        subagent_tasks = []
        for item in sorted(actions, key=lambda x: x.get("priority", 99)):
            action = item["action"]
            if item.get("delegate_to_subagent"):
                subagent_tasks.append({"name": action.replace(" ", "_")[:30], "task": action})
            else:
                result = self._execute_action(action)
                action_results.append({"action": action, "result": result})
        cycle_result["direct_actions"] = action_results

        # Audit direct actions
        for ar in action_results:
            self.ops.audit.log("orchestrator", "system", "execute_action",
                               details={"action": ar["action"]},
                               result=str(ar.get("result", ""))[:200])

        # Phase 5: Sub-agents
        if subagent_tasks:
            try:
                subagent_results = self._run_async(self.spawner.run_parallel(subagent_tasks))
                cycle_result["subagent_results"] = {
                    k: {"status": v.get("status")} for k, v in subagent_results.items()
                }
            except Exception as e:
                cycle_result["subagent_results"] = {"error": str(e)}

        # Phase 6: Run strategies
        cycle_result["strategy_results"] = self.strategy_runner.run_all(
            ethics_tester=self.ops.ethics_tester,
            task_router=self.task_router,
            log_action=self.log_action,
            learn=self.learn,
        )

        # Phase 6.1: Ethics
        cycle_result["ethics"] = self.ops.run_ethics_checks(
            list(self.strategy_runner.agents.keys()),
            log_action=self.log_action,
            learn=self.learn,
            notify_creator=self.ops.notify_creator,
        )

        # Phase 6.5: Self-improvement
        cycle_result["self_improvement"] = self._run_self_improvement()

        # Phase 6.58: Product iteration
        cycle_result["product_iteration"] = self._run_product_iteration()

        # Phase 6.8: Browser metrics
        cycle_result["browser_metrics"] = self._get_browser_metrics()

        # Phase 6.85: Exchange rates (every 6 cycles)
        if self._cycle % 6 == 0:
            cycle_result["exchange_rates_refreshed"] = self.fc.refresh_exchange_rates()

        # Phase 6.85b: Mid-cycle anonymity recheck
        if self.config.privacy.verify_anonymity and self.config.privacy.proxy_type != "none":
            recheck = self.ops.check_anonymity()
            cycle_result["anonymity_recheck"] = recheck
            if not recheck.get("anonymous"):
                logger.critical("MID-CYCLE ANONYMITY LOST. Aborting remaining phases.")
                cycle_result["status"] = "partial"
                cycle_result["reason"] = "anonymity_lost_mid_cycle"
                return cycle_result

        # Phase 6.9: Payments (every 3 cycles)
        if self._cycle % 3 == 0:
            cycle_result["payments"] = self.fc.run_payment_cycle(
                payment_manager=self.payment_manager,
                audit=self.ops.audit,
                notify_creator=self.ops.notify_creator,
                log_action=self.log_action,
                run_async=self._run_async,
            )

        # Phase 6.95: Ledger integrity
        cycle_result["ledger_integrity"] = self.fc.verify_ledger_integrity(
            audit=self.ops.audit,
        )

        # Phase 6.97: Strategy performance
        self._evaluate_and_adjust_strategies(cycle_result)

        # Phase 6.99: Reinvestment
        cycle_result["reinvestment"] = self.fc.compute_reinvestment(
            log_action=self.log_action,
        )

        # Phase 7: Commercialista report
        cycle_result["timestamp"] = datetime.now().isoformat()
        cycle_result["net_profit"] = self.fc.finance.get_net_profit()
        cycle_result["api_costs_session"] = get_cost_tracker().get_summary()
        cycle_result["budget_after"] = self.fc.get_budget()

        # Phase 7.5: Financial reports
        self.fc.send_financial_reports(
            notify_creator=self.ops.notify_creator,
            log_action=self.log_action,
            cycle=self._cycle,
        )

        # Phase 8: Reflect
        self._reflect_on_cycle(cycle_result)

        # Phase 9: Broadcast
        self.broadcast(
            "info", f"Cycle {self._cycle} complete",
            json.dumps({
                "net_profit": cycle_result["net_profit"],
                "actions_taken": len(action_results),
            }, default=str),
        )

        self.ops.audit.log("orchestrator", "system", "cycle_complete",
                           details={"cycle": self._cycle,
                                    "net_profit": cycle_result["net_profit"]})

        # Persist cost tracker
        get_cost_tracker().save_state(str(self.config.data_dir / "cost_tracker.json"))

        # Phase 9.5: Backups
        self.ops.run_scheduled_backups(self._cycle)

        # Phase 10: Alerts
        self.ops.send_auto_alerts(
            cycle_result, cycle_result.get("budget_after", {}),
            finance=self.fc, cycle=self._cycle,
        )

        return cycle_result

    def _evaluate_and_adjust_strategies(self, cycle_result: dict) -> None:
        """Phase 6.97: Performance evaluation + auto-pause/scale."""
        try:
            perf = self.fc.evaluate_strategy_performance()
            cycle_result["strategy_performance"] = {
                "total_revenue": perf["total_revenue"],
                "total_expenses": perf["total_expenses"],
                "overall_roi_pct": perf["overall_roi_pct"],
            }

            paused = self.strategy_runner.auto_pause_losers(
                perf,
                log_action=self.log_action,
                audit=self.ops.audit,
                notify_creator=self.ops.notify_creator,
                run_postmortem=self._run_failure_postmortem,
            )

            if paused:
                freed = self.strategy_runner.reallocate_paused_budget(
                    paused, perf,
                    log_action=self.log_action,
                    notify_creator=self.ops.notify_creator,
                )
                cycle_result["strategy_performance"]["reallocated"] = freed

            for s in perf["strategies_to_review"]:
                self.log_action("strategy_review",
                                f"'{s['name']}' needs review: net=€{s['net']:.2f}")

            scaled = self.strategy_runner.auto_scale_winners(
                perf["strategies_to_scale"],
                log_action=self.log_action,
                notify_creator=self.ops.notify_creator,
            )
            cycle_result["strategy_performance"]["scaled"] = len(scaled)
        except Exception as e:
            logger.error(f"Strategy performance eval failed: {e}")

    # ── Delegated helpers (kept thin) ────────────────────────

    def _get_strategy_failure_context(self) -> list[str]:
        lines: list[str] = []
        try:
            lessons = self.memory.get_lessons(agent=self.name, include_shared=True)
            for lesson in lessons:
                if lesson.get("severity") in ("high", "critical"):
                    lines.append(f"[{lesson.get('category', '?')}] {lesson.get('lesson', '')}")
        except Exception:
            pass
        try:
            rows = self.db.execute(
                "SELECT action, platform, fail_count, reason "
                "FROM provision_failures ORDER BY fail_count DESC LIMIT 20"
            )
            for r in rows:
                lines.append(f"[provision_failure] {r['action']} on {r['platform']}: {r['fail_count']}x")
        except Exception:
            pass
        return lines

    def _review_strategies(self) -> list[dict[str, Any]]:
        reviews = []
        strategies = self.db.execute("SELECT * FROM strategies WHERE status = 'active'")
        for strategy in strategies:
            s = dict(strategy)
            pause_check = self.risk.should_pause_strategy(s["id"])
            roi = self.fc.finance.get_roi(s["id"])
            review = {
                "strategy": s["name"], "roi": roi,
                "should_pause": pause_check["should_pause"],
                "reasons": pause_check["reasons"],
            }
            if pause_check["should_pause"]:
                try:
                    self.strategy_runner.strategy_lifecycle.pause(
                        s["id"], reason="; ".join(pause_check["reasons"][:3]),
                    )
                except Exception as e:
                    logger.warning(f"Could not pause {s['name']}: {e}")
            reviews.append(review)
        return reviews

    def _execute_action(self, action: str) -> str:
        if action == "discover_opportunities":
            return "Discovery queued"
        elif action == "rebalance":
            pnl = self.fc.finance.get_strategy_pnl()
            winners = [s for s in pnl if s["net"] > 0]
            return f"Winners: {len(winners)}, Losers: {len(pnl) - len(winners)}"
        elif "provision" in action.lower() or "register" in action.lower():
            legal_result = self.legal.assess_activity(
                activity_name=action[:30].lower().replace(" ", "_"),
                activity_type="registration", description=action,
            )
            if legal_result["status"] == "blocked":
                return f"BLOCKED by legal: {action}"
            result = self.infra.provisioner.run()
            return json.dumps(result, default=str)[:500]
        else:
            return f"Action '{action}' queued"

    def _process_agent_requests(self) -> dict[str, Any]:
        messages = self.check_messages()
        processed = []
        for msg in messages:
            processed.append({"from": msg["from_agent"], "type": msg["msg_type"]})
            self.memory.mark_message_acted_on(msg["id"])
        return {"processed": len(processed)}

    def _run_failure_postmortem(self, strategy_data: dict) -> None:
        try:
            name = strategy_data.get("name", "unknown")
            analysis = self.think_json(
                f"Strategy '{name}' was auto-paused. "
                f"Net: €{strategy_data.get('net', 0):.2f}, ROI: {strategy_data.get('roi_pct', 0)}%.\n"
                "Analyze root causes. Return: {\"root_causes\": [str], \"prevention_rules\": [str]}"
            )
            for rule in analysis.get("prevention_rules", []):
                self.memory.store_lesson(
                    agent=self.name, category="strategy_failure",
                    situation=f"'{name}' auto-paused", lesson=rule, rule=rule,
                    severity="high",
                )
        except Exception as e:
            logger.warning(f"Post-mortem failed for {strategy_data.get('name', '?')}: {e}")

    def _run_self_improvement(self) -> dict[str, Any]:
        if self._cycle % 3 != 0:
            return {"status": "skipped"}
        results = {}
        for name, agent in self.strategy_runner.agents.items():
            analysis = self.self_improver.analyze_performance(name)
            if analysis["data_richness"] == "good":
                improvements = self.self_improver.generate_improvements(name)
                results[name] = {"improvements_proposed": len(improvements)}
            else:
                results[name] = {"analysis": "sparse_data"}
        deployed = self.self_improver.deploy_improvements()
        results["_deployed"] = deployed
        return results

    def _run_product_iteration(self) -> dict[str, Any]:
        if self._cycle % 5 != 0:
            return {"status": "skipped"}
        try:
            return self.product_iterator.run()
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _get_browser_metrics(self) -> dict[str, Any]:
        try:
            return {
                "success_rates": self.browser_learner.get_success_rate(),
                "failure_breakdown": self.browser_learner.get_failure_breakdown(),
            }
        except Exception:
            return {"status": "no_data"}

    def _run_scheduled_backups(self) -> dict[str, Any]:
        """Backward compat delegate."""
        return self.ops.run_scheduled_backups(self._cycle)

    # Backward compat delegates for self-healing (now on StrategyRunner)
    def _init_strategy_health(self) -> None:
        self.strategy_runner._init_strategy_health()

    def record_strategy_proxy_failure(self, strategy_name: str, reason: str) -> None:
        self.strategy_runner.record_strategy_proxy_failure(strategy_name, reason)

    def record_strategy_success(self, strategy_name: str) -> None:
        self.strategy_runner.record_strategy_success(strategy_name)

    def _is_strategy_auto_paused(self, strategy_name: str) -> bool:
        try:
            rows = self.db.execute(
                "SELECT auto_paused_at FROM strategy_health "
                "WHERE strategy_name = ? AND auto_paused_at IS NOT NULL",
                (strategy_name,),
            )
            return bool(rows)
        except Exception:
            return False

    def _get_strategy_pause_reason(self, strategy_name: str) -> str:
        try:
            rows = self.db.execute(
                "SELECT last_failure_reason, consecutive_proxy_failures, "
                "next_retry_at FROM strategy_health "
                "WHERE strategy_name = ? AND auto_paused_at IS NOT NULL",
                (strategy_name,),
            )
            if rows:
                r = rows[0]
                import time as _time
                remaining = max(0, int((r["next_retry_at"] or 0) - _time.time()))
                return (
                    f"proxy failures ({r['consecutive_proxy_failures']}x): "
                    f"{r['last_failure_reason']} — retry in {remaining}s"
                )
        except Exception:
            pass
        return ""

    def _check_strategy_retries(self, results: dict[str, Any]) -> None:
        self.strategy_runner._check_strategy_retries(results)

    def _reflect_on_cycle(self, cycle_result: dict):
        strategy_results = cycle_result.get("strategy_results", {})
        for name, result in strategy_results.items():
            if result.get("status") == "error":
                self.learn(
                    category="mistake",
                    situation=f"Strategy {name} failed: {result.get('error', 'unknown')}",
                    lesson=f"Strategy {name} encountered an error",
                    rule=f"Monitor {name} closely",
                    severity="high",
                )

    # ── Public API (used by other agents) ────────────────────

    def notify_creator(self, message: str) -> bool:
        return self.ops.notify_creator(message)

    def ask_creator(self, question: str, timeout: int = 3600) -> str | None:
        return self.ops.ask_creator(question, timeout)

    def humanize_content(self, content: str, style: str = "default",
                         context: str = "") -> str:
        return self.humanizer.humanize(content, style, context)

    def get_phone_number(self, platform: str, requesting_agent: str) -> dict[str, Any]:
        return self.infra.get_phone_number(platform, requesting_agent)

    def run_pipeline(self, pipeline_name: str, context: dict | None = None) -> dict[str, Any]:
        pipeline = get_pipeline(pipeline_name)
        if not pipeline:
            return {"status": "error", "reason": f"Pipeline '{pipeline_name}' not found"}
        return self.workflow_engine.execute(pipeline, context)

    def route_task(self, task: str, task_type: str = "", priority: int = 5) -> dict[str, Any]:
        return self.task_router.route(task, task_type, priority)

    # Backward compat — expose subsystem objects that tests/other code may reference
    @property
    def finance(self):
        return self.fc.finance

    @property
    def ledger(self):
        return self.fc.ledger

    @property
    def commercialista(self):
        return self.fc.commercialista

    @property
    def spending_guard(self):
        return self.fc.spending_guard

    @property
    def exchange_rates(self):
        return self.fc.exchange_rates

    @property
    def reporter(self):
        return self.fc.reporter

    @property
    def reconciliation(self):
        return self.fc.reconciliation

    @property
    def telegram(self):
        return self.ops.telegram

    @telegram.setter
    def telegram(self, value):
        self.ops.telegram = value

    @property
    def ethics_tester(self):
        return self.ops.ethics_tester

    @property
    def audit(self):
        return self.ops.audit

    @property
    def alerting(self):
        return self.ops.alerting

    @property
    def backup_manager(self):
        return self.ops.backup_manager

    @property
    def strategy_lifecycle(self):
        return self.strategy_runner.strategy_lifecycle

    @property
    def _strategy_agents(self):
        return self.strategy_runner.agents

    @_strategy_agents.setter
    def _strategy_agents(self, value):
        self.strategy_runner._strategy_agents = value

    @property
    def corporate(self):
        return self.infra.corporate

    @property
    def provisioner(self):
        return self.infra.provisioner

    @property
    def llc_provisioner(self):
        return self.infra.llc_provisioner

    @property
    def phone_provisioner(self):
        return self.infra.phone_provisioner

    @property
    def api_provisioner(self):
        return self.infra.api_provisioner

    @property
    def bootstrap_wallet(self):
        return self._bootstrap_wallet_obj

    @bootstrap_wallet.setter
    def bootstrap_wallet(self, value):
        self._bootstrap_wallet_obj = value

    @property
    def payment_manager(self):
        return self._payment_manager_obj

    @payment_manager.setter
    def payment_manager(self, value):
        self._payment_manager_obj = value
