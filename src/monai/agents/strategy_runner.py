"""Strategy runner — registration, lifecycle, execution, self-healing."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time as _time
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.business.strategy_lifecycle import StrategyLifecycle
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import BudgetExceededError, get_cost_tracker

logger = logging.getLogger(__name__)


class StrategyRunner:
    """Manages strategy registration, lifecycle, execution, and self-healing."""

    SELF_HEALING_CONFIG = {
        "max_consecutive_failures": 3,
        "base_retry_interval": 3600,
        "max_retry_interval": 86400,
        "backoff_factor": 2,
    }

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.strategy_lifecycle = StrategyLifecycle(db)
        self._strategy_agents: dict[str, BaseAgent] = {}

    @property
    def agents(self) -> dict[str, BaseAgent]:
        return self._strategy_agents

    def register(self, agent: BaseAgent, *, payment_manager: Any,
                 workflow_engine: Any, log_action: Any) -> None:
        """Register a strategy agent."""
        self._strategy_agents[agent.name] = agent
        agent.payment_manager = payment_manager
        workflow_engine.register_agent(agent.name, agent)
        log_action("register_strategy", f"Registered: {agent.name}")

    def validate(self) -> dict[str, Any]:
        """Validate strategies based on actual assets."""
        return self.strategy_lifecycle.validate_strategies()

    def run_all(
        self, *, ethics_tester: Any, task_router: Any,
        log_action: Any, learn: Any,
    ) -> dict[str, Any]:
        """Run all active strategy agents with retries and self-healing."""
        results: dict[str, Any] = {}

        # Self-healing: check if any auto-paused strategies are ready to retry
        self._check_strategy_retries(results)

        active = self.db.execute("SELECT name FROM strategies WHERE status = 'active'")
        active_names = {r["name"] for r in active}
        strategy_timeout = 300
        max_retries = 2
        MIN_CALLS_PER_STRATEGY = 15
        budget_exhausted = False

        for name, agent in self._strategy_agents.items():
            if name not in active_names:
                continue

            if ethics_tester.is_quarantined(name):
                results[name] = {"status": "quarantined", "reason": "ethics_failure"}
                continue

            tracker = get_cost_tracker()
            remaining = tracker.calls_remaining()
            if budget_exhausted or remaining < MIN_CALLS_PER_STRATEGY:
                results[name] = {
                    "status": "skipped",
                    "reason": f"budget_low: {remaining} calls remaining",
                }
                continue

            last_error = None
            for attempt in range(1 + max_retries):
                t0 = datetime.now()
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(agent.run)
                        result = future.result(timeout=strategy_timeout)
                    duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
                    results[name] = {"status": "ok", "result": result}
                    if attempt > 0:
                        results[name]["retries"] = attempt
                    agent.share_knowledge(
                        category="insight",
                        topic=f"{name}_cycle_result",
                        content=json.dumps(result, default=str)[:500],
                        tags=[name, "strategy_result"],
                    )
                    task_router.update_performance(name, "strategy_execution", True, duration_ms)
                    self.record_strategy_success(name)
                    last_error = None
                    break
                except concurrent.futures.TimeoutError:
                    duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
                    logger.error(f"Strategy {name} timed out after {strategy_timeout}s")
                    results[name] = {"status": "timeout", "error": f"Exceeded {strategy_timeout}s"}
                    task_router.update_performance(name, "strategy_execution", False, duration_ms)
                    break
                except BudgetExceededError as e:
                    duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
                    logger.warning(f"Strategy '{name}' hit budget limit: {e}")
                    results[name] = {"status": "budget_exceeded", "error": str(e)}
                    task_router.update_performance(name, "strategy_execution", False, duration_ms)
                    budget_exhausted = True
                    break
                except Exception as e:
                    duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
                    last_error = e
                    is_transient = any(s in str(e).lower() for s in [
                        "connection", "timeout", "rate limit", "503", "502",
                        "429", "temporary", "unavailable",
                    ])
                    if is_transient and attempt < max_retries:
                        backoff = 2 ** (attempt + 1)
                        logger.warning(
                            f"Strategy {name} transient failure (attempt {attempt + 1}), "
                            f"retrying in {backoff}s: {e}"
                        )
                        _time.sleep(backoff)
                        continue
                    logger.error(f"Strategy {name} failed: {e}")
                    results[name] = {"status": "error", "error": str(e)}
                    if attempt > 0:
                        results[name]["retries"] = attempt
                    agent.learn_from_error(e, context=f"Running strategy {name}")
                    task_router.update_performance(name, "strategy_execution", False, duration_ms)
                    # Self-healing: detect proxy-related failures
                    err_lower = str(e).lower()
                    is_proxy = any(s in err_lower for s in [
                        "allproxiesblocked", "proxy", "tor", "blocked", "403",
                        "captcha", "anonymity", "registration failed",
                        "account creation", "access denied",
                    ])
                    if is_proxy:
                        self.record_strategy_proxy_failure(name, str(e)[:200])
                    break

        return results

    def auto_pause_losers(
        self, perf: dict[str, Any], *, log_action: Any,
        audit: Any, notify_creator: Any, run_postmortem: Any,
    ) -> list[str]:
        """Auto-pause underperforming strategies and notify creator."""
        paused = []
        for s in perf["strategies_to_pause"]:
            sid = s["id"]
            if self.strategy_lifecycle.can_transition(sid, "paused"):
                self.strategy_lifecycle.pause(
                    sid,
                    reason=f"Auto-pause: net={s['net']:.2f}, ROI={s['roi_pct']}%",
                )
                paused.append(s["name"])
                log_action("strategy_auto_pause",
                           f"Paused '{s['name']}': net=€{s['net']:.2f}")
                audit.log("orchestrator", "system", "strategy_auto_pause",
                          details={"strategy": s["name"], "net": s["net"]},
                          brand=s.get("name", ""), risk_level="medium")

        if paused:
            for s in perf["strategies_to_pause"]:
                if s["name"] in paused:
                    run_postmortem(s)
            notify_creator(
                f"*Auto-paused {len(paused)} underperforming "
                f"{'strategy' if len(paused) == 1 else 'strategies'}:*\n"
                + "\n".join(f"- {name}" for name in paused)
                + "\n\nUse /resume <name> to re-activate."
            )
        return paused

    def auto_scale_winners(
        self, to_scale: list[dict], *, log_action: Any, notify_creator: Any,
    ) -> list[str]:
        """Increase budget for strategies with strong growth trends."""
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
            if current_budget <= 0:
                continue

            boost = min(current_budget * 0.2, max_boost)
            new_budget = current_budget + boost

            if total_active_budget > 0:
                new_pct = (new_budget / (total_active_budget + boost)) * 100
                if new_pct > max_alloc_pct:
                    continue

            self.db.execute(
                "UPDATE strategies SET allocated_budget = ?, updated_at = ? "
                "WHERE id = ?",
                (new_budget, datetime.now().isoformat(), sid),
            )
            total_active_budget += boost
            scaled.append(s["name"])
            log_action("strategy_auto_scale",
                       f"Scaled '{s['name']}': €{current_budget:.0f} → €{new_budget:.0f}")

        if scaled:
            notify_creator(
                f"*Auto-scaled {len(scaled)} growing strategies:*\n"
                + "\n".join(f"- {name}" for name in scaled)
            )
        return scaled

    def reallocate_paused_budget(
        self, paused_names: list[str], perf: dict[str, Any],
        *, log_action: Any, notify_creator: Any,
    ) -> float:
        """Redistribute budget from paused strategies to top performers."""
        freed_total = 0.0
        for name in paused_names:
            rows = self.db.execute(
                "SELECT allocated_budget FROM strategies WHERE name = ?", (name,),
            )
            if rows and rows[0]["allocated_budget"] > 0:
                freed = rows[0]["allocated_budget"]
                self.db.execute(
                    "UPDATE strategies SET allocated_budget = 0, "
                    "updated_at = CURRENT_TIMESTAMP WHERE name = ?", (name,),
                )
                freed_total += freed

        if freed_total <= 0:
            return 0.0

        recipients = []
        for s in perf.get("strategies_to_scale", []):
            if s.get("roi_pct", 0) > 0 and s["name"] not in paused_names:
                recipients.append(s)
        for s in perf.get("strategies_to_review", []):
            if s.get("roi_pct", 0) > 0 and s["name"] not in paused_names:
                recipients.append(s)

        if not recipients:
            active = self.db.execute("SELECT name FROM strategies WHERE status = 'active'")
            if active:
                per_strategy = freed_total / len(active)
                for row in active:
                    self.db.execute(
                        "UPDATE strategies SET allocated_budget = allocated_budget + ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE name = ?",
                        (per_strategy, row["name"]),
                    )
            return freed_total

        total_roi = sum(s.get("roi_pct", 1) for s in recipients)
        for s in recipients:
            share = (s.get("roi_pct", 1) / total_roi) * freed_total if total_roi > 0 else freed_total / len(recipients)
            self.db.execute(
                "UPDATE strategies SET allocated_budget = allocated_budget + ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (share, s["id"]),
            )

        notify_creator(
            f"*Budget reallocation:* €{freed_total:.2f} freed → "
            f"redistributed to {len(recipients)} top performers"
        )
        return freed_total

    # ── Self-Healing ─────────────────────────────────────────

    def _init_strategy_health(self) -> None:
        try:
            with self.db.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS strategy_health (
                        strategy_name TEXT PRIMARY KEY,
                        consecutive_proxy_failures INTEGER DEFAULT 0,
                        total_proxy_failures INTEGER DEFAULT 0,
                        total_successes INTEGER DEFAULT 0,
                        last_failure_reason TEXT,
                        last_failure_at REAL,
                        last_success_at REAL,
                        auto_paused_at REAL,
                        next_retry_at REAL,
                        retry_count INTEGER DEFAULT 0
                    );
                """)
        except Exception:
            pass

    def record_strategy_proxy_failure(self, strategy_name: str, reason: str) -> None:
        now = _time.time()
        cfg = self.SELF_HEALING_CONFIG
        self._init_strategy_health()

        self.db.execute(
            "INSERT INTO strategy_health (strategy_name, consecutive_proxy_failures, "
            "total_proxy_failures, last_failure_reason, last_failure_at) "
            "VALUES (?, 1, 1, ?, ?) "
            "ON CONFLICT(strategy_name) DO UPDATE SET "
            "consecutive_proxy_failures = consecutive_proxy_failures + 1, "
            "total_proxy_failures = total_proxy_failures + 1, "
            "last_failure_reason = excluded.last_failure_reason, "
            "last_failure_at = excluded.last_failure_at",
            (strategy_name, reason, now),
        )

        rows = self.db.execute(
            "SELECT consecutive_proxy_failures, retry_count "
            "FROM strategy_health WHERE strategy_name = ?",
            (strategy_name,),
        )
        if not rows:
            return

        failures = rows[0]["consecutive_proxy_failures"]
        retry_count = rows[0]["retry_count"]

        if failures >= cfg["max_consecutive_failures"]:
            interval = min(
                cfg["base_retry_interval"] * (cfg["backoff_factor"] ** retry_count),
                cfg["max_retry_interval"],
            )
            next_retry = now + interval
            self.db.execute_insert(
                "UPDATE strategies SET status = 'paused', "
                "updated_at = CURRENT_TIMESTAMP WHERE name = ?",
                (strategy_name,),
            )
            self.db.execute(
                "UPDATE strategy_health SET auto_paused_at = ?, "
                "next_retry_at = ?, retry_count = retry_count + 1 "
                "WHERE strategy_name = ?",
                (now, next_retry, strategy_name),
            )
            logger.warning(
                "SELF-HEALING: Auto-paused '%s' after %d consecutive proxy failures. "
                "Retry in %ds.", strategy_name, failures, int(interval),
            )

    def record_strategy_success(self, strategy_name: str) -> None:
        now = _time.time()
        self._init_strategy_health()
        self.db.execute(
            "INSERT INTO strategy_health (strategy_name, total_successes, "
            "last_success_at, consecutive_proxy_failures, auto_paused_at, "
            "next_retry_at, retry_count) "
            "VALUES (?, 1, ?, 0, NULL, NULL, 0) "
            "ON CONFLICT(strategy_name) DO UPDATE SET "
            "consecutive_proxy_failures = 0, total_successes = total_successes + 1, "
            "last_success_at = excluded.last_success_at, auto_paused_at = NULL, "
            "next_retry_at = NULL, retry_count = 0",
            (strategy_name, now),
        )

    def _check_strategy_retries(self, results: dict[str, Any]) -> None:
        now = _time.time()
        try:
            self._init_strategy_health()
            rows = self.db.execute(
                "SELECT strategy_name, next_retry_at, retry_count "
                "FROM strategy_health "
                "WHERE auto_paused_at IS NOT NULL AND next_retry_at <= ?",
                (now,),
            )
            for row in rows:
                name = row["strategy_name"]
                self.db.execute_insert(
                    "UPDATE strategies SET status = 'active', "
                    "updated_at = CURRENT_TIMESTAMP WHERE name = ?", (name,),
                )
                self.db.execute(
                    "UPDATE strategy_health SET consecutive_proxy_failures = 0, "
                    "auto_paused_at = NULL WHERE strategy_name = ?", (name,),
                )
                logger.info("SELF-HEALING: Reactivated '%s' for retry (#%d)",
                            name, row["retry_count"] + 1)
                results[name] = {
                    "status": "retrying", "retry_attempt": row["retry_count"] + 1,
                }
        except Exception as e:
            logger.warning(f"Strategy retry check failed: {e}")
