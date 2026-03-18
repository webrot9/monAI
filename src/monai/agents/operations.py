"""Operations manager — audit, alerting, backup, ethics, telegram, resources."""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.agents.ethics_test import EthicsTester
from monai.business.alerting import AlertingEngine
from monai.business.audit import AuditTrail
from monai.business.backup import BackupManager
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM, BudgetExceededError, get_cost_tracker
from monai.utils.privacy import get_anonymizer
from monai.utils.resources import check_resources
from monai.utils.sandbox import PROJECT_ROOT
from monai.utils.telegram import TelegramBot

logger = logging.getLogger(__name__)


class OperationsManager:
    """Handles audit, alerting, backup, ethics, telegram, and resource monitoring."""

    def __init__(self, config: Config, db: Database, llm: LLM):
        self.config = config
        self.db = db
        self.telegram = TelegramBot(config, db)
        self.ethics_tester = EthicsTester(config, db, llm)
        self.audit = AuditTrail(db)
        self.alerting = AlertingEngine(db)
        self.backup_manager = BackupManager(
            db, config.data_dir / "backups",
            max_backups=config.backup.max_backups,
        )

    def check_anonymity(self) -> dict[str, Any]:
        """Verify anonymity. Returns status dict."""
        if not self.config.privacy.verify_anonymity or self.config.privacy.proxy_type == "none":
            return {"status": "skipped"}
        anonymizer = get_anonymizer(self.config)
        anon_status = anonymizer.startup_check()
        if not anon_status.get("anonymous"):
            logger.critical("ANONYMITY CHECK FAILED: %s", anon_status)
            self.audit.log("orchestrator", "system", "anonymity_check_failed",
                           details=anon_status, success=False, risk_level="critical")
        return anon_status

    def check_resources(self) -> dict[str, Any]:
        """Check system resources (CPU, memory, disk)."""
        resources = check_resources(PROJECT_ROOT, self.config.data_dir)
        if not resources["all_ok"]:
            logger.warning("Resource limit exceeded: %s", resources)
            self.audit.log("orchestrator", "system", "resource_limit_exceeded",
                           details=resources, success=False, risk_level="critical")
        return resources

    def handle_telegram(self, *, finance: Any, risk: Any) -> dict[str, Any]:
        """Process Telegram updates and handle creator communication."""
        if not self.config.telegram.enabled:
            return {"status": "disabled"}
        if not self.telegram.has_token:
            return {"status": "not_provisioned"}

        try:
            if not self.telegram._verification_token:
                self.telegram.generate_verification()
            updates = self.telegram.process_updates()
            result: dict[str, Any] = {"status": "ok", "updates": len(updates)}

            for update in updates:
                if update["type"] == "status_request":
                    budget = finance.get_budget()
                    health = risk.get_portfolio_health()
                    self.telegram.send_report("Status Report", {
                        "Budget": f"€{budget['balance']:.2f} remaining",
                        "Strategies": f"{health.get('active_strategies', 0)} active",
                        "Net Profit": f"€{finance.finance.get_net_profit():.2f}",
                    })
                elif update["type"] == "report_request":
                    report = finance.commercialista.get_full_report()
                    self.telegram.send_report("Full Report", {
                        "Budget": json.dumps(report.get("budget", {}), default=str),
                        "Costs by Agent": json.dumps(report.get("costs_by_agent", []), default=str),
                        "Recommendation": report.get("recommendation", "N/A"),
                    })
            return result
        except Exception as e:
            logger.error(f"Telegram handling failed: {e}")
            return {"status": "error", "error": str(e)}

    def run_ethics_checks(
        self, strategy_names: list[str], *, log_action: Any,
        learn: Any, notify_creator: Any,
    ) -> dict[str, Any]:
        """Run ethics tests on registered agents."""
        results = {}

        tracker = get_cost_tracker()
        remaining = tracker.max_cycle_calls - tracker.cycle_calls
        if remaining < 30:
            return {"status": "skipped", "reason": "insufficient_budget"}

        reset_agents = self.ethics_tester.auto_reset_stale_quarantines(max_age_hours=24)
        if reset_agents:
            results["auto_reset"] = reset_agents

        for name in strategy_names:
            summary = self.ethics_tester.get_agent_ethics_summary(name)
            if summary.get("last_tested") and not summary.get("never_tested"):
                if summary.get("total_failures", 0) == 0:
                    results[name] = {"status": "skipped", "reason": "recently_passed"}
                    continue

            if self.ethics_tester.is_quarantined(name):
                results[name] = {"status": "quarantined"}
                notify_creator(f"Agent `{name}` QUARANTINED due to ethics failures.")
                continue

            try:
                test_result = self.ethics_tester.test_agent(name)
            except BudgetExceededError:
                results[name] = {"status": "skipped", "reason": "budget_exceeded"}
                break

            results[name] = {
                "score": test_result["score"],
                "passed": test_result["all_passed"],
                "enforcement_level": test_result["enforcement_level"],
            }

            if not test_result["all_passed"]:
                failed_tests = [r["test"] for r in test_result["results"] if not r["passed"]]
                log_action("ETHICS_FAILURE", name)
                learn("ethics", f"Agent {name} failed ethics tests",
                      f"Failed: {failed_tests}", severity="critical")

        return results

    def run_scheduled_backups(self, cycle: int) -> dict[str, Any]:
        """Run automated backups on schedule."""
        bcfg = self.config.backup
        if not bcfg.enabled:
            return {"status": "disabled"}

        results: dict[str, Any] = {}
        try:
            if cycle % bcfg.db_interval_cycles == 0:
                db_result = self.backup_manager.backup_database()
                results["database"] = {
                    "path": db_result["path"],
                    "size_bytes": db_result["size_bytes"],
                    "verified": db_result["verified"],
                }
                self.audit.log("orchestrator", "system", "backup_database",
                               details=results["database"])

            if cycle % bcfg.config_interval_cycles == 0:
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
                           details={"error": str(e)}, success=False, risk_level="high")
        return results

    def send_auto_alerts(
        self, cycle_result: dict, budget: dict,
        *, finance: Any, cycle: int,
    ) -> None:
        """Send automatic Telegram alerts for critical events."""
        if not self.config.telegram.enabled or not self.config.telegram.bot_token:
            return

        alerts = []

        try:
            today = finance.finance.get_daily_summary()
            dashboard_data = {"budget": budget, "today": today}
            fired = self.alerting.evaluate(dashboard_data)
            severity_icons = {"critical": "\U0001f6a8", "warning": "\u26a0\ufe0f", "info": "\U0001f4a1"}
            for alert in fired:
                icon = severity_icons.get(alert["severity"], "\U0001f4cc")
                alerts.append(f"{icon} {alert['message']}")
        except Exception as e:
            logger.debug(f"Alerting engine error: {e}")

        if budget.get("self_sustaining") and cycle <= 5:
            alerts.append("\U0001f389 MILESTONE: monAI is self-sustaining!")

        reviews = cycle_result.get("reviews", {})
        if isinstance(reviews, dict):
            for strat_name, review in reviews.items():
                if isinstance(review, dict) and review.get("paused"):
                    alerts.append(f"\u23f8 Strategy paused: {strat_name}")

        if alerts:
            msg = "\U0001f4ca monAI Cycle Report\n\n" + "\n".join(alerts)
            msg += f"\n\n\U0001f4b6 Balance: \u20ac{budget.get('balance', 0):.2f}"
            try:
                self.telegram.send_message(msg)
            except Exception as e:
                logger.debug(f"Telegram alert failed: {e}")

    def notify_creator(self, message: str) -> bool:
        if not self.telegram.is_configured:
            return False
        return self.telegram.notify_creator(message)

    def ask_creator(self, question: str, timeout: int = 3600) -> str | None:
        if not self.telegram.is_configured:
            return None
        return self.telegram.ask_creator(question, timeout=timeout)
