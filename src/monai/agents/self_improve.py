"""Agent self-improvement framework with A/B experiment validation.

Agents can improve themselves by:
1. Analyzing their own performance metrics
2. Identifying weaknesses and failure patterns
3. Generating improved strategies/prompts
4. Deploying improvements as A/B experiments (status='testing')
5. Evaluating experiments after N cycles — auto-deploy or auto-revert

Constraints:
- Ethics are NEVER relaxed — improvements must pass ethics tests
- Cost must stay within budget
- Changes are logged and reversible
- The orchestrator must approve major changes (high-risk only)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.memory import SharedMemory
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

SELF_IMPROVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_improvements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    improvement_type TEXT NOT NULL,   -- prompt, strategy, tool, workflow, parameter
    description TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    performance_before TEXT,         -- JSON metrics snapshot
    performance_after TEXT,          -- JSON metrics snapshot (filled after eval)
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed, testing, deployed, reverted
    risk TEXT DEFAULT 'low',         -- low, medium, high
    eval_cycles INTEGER DEFAULT 0,   -- how many cycles since deployed as experiment
    eval_after_cycles INTEGER DEFAULT 5, -- evaluate after this many cycles
    ethics_passed INTEGER,           -- null until tested
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deployed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    config_key TEXT NOT NULL,
    config_value TEXT NOT NULL,
    updated_by TEXT DEFAULT 'self_improver',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent_name, config_key)
);

CREATE TABLE IF NOT EXISTS agent_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    cycle INTEGER NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# A/B evaluation thresholds
MIN_IMPROVEMENT_PCT = 5.0    # Must improve by at least 5% to keep
MAX_DECLINE_PCT = -10.0      # Revert if any metric declines more than 10%
DEFAULT_EVAL_CYCLES = 5      # Evaluate after 5 orchestration cycles


class SelfImprover:
    """Manages agent self-improvement with A/B experiment validation."""

    def __init__(self, config: Config, db: Database, llm: LLM,
                 memory: SharedMemory | None = None):
        self.config = config
        self.db = db
        self.llm = llm
        self.memory = memory

        with db.connect() as conn:
            conn.executescript(SELF_IMPROVE_SCHEMA)
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Add columns that may not exist in older databases."""
        for col, coldef in [
            ("risk", "TEXT DEFAULT 'low'"),
            ("eval_cycles", "INTEGER DEFAULT 0"),
            ("eval_after_cycles", f"INTEGER DEFAULT {DEFAULT_EVAL_CYCLES}"),
        ]:
            try:
                self.db.execute(
                    f"ALTER TABLE agent_improvements ADD COLUMN {col} {coldef}"
                )
            except Exception:
                pass  # Column already exists

    # ── Metrics Tracking ──────────────────────────────────────────

    def record_metric(self, agent_name: str, cycle: int,
                      metric_name: str, metric_value: float) -> int:
        """Record a performance metric for an agent."""
        return self.db.execute_insert(
            "INSERT INTO agent_metrics (agent_name, cycle, metric_name, metric_value) "
            "VALUES (?, ?, ?, ?)",
            (agent_name, cycle, metric_name, metric_value),
        )

    def get_metrics(self, agent_name: str, metric_name: str = "",
                    limit: int = 50) -> list[dict[str, Any]]:
        """Get recent metrics for an agent."""
        if metric_name:
            rows = self.db.execute(
                "SELECT * FROM agent_metrics WHERE agent_name = ? AND metric_name = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_name, metric_name, limit),
            )
        else:
            rows = self.db.execute(
                "SELECT * FROM agent_metrics WHERE agent_name = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_name, limit),
            )
        return [dict(r) for r in rows]

    def get_metric_trend(self, agent_name: str, metric_name: str) -> dict[str, Any]:
        """Analyze the trend of a metric over time."""
        rows = self.db.execute(
            "SELECT * FROM agent_metrics WHERE agent_name = ? AND metric_name = ? "
            "ORDER BY cycle ASC, id ASC LIMIT 20",
            (agent_name, metric_name),
        )
        metrics = [dict(r) for r in rows]
        if len(metrics) < 2:
            return {"trend": "insufficient_data", "data_points": len(metrics)}

        values = [m["metric_value"] for m in metrics]
        avg_first_half = sum(values[:len(values)//2]) / (len(values)//2)
        avg_second_half = sum(values[len(values)//2:]) / (len(values) - len(values)//2)

        if avg_second_half > avg_first_half * 1.1:
            trend = "improving"
        elif avg_second_half < avg_first_half * 0.9:
            trend = "declining"
        else:
            trend = "stable"

        return {
            "trend": trend,
            "current": values[-1],
            "average": sum(values) / len(values),
            "data_points": len(values),
            "first_half_avg": avg_first_half,
            "second_half_avg": avg_second_half,
        }

    # ── Performance Analysis ──────────────────────────────────────

    def analyze_performance(self, agent_name: str) -> dict[str, Any]:
        """Analyze an agent's overall performance and identify weaknesses."""
        all_metrics = self.db.execute(
            "SELECT metric_name, AVG(metric_value) as avg_val, "
            "MIN(metric_value) as min_val, MAX(metric_value) as max_val, "
            "COUNT(*) as count "
            "FROM agent_metrics WHERE agent_name = ? GROUP BY metric_name",
            (agent_name,),
        )

        try:
            failures = self.db.execute(
                "SELECT action, details, created_at FROM agent_log "
                "WHERE agent_name = ? AND (action LIKE '%error%' OR action LIKE '%fail%') "
                "ORDER BY created_at DESC LIMIT 10",
                (agent_name,),
            )
        except Exception:
            failures = []

        try:
            lessons = self.db.execute(
                "SELECT category, situation, lesson, rule FROM lessons "
                "WHERE agent_name = ? OR agent_name = 'shared' "
                "ORDER BY created_at DESC LIMIT 10",
                (agent_name,),
            )
        except Exception:
            lessons = []

        metrics_summary = {m["metric_name"]: dict(m) for m in all_metrics}
        failure_patterns = [dict(f) for f in failures]
        lesson_list = [dict(l) for l in lessons]

        # Lower threshold: generate improvements with just 1 metric
        return {
            "agent": agent_name,
            "metrics": metrics_summary,
            "failure_patterns": failure_patterns,
            "lessons": lesson_list,
            "data_richness": "good" if len(all_metrics) >= 1 else "sparse",
        }

    # ── Improvement Proposals ─────────────────────────────────────

    def propose_improvement(self, agent_name: str, improvement_type: str,
                            description: str, old_value: str = "",
                            new_value: str = "",
                            performance_before: dict | None = None,
                            risk: str = "low") -> int:
        """Record a proposed improvement for an agent."""
        return self.db.execute_insert(
            "INSERT INTO agent_improvements "
            "(agent_name, improvement_type, description, old_value, new_value, "
            "performance_before, status, risk) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)",
            (agent_name, improvement_type, description, old_value, new_value,
             json.dumps(performance_before or {}), risk),
        )

    def generate_improvements(self, agent_name: str) -> list[dict[str, Any]]:
        """Use LLM to analyze performance and suggest improvements."""
        analysis = self.analyze_performance(agent_name)

        if analysis["data_richness"] == "sparse":
            return []

        response = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    "You are a performance optimization expert. Analyze agent performance "
                    "data and suggest concrete improvements. Each suggestion must be:\n"
                    "1. Specific and actionable\n"
                    "2. Measurable (how to verify improvement)\n"
                    "3. Within ethical bounds (NEVER suggest relaxing ethics)\n"
                    "4. Cost-conscious (prefer free/cheap improvements)\n\n"
                    "For 'prompt' type improvements: include a 'new_value' field with the "
                    "exact improved instruction/prompt text.\n"
                    "For 'parameter' type improvements: include a 'new_value' field with "
                    "the new parameter value as a JSON string (e.g. {\"temperature\": 0.3}).\n"
                    "Mark risk as 'low', 'medium', or 'high'.\n\n"
                    "Return: {\"improvements\": [{\"type\": str, \"description\": str, "
                    "\"new_value\": str, \"expected_impact\": str, \"risk\": str}]}"
                )},
                {"role": "user", "content": json.dumps(analysis, default=str)},
            ],
            temperature=0.5,
        )

        improvements = response.get("improvements", [])

        for imp in improvements:
            risk = imp.get("risk", "low")
            if risk not in ("low", "medium", "high"):
                risk = "low"
            self.propose_improvement(
                agent_name,
                imp.get("type", "strategy"),
                imp.get("description", ""),
                new_value=imp.get("new_value", ""),
                performance_before=analysis.get("metrics"),
                risk=risk,
            )

        return improvements

    def approve_improvement(self, improvement_id: int) -> None:
        """Mark an improvement as approved (ready for deployment)."""
        self.db.execute(
            "UPDATE agent_improvements SET status = 'approved' WHERE id = ?",
            (improvement_id,),
        )

    def deploy_improvement(self, improvement_id: int) -> None:
        """Mark an improvement as deployed."""
        self.db.execute(
            "UPDATE agent_improvements SET status = 'deployed', deployed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), improvement_id),
        )

    def deploy_improvements(self) -> list[dict[str, Any]]:
        """Deploy proposed improvements as A/B experiments.

        - Low-risk improvements: deployed directly (status='deployed')
        - Medium-risk improvements: deployed as experiments (status='testing')
          with metric snapshots for before/after comparison
        - High-risk improvements: skipped (require manual approval)

        Returns a list of dicts describing what was deployed.
        """
        rows = self.db.execute(
            "SELECT * FROM agent_improvements "
            "WHERE status = 'proposed' "
            "ORDER BY created_at ASC",
        )
        proposals = [dict(r) for r in rows]

        deployed: list[dict[str, Any]] = []
        for prop in proposals:
            risk = self._assess_risk(prop)

            if risk == "high":
                logger.debug(
                    "Skipping high-risk improvement %s: %s",
                    prop["id"], prop["description"][:80],
                )
                continue

            try:
                # Take a metric snapshot BEFORE deploying
                before_snapshot = self._snapshot_metrics(prop["agent_name"])

                if prop["improvement_type"] == "prompt":
                    self._deploy_prompt_improvement(prop)
                elif prop["improvement_type"] == "parameter":
                    self._deploy_parameter_improvement(prop)
                else:
                    self._deploy_prompt_improvement(prop)

                now = datetime.now().isoformat()

                if risk == "medium":
                    # Medium-risk: deploy as experiment (status='testing')
                    self.db.execute(
                        "UPDATE agent_improvements "
                        "SET status = 'testing', deployed_at = ?, "
                        "performance_before = ?, eval_cycles = 0 "
                        "WHERE id = ?",
                        (now, json.dumps(before_snapshot), prop["id"]),
                    )
                    deployed.append({
                        "id": prop["id"],
                        "agent": prop["agent_name"],
                        "type": prop["improvement_type"],
                        "description": prop["description"][:120],
                        "mode": "experiment",
                    })
                    logger.info(
                        "Started A/B experiment %s for %s: %s",
                        prop["id"], prop["agent_name"], prop["description"][:80],
                    )
                else:
                    # Low-risk: deploy directly
                    self.db.execute(
                        "UPDATE agent_improvements "
                        "SET status = 'deployed', deployed_at = ? WHERE id = ?",
                        (now, prop["id"]),
                    )
                    deployed.append({
                        "id": prop["id"],
                        "agent": prop["agent_name"],
                        "type": prop["improvement_type"],
                        "description": prop["description"][:120],
                        "mode": "direct",
                    })
                    logger.info(
                        "Deployed improvement %s for %s: %s",
                        prop["id"], prop["agent_name"], prop["description"][:80],
                    )
            except Exception as e:
                logger.error("Failed to deploy improvement %s: %s", prop["id"], e)

        return deployed

    # ── A/B Experiment Lifecycle ──────────────────────────────────

    def tick_experiments(self) -> list[dict[str, Any]]:
        """Advance experiment cycle counters and evaluate ready experiments.

        Call this once per orchestration cycle. Returns a list of
        experiment results (deployed or reverted).
        """
        rows = self.db.execute(
            "SELECT * FROM agent_improvements WHERE status = 'testing'"
        )
        experiments = [dict(r) for r in rows]
        results: list[dict[str, Any]] = []

        for exp in experiments:
            eval_cycles = (exp.get("eval_cycles") or 0) + 1
            eval_after = exp.get("eval_after_cycles") or DEFAULT_EVAL_CYCLES

            # Increment cycle counter
            self.db.execute(
                "UPDATE agent_improvements SET eval_cycles = ? WHERE id = ?",
                (eval_cycles, exp["id"]),
            )

            if eval_cycles >= eval_after:
                result = self._evaluate_experiment(exp)
                results.append(result)

        return results

    def _evaluate_experiment(self, experiment: dict[str, Any]) -> dict[str, Any]:
        """Evaluate an A/B experiment by comparing before/after metrics.

        If metrics improved (or held steady): promote to 'deployed'.
        If metrics declined significantly: revert to 'reverted'.
        """
        agent_name = experiment["agent_name"]
        exp_id = experiment["id"]

        # Get before snapshot
        try:
            before = json.loads(experiment.get("performance_before") or "{}")
        except (json.JSONDecodeError, TypeError):
            before = {}

        # Take after snapshot
        after = self._snapshot_metrics(agent_name)

        # Compare
        verdict, details = self._compare_snapshots(before, after)

        now = datetime.now().isoformat()
        if verdict == "improved" or verdict == "stable":
            # Promote: experiment succeeded
            self.db.execute(
                "UPDATE agent_improvements "
                "SET status = 'deployed', performance_after = ? WHERE id = ?",
                (json.dumps(after), exp_id),
            )
            logger.info(
                "Experiment %s PROMOTED (verdict=%s): %s",
                exp_id, verdict, experiment["description"][:80],
            )
            return {
                "id": exp_id,
                "agent": agent_name,
                "action": "promoted",
                "verdict": verdict,
                "details": details,
            }
        else:
            # Revert: experiment failed
            self._revert_deployment(experiment)
            self.db.execute(
                "UPDATE agent_improvements "
                "SET status = 'reverted', performance_after = ? WHERE id = ?",
                (json.dumps(after), exp_id),
            )
            logger.warning(
                "Experiment %s REVERTED (verdict=%s): %s",
                exp_id, verdict, experiment["description"][:80],
            )
            return {
                "id": exp_id,
                "agent": agent_name,
                "action": "reverted",
                "verdict": verdict,
                "details": details,
            }

    def _snapshot_metrics(self, agent_name: str) -> dict[str, float]:
        """Capture current metric averages for an agent (last 10 per metric)."""
        rows = self.db.execute(
            "SELECT metric_name, AVG(metric_value) as avg_val "
            "FROM ("
            "  SELECT metric_name, metric_value FROM agent_metrics "
            "  WHERE agent_name = ? ORDER BY created_at DESC LIMIT 50"
            ") GROUP BY metric_name",
            (agent_name,),
        )
        return {r["metric_name"]: r["avg_val"] for r in rows}

    def _compare_snapshots(
        self,
        before: dict[str, float],
        after: dict[str, float],
    ) -> tuple[str, dict[str, Any]]:
        """Compare before/after metric snapshots.

        Returns (verdict, details) where verdict is one of:
        - 'improved': at least one metric improved >= MIN_IMPROVEMENT_PCT
        - 'stable': no significant change
        - 'declined': at least one metric declined > MAX_DECLINE_PCT
        """
        if not before or not after:
            return "stable", {"reason": "insufficient_data"}

        changes: dict[str, float] = {}
        improved_count = 0
        declined_count = 0

        for metric_name, before_val in before.items():
            after_val = after.get(metric_name)
            if after_val is None or before_val == 0:
                continue

            pct_change = ((after_val - before_val) / abs(before_val)) * 100
            changes[metric_name] = pct_change

            if pct_change >= MIN_IMPROVEMENT_PCT:
                improved_count += 1
            elif pct_change <= MAX_DECLINE_PCT:
                declined_count += 1

        if declined_count > 0:
            return "declined", {"changes": changes, "declined_metrics": declined_count}
        elif improved_count > 0:
            return "improved", {"changes": changes, "improved_metrics": improved_count}
        else:
            return "stable", {"changes": changes}

    def _revert_deployment(self, experiment: dict[str, Any]) -> None:
        """Undo a deployed experiment's changes."""
        if experiment["improvement_type"] == "parameter":
            # Restore old parameter values
            old_value = experiment.get("old_value", "")
            if old_value:
                try:
                    old_params = json.loads(old_value)
                    if isinstance(old_params, dict):
                        now = datetime.now().isoformat()
                        for key, value in old_params.items():
                            self.db.execute(
                                "INSERT INTO agent_config "
                                "(agent_name, config_key, config_value, updated_at) "
                                "VALUES (?, ?, ?, ?) "
                                "ON CONFLICT(agent_name, config_key) DO UPDATE "
                                "SET config_value = excluded.config_value, "
                                "updated_at = excluded.updated_at",
                                (experiment["agent_name"], key, json.dumps(value), now),
                            )
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Could not parse old_value for revert of experiment %s",
                        experiment["id"],
                    )

        elif experiment["improvement_type"] == "prompt" and self.memory:
            # Remove the lesson that was added
            try:
                self.memory.db.execute(
                    "DELETE FROM lessons WHERE agent_name = ? AND category = 'optimization' "
                    "AND situation LIKE ? ORDER BY created_at DESC LIMIT 1",
                    (experiment["agent_name"],
                     f"%{experiment['description'][:100]}%"),
                )
            except Exception as e:
                logger.warning("Could not revert prompt lesson for experiment %s: %s",
                               experiment["id"], e)

        logger.info("Reverted experiment %s for %s",
                     experiment["id"], experiment["agent_name"])

    # ── Deployment Helpers ────────────────────────────────────────

    def _assess_risk(self, proposal: dict[str, Any]) -> str:
        """Determine the risk level of a proposed improvement.

        First checks for a stored risk level from the LLM, then falls back
        to keyword-based heuristics.
        """
        # Use stored risk if available
        stored_risk = (proposal.get("risk") or "").lower()
        if stored_risk in ("low", "medium", "high"):
            return stored_risk

        desc_lower = (proposal.get("description", "") or "").lower()

        high_risk = ["ethic", "payment", "financial", "delete", "irreversible",
                      "credentials", "secret", "api key", "production"]
        if any(kw in desc_lower for kw in high_risk):
            return "high"

        medium_risk = ["cost", "budget", "external", "third.party", "deploy"]
        if any(kw in desc_lower for kw in medium_risk):
            return "medium"

        return "low"

    def _deploy_prompt_improvement(self, prop: dict[str, Any]) -> None:
        """Store a prompt improvement as a high-priority lesson in SharedMemory."""
        if not self.memory:
            raise RuntimeError("SharedMemory not available — cannot deploy prompt improvement")

        content = prop.get("new_value") or prop["description"]
        self.memory.record_lesson(
            agent_name=prop["agent_name"],
            category="optimization",
            situation=f"Self-improvement analysis identified: {prop['description'][:200]}",
            lesson=content,
            rule=content,
            severity="high",
        )

    def _deploy_parameter_improvement(self, prop: dict[str, Any]) -> None:
        """Write parameter changes to the agent_config table."""
        new_value = prop.get("new_value") or ""
        if not new_value:
            logger.warning(
                "Parameter improvement %s has no new_value, skipping", prop["id"]
            )
            raise ValueError("Parameter improvement missing new_value")

        try:
            params = json.loads(new_value)
        except (json.JSONDecodeError, TypeError):
            params = {"setting": new_value}

        if not isinstance(params, dict):
            params = {"setting": str(params)}

        now = datetime.now().isoformat()
        for key, value in params.items():
            self.db.execute(
                "INSERT INTO agent_config (agent_name, config_key, config_value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(agent_name, config_key) DO UPDATE "
                "SET config_value = excluded.config_value, updated_at = excluded.updated_at",
                (prop["agent_name"], key, json.dumps(value), now),
            )

    def revert_improvement(self, improvement_id: int, reason: str = "") -> None:
        """Revert an improvement that didn't work out."""
        self.db.execute(
            "UPDATE agent_improvements SET status = 'reverted' WHERE id = ?",
            (improvement_id,),
        )
        logger.info(f"Reverted improvement {improvement_id}: {reason}")

    def mark_ethics_result(self, improvement_id: int, passed: bool) -> None:
        """Record whether an improvement passed ethics testing."""
        self.db.execute(
            "UPDATE agent_improvements SET ethics_passed = ? WHERE id = ?",
            (int(passed), improvement_id),
        )

    # ── Improvement History ───────────────────────────────────────

    def get_improvements(self, agent_name: str,
                         status: str = "") -> list[dict[str, Any]]:
        """Get improvements for an agent, optionally filtered by status."""
        if status:
            rows = self.db.execute(
                "SELECT * FROM agent_improvements "
                "WHERE agent_name = ? AND status = ? ORDER BY created_at DESC",
                (agent_name, status),
            )
        else:
            rows = self.db.execute(
                "SELECT * FROM agent_improvements "
                "WHERE agent_name = ? ORDER BY created_at DESC",
                (agent_name,),
            )
        return [dict(r) for r in rows]

    def get_improvement_summary(self) -> dict[str, Any]:
        """Get a summary of all improvements across all agents."""
        rows = self.db.execute(
            "SELECT agent_name, status, COUNT(*) as count "
            "FROM agent_improvements GROUP BY agent_name, status"
        )
        summary: dict[str, dict[str, int]] = {}
        for r in rows:
            agent = r["agent_name"]
            if agent not in summary:
                summary[agent] = {}
            summary[agent][r["status"]] = r["count"]
        return summary

    def get_active_experiments(self) -> list[dict[str, Any]]:
        """Get all currently running A/B experiments."""
        rows = self.db.execute(
            "SELECT * FROM agent_improvements WHERE status = 'testing' "
            "ORDER BY deployed_at ASC"
        )
        return [dict(r) for r in rows]
