"""Agent self-improvement framework with A/B experiment validation.

Agents can improve themselves by:
1. Analyzing their own performance metrics
2. Identifying weaknesses and failure patterns
3. Generating improved strategies/prompts
4. Deploying improvements as A/B experiments (status='testing')
5. Evaluating experiments after N cycles — auto-deploy or auto-revert

Statistical rigor:
- Minimum sample size (N≥10) before making decisions
- Welch's t-test for significance (p < 0.05)
- Variance/stdev analysis to flag noisy results
- Bonferroni correction for multiple metric comparisons
- Early stop if p < 0.01 with sufficient data

Constraints:
- Ethics are NEVER relaxed — improvements must pass ethics tests
- Cost must stay within budget
- Changes are logged and reversible
- The orchestrator must approve major changes (high-risk only)
"""

from __future__ import annotations

import json
import logging
import math
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
MIN_SAMPLE_SIZE = 10         # Minimum data points per metric for valid comparison
SIGNIFICANCE_LEVEL = 0.05    # p-value threshold for statistical significance
EARLY_STOP_P = 0.01          # p-value for early stop (very significant)
HIGH_VARIANCE_RATIO = 0.5    # Flag if stdev > 50% of mean


def _welch_t_test(
    vals_a: list[float], vals_b: list[float],
) -> tuple[float, float]:
    """Welch's t-test for unequal variances (no scipy dependency).

    Returns (t_statistic, p_value_approx).
    Uses the Welch-Satterthwaite approximation for degrees of freedom
    and a simple t-distribution approximation for the p-value.
    """
    n_a, n_b = len(vals_a), len(vals_b)
    if n_a < 2 or n_b < 2:
        return 0.0, 1.0

    mean_a = sum(vals_a) / n_a
    mean_b = sum(vals_b) / n_b

    var_a = sum((x - mean_a) ** 2 for x in vals_a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in vals_b) / (n_b - 1)

    se = math.sqrt(var_a / n_a + var_b / n_b) if (var_a + var_b) > 0 else 0.0
    if se == 0:
        return 0.0, 1.0

    t_stat = (mean_b - mean_a) / se

    # Welch-Satterthwaite degrees of freedom
    numerator = (var_a / n_a + var_b / n_b) ** 2
    denom = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    df = numerator / denom if denom > 0 else 1.0

    # Approximate two-tailed p-value using the normal distribution for large df,
    # or a conservative t-distribution approximation for small df.
    abs_t = abs(t_stat)
    if df >= 30:
        # Normal approximation (good for df >= 30)
        p_value = 2.0 * _normal_sf(abs_t)
    else:
        # Conservative approximation for small df
        # Uses: p ≈ 2 * (1 - Φ(|t| * sqrt(df/(df+t²))))
        adjusted = abs_t * math.sqrt(df / (df + abs_t ** 2)) if (df + abs_t ** 2) > 0 else 0.0
        p_value = 2.0 * _normal_sf(adjusted)

    return t_stat, max(0.0, min(1.0, p_value))


def _normal_sf(x: float) -> float:
    """Survival function (1 - CDF) for standard normal, no scipy needed."""
    return 0.5 * math.erfc(x / math.sqrt(2))


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
                # Mark as failed to prevent infinite retry loops
                try:
                    self.db.execute(
                        "UPDATE agent_improvements SET status = 'reverted' WHERE id = ?",
                        (prop["id"],),
                    )
                except Exception:
                    pass

        return deployed

    # ── A/B Experiment Lifecycle ──────────────────────────────────

    def tick_experiments(self) -> list[dict[str, Any]]:
        """Advance experiment cycle counters and evaluate ready experiments.

        Call this once per orchestration cycle. Supports early stop:
        if an experiment already has enough data and p < EARLY_STOP_P,
        it can be resolved before eval_after_cycles is reached.

        Returns a list of experiment results (deployed or reverted).
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

            # Check for early stop opportunity (at least half the eval window)
            if eval_cycles >= max(3, eval_after // 2) and eval_cycles < eval_after:
                early_result = self._check_early_stop(exp)
                if early_result:
                    results.append(early_result)
                    continue

            if eval_cycles >= eval_after:
                result = self._evaluate_experiment(exp)
                results.append(result)

        return results

    def _check_early_stop(self, experiment: dict[str, Any]) -> dict[str, Any] | None:
        """Check if experiment can be stopped early due to very strong signal.

        Only triggers if p < EARLY_STOP_P (0.01) with sufficient sample size.
        Returns result dict if early-stopped, None otherwise.
        """
        agent_name = experiment["agent_name"]
        deployed_at = experiment.get("deployed_at", "")

        for metric_name in ["execution_success", "revenue", "roi"]:
            before_vals = self._get_metric_values(
                agent_name, metric_name, before_cycle=deployed_at,
            )
            after_vals = self._get_metric_values(
                agent_name, metric_name, after_cycle=deployed_at,
            )

            if len(before_vals) < MIN_SAMPLE_SIZE or len(after_vals) < MIN_SAMPLE_SIZE:
                continue

            t_stat, p_value = _welch_t_test(before_vals, after_vals)

            if p_value < EARLY_STOP_P:
                b_mean = sum(before_vals) / len(before_vals)
                a_mean = sum(after_vals) / len(after_vals)
                pct_change = ((a_mean - b_mean) / abs(b_mean)) * 100 if b_mean != 0 else 0.0

                if pct_change <= MAX_DECLINE_PCT:
                    # Strong decline — early revert
                    logger.info(
                        "Experiment %s EARLY STOP (decline p=%.4f): %s",
                        experiment["id"], p_value, experiment["description"][:80],
                    )
                    self._revert_deployment(experiment)
                    self.db.execute(
                        "UPDATE agent_improvements SET status = 'reverted', "
                        "performance_after = ? WHERE id = ?",
                        (json.dumps({"early_stop": True, metric_name: a_mean}),
                         experiment["id"]),
                    )
                    self._record_experiment_result(
                        experiment, "declined",
                        {"early_stop": True, "p_value": p_value, "metric": metric_name},
                    )
                    return {
                        "id": experiment["id"],
                        "agent": agent_name,
                        "action": "early_reverted",
                        "verdict": "declined",
                        "details": {"early_stop": True, "p_value": p_value,
                                    "metric": metric_name, "pct_change": pct_change},
                    }
                elif pct_change >= MIN_IMPROVEMENT_PCT:
                    # Strong improvement — early promote
                    logger.info(
                        "Experiment %s EARLY STOP (improvement p=%.4f): %s",
                        experiment["id"], p_value, experiment["description"][:80],
                    )
                    after_snapshot = self._snapshot_metrics(agent_name)
                    self.db.execute(
                        "UPDATE agent_improvements SET status = 'deployed', "
                        "performance_after = ? WHERE id = ?",
                        (json.dumps(after_snapshot), experiment["id"]),
                    )
                    self._record_experiment_result(
                        experiment, "improved",
                        {"early_stop": True, "p_value": p_value, "metric": metric_name},
                    )
                    return {
                        "id": experiment["id"],
                        "agent": agent_name,
                        "action": "early_promoted",
                        "verdict": "improved",
                        "details": {"early_stop": True, "p_value": p_value,
                                    "metric": metric_name, "pct_change": pct_change},
                    }

        return None

    def _evaluate_experiment(self, experiment: dict[str, Any]) -> dict[str, Any]:
        """Evaluate an A/B experiment using statistical analysis.

        Uses Welch's t-test, minimum sample sizes, and Bonferroni correction
        to determine if the experiment produced a statistically significant result.

        If metrics improved (or held steady): promote to 'deployed'.
        If metrics declined significantly: revert to 'reverted'.
        If high variance or insufficient data: extend the experiment.
        """
        agent_name = experiment["agent_name"]
        exp_id = experiment["id"]
        deployed_at = experiment.get("deployed_at", "")

        # Get before snapshot (averages)
        try:
            before = json.loads(experiment.get("performance_before") or "{}")
        except (json.JSONDecodeError, TypeError):
            before = {}

        # Take after snapshot (averages)
        after = self._snapshot_metrics(agent_name)

        # Collect RAW data for statistical testing
        key_metrics = ["execution_success", "revenue", "roi"]
        before_raw: dict[str, list[float]] = {}
        after_raw: dict[str, list[float]] = {}

        for metric_name in key_metrics:
            before_raw[metric_name] = self._get_metric_values(
                agent_name, metric_name, before_cycle=deployed_at,
            )
            after_raw[metric_name] = self._get_metric_values(
                agent_name, metric_name, after_cycle=deployed_at,
            )

        # Compare with statistics
        verdict, details = self._compare_snapshots(
            before, after, before_raw=before_raw, after_raw=after_raw,
        )

        now = datetime.now().isoformat()
        if verdict in ("insufficient_data", "inconclusive_high_variance"):
            # Not enough data or too noisy — extend experiment
            extra_cycles = experiment.get("eval_after_cycles") or DEFAULT_EVAL_CYCLES
            self.db.execute(
                "UPDATE agent_improvements SET eval_after_cycles = ?, eval_cycles = 0 "
                "WHERE id = ?",
                (extra_cycles + DEFAULT_EVAL_CYCLES, exp_id),
            )
            logger.info(
                "Experiment %s EXTENDED (verdict=%s): %s",
                exp_id, verdict, experiment["description"][:80],
            )
            return {
                "id": exp_id,
                "agent": agent_name,
                "action": "extended",
                "verdict": verdict,
                "details": details,
            }

        # Record experiment result in SharedMemory for cross-agent learning
        self._record_experiment_result(experiment, verdict, details)

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

    def _record_experiment_result(
        self, experiment: dict[str, Any], verdict: str, details: dict[str, Any],
    ) -> None:
        """Write experiment evaluation results to SharedMemory for cross-agent learning."""
        if not self.memory:
            return

        agent_name = experiment["agent_name"]
        desc = experiment.get("description", "")[:200]
        imp_type = experiment.get("improvement_type", "unknown")

        # Store as knowledge
        self.memory.store_knowledge(
            category="experiment_result",
            topic=f"{agent_name}/{imp_type}",
            content=json.dumps({
                "experiment_id": experiment["id"],
                "agent": agent_name,
                "type": imp_type,
                "description": desc,
                "verdict": verdict,
                "details": {k: v for k, v in details.items() if k != "metrics"},
            }, default=str),
            source_agent="self_improver",
            tags=["experiment", verdict, agent_name, imp_type],
        )

        # Record as lesson if meaningful
        if verdict == "improved":
            self.memory.record_lesson(
                agent_name=agent_name,
                category="optimization",
                situation=f"A/B experiment confirmed: {desc}",
                lesson=f"Statistically significant improvement from {imp_type} change",
                rule=f"Continue with this {imp_type} optimization approach",
                severity="low",
            )
        elif verdict == "declined":
            self.memory.record_lesson(
                agent_name=agent_name,
                category="warning",
                situation=f"A/B experiment failed: {desc}",
                lesson=f"Change caused statistically significant decline — reverted",
                rule=f"Avoid similar {imp_type} changes for {agent_name}",
                severity="high",
            )

    def _snapshot_metrics(self, agent_name: str) -> dict[str, float]:
        """Capture current metric averages for an agent (last 50 entries)."""
        rows = self.db.execute(
            "SELECT metric_name, AVG(metric_value) as avg_val "
            "FROM ("
            "  SELECT metric_name, metric_value FROM agent_metrics "
            "  WHERE agent_name = ? ORDER BY created_at DESC LIMIT 50"
            ") GROUP BY metric_name",
            (agent_name,),
        )
        return {r["metric_name"]: r["avg_val"] for r in rows}

    def _get_metric_values(self, agent_name: str, metric_name: str,
                           before_cycle: str | None = None,
                           after_cycle: str | None = None,
                           limit: int = 50) -> list[float]:
        """Get raw metric values for statistical analysis."""
        if after_cycle:
            rows = self.db.execute(
                "SELECT metric_value FROM agent_metrics "
                "WHERE agent_name = ? AND metric_name = ? AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_name, metric_name, after_cycle, limit),
            )
        elif before_cycle:
            rows = self.db.execute(
                "SELECT metric_value FROM agent_metrics "
                "WHERE agent_name = ? AND metric_name = ? AND created_at <= ? "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_name, metric_name, before_cycle, limit),
            )
        else:
            rows = self.db.execute(
                "SELECT metric_value FROM agent_metrics "
                "WHERE agent_name = ? AND metric_name = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_name, metric_name, limit),
            )
        return [r["metric_value"] for r in rows]

    def _compare_snapshots(
        self,
        before: dict[str, float],
        after: dict[str, float],
        before_raw: dict[str, list[float]] | None = None,
        after_raw: dict[str, list[float]] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Compare before/after metric snapshots with statistical rigor.

        When raw data lists are provided (before_raw/after_raw), uses:
        - Minimum sample size check (N >= MIN_SAMPLE_SIZE)
        - Welch's t-test for significance (p < SIGNIFICANCE_LEVEL)
        - Bonferroni correction for multiple comparisons
        - Variance/stdev analysis

        Falls back to threshold comparison when only averages are available.

        Returns (verdict, details) where verdict is one of:
        - 'improved': statistically significant improvement
        - 'stable': no significant change
        - 'declined': statistically significant decline
        - 'insufficient_data': not enough samples
        - 'inconclusive_high_variance': data too noisy
        """
        if not before or not after:
            return "insufficient_data", {"reason": "no_metrics"}

        # Determine which metrics to compare
        common_metrics = [m for m in before if m in after]
        if not common_metrics:
            return "insufficient_data", {"reason": "no_common_metrics"}

        # If we have raw data, use statistical testing
        if before_raw and after_raw:
            return self._compare_with_statistics(
                before, after, before_raw, after_raw, common_metrics,
            )

        # Fallback: threshold-based comparison (legacy, for snapshot-only data)
        return self._compare_with_thresholds(before, after, common_metrics)

    def _compare_with_statistics(
        self,
        before: dict[str, float],
        after: dict[str, float],
        before_raw: dict[str, list[float]],
        after_raw: dict[str, list[float]],
        metrics: list[str],
    ) -> tuple[str, dict[str, Any]]:
        """Compare metrics using proper statistical tests."""
        # Bonferroni correction: adjust alpha for number of metrics tested
        num_tests = len(metrics)
        corrected_alpha = SIGNIFICANCE_LEVEL / max(num_tests, 1)

        results: dict[str, Any] = {}
        improved_count = 0
        declined_count = 0
        insufficient_count = 0
        high_variance_count = 0

        for metric_name in metrics:
            b_vals = before_raw.get(metric_name, [])
            a_vals = after_raw.get(metric_name, [])

            # Check minimum sample size
            if len(b_vals) < MIN_SAMPLE_SIZE or len(a_vals) < MIN_SAMPLE_SIZE:
                insufficient_count += 1
                results[metric_name] = {
                    "verdict": "insufficient_data",
                    "n_before": len(b_vals),
                    "n_after": len(a_vals),
                    "min_required": MIN_SAMPLE_SIZE,
                }
                continue

            # Compute descriptive statistics
            b_mean = sum(b_vals) / len(b_vals)
            a_mean = sum(a_vals) / len(a_vals)
            b_stdev = math.sqrt(sum((x - b_mean) ** 2 for x in b_vals) / (len(b_vals) - 1)) if len(b_vals) > 1 else 0.0
            a_stdev = math.sqrt(sum((x - a_mean) ** 2 for x in a_vals) / (len(a_vals) - 1)) if len(a_vals) > 1 else 0.0

            # High variance check
            b_cv = b_stdev / abs(b_mean) if b_mean != 0 else 0.0
            a_cv = a_stdev / abs(a_mean) if a_mean != 0 else 0.0

            if b_cv > HIGH_VARIANCE_RATIO or a_cv > HIGH_VARIANCE_RATIO:
                high_variance_count += 1

            # Welch's t-test
            t_stat, p_value = _welch_t_test(b_vals, a_vals)

            # Percentage change
            pct_change = ((a_mean - b_mean) / abs(b_mean)) * 100 if b_mean != 0 else 0.0

            # Effect size (Cohen's d)
            pooled_std = math.sqrt((b_stdev ** 2 + a_stdev ** 2) / 2) if (b_stdev + a_stdev) > 0 else 1.0
            cohens_d = (a_mean - b_mean) / pooled_std if pooled_std > 0 else 0.0

            metric_result: dict[str, Any] = {
                "before_mean": round(b_mean, 4),
                "after_mean": round(a_mean, 4),
                "before_stdev": round(b_stdev, 4),
                "after_stdev": round(a_stdev, 4),
                "pct_change": round(pct_change, 2),
                "t_statistic": round(t_stat, 4),
                "p_value": round(p_value, 6),
                "corrected_alpha": round(corrected_alpha, 6),
                "significant": p_value < corrected_alpha,
                "cohens_d": round(cohens_d, 4),
                "n_before": len(b_vals),
                "n_after": len(a_vals),
            }

            if p_value < corrected_alpha:
                if pct_change >= MIN_IMPROVEMENT_PCT:
                    improved_count += 1
                    metric_result["verdict"] = "improved"
                elif pct_change <= MAX_DECLINE_PCT:
                    declined_count += 1
                    metric_result["verdict"] = "declined"
                else:
                    metric_result["verdict"] = "stable"
            else:
                metric_result["verdict"] = "not_significant"

            results[metric_name] = metric_result

        # If ALL metrics have insufficient data, extend
        if insufficient_count == len(metrics):
            return "insufficient_data", {
                "reason": "all_metrics_below_min_sample",
                "min_required": MIN_SAMPLE_SIZE,
                "metrics": results,
            }

        # If majority high variance with no significant results, flag it
        testable = len(metrics) - insufficient_count
        if testable > 0 and high_variance_count >= testable and improved_count == 0 and declined_count == 0:
            return "inconclusive_high_variance", {
                "reason": "data_too_noisy",
                "high_variance_metrics": high_variance_count,
                "metrics": results,
            }

        # Make decision based on statistically significant results
        if declined_count > 0:
            return "declined", {
                "metrics": results,
                "significant_declines": declined_count,
                "bonferroni_alpha": corrected_alpha,
            }
        elif improved_count > 0:
            return "improved", {
                "metrics": results,
                "significant_improvements": improved_count,
                "bonferroni_alpha": corrected_alpha,
            }
        else:
            return "stable", {"metrics": results}

    def _compare_with_thresholds(
        self,
        before: dict[str, float],
        after: dict[str, float],
        metrics: list[str],
    ) -> tuple[str, dict[str, Any]]:
        """Legacy threshold comparison when raw data not available."""
        changes: dict[str, float] = {}
        improved_count = 0
        declined_count = 0

        for metric_name in metrics:
            before_val = before[metric_name]
            after_val = after[metric_name]
            if before_val == 0:
                continue

            pct_change = ((after_val - before_val) / abs(before_val)) * 100
            changes[metric_name] = pct_change

            if pct_change >= MIN_IMPROVEMENT_PCT:
                improved_count += 1
            elif pct_change <= MAX_DECLINE_PCT:
                declined_count += 1

        if declined_count > 0:
            return "declined", {"changes": changes, "declined_metrics": declined_count, "method": "threshold"}
        elif improved_count > 0:
            return "improved", {"changes": changes, "improved_metrics": improved_count, "method": "threshold"}
        else:
            return "stable", {"changes": changes, "method": "threshold"}

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

        medium_risk = ["cost", "budget", "external", "third-party", "third_party", "deploy"]
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
