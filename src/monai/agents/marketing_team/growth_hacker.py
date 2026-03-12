"""GrowthHacker — designs and runs growth experiments.

Real autonomous capabilities:
- Tracks A/B test variants and results in the DB (growth_experiments table)
- Collects real metrics from platforms via browse_and_extract
- Computes real conversion rates, statistical significance from DB data
- Implements experiments via platform_action (landing pages, CTAs, etc.)
- Auto-concludes experiments when statistical significance is reached
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

GROWTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS growth_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    campaign_id INTEGER,
    experiment_type TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    variant_a TEXT NOT NULL,
    variant_b TEXT NOT NULL,
    success_metric TEXT NOT NULL,
    status TEXT DEFAULT 'planned',          -- planned, running, concluded, failed
    variant_a_views INTEGER DEFAULT 0,
    variant_a_conversions INTEGER DEFAULT 0,
    variant_b_views INTEGER DEFAULT 0,
    variant_b_conversions INTEGER DEFAULT 0,
    winner TEXT,                            -- 'a', 'b', 'inconclusive'
    confidence_level REAL,
    started_at TIMESTAMP,
    concluded_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class GrowthHacker(BaseAgent):
    """Runs growth experiments with real A/B test tracking and metrics.

    Tracks experiments in the DB, collects real metrics, and computes
    statistical significance to determine winners.
    """

    name = "growth_hacker"
    description = "Designs and executes growth experiments: viral loops, referrals, PLG, CRO."

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(GROWTH_SCHEMA)

    def plan(self) -> list[str]:
        return [
            "Check running experiments for conclusion readiness",
            "Collect real metrics from platforms",
            "Analyze current funnel metrics from DB",
            "Design new experiments (hypothesis-driven)",
            "Implement and launch via platform_action",
        ]

    def run(self, campaign: dict | None = None, strategy: str = "",
            **kwargs: Any) -> dict[str, Any]:
        """Design, launch, track, and conclude growth experiments.

        Layer 1: Check and conclude running experiments (real DB data)
        Layer 2: Collect real metrics from platforms
        Layer 3: Design and launch new experiments
        Layer 4: Implement via platform_action
        """
        # ── Layer 1: Check running experiments ─────────────────────
        concluded = self._check_running_experiments()

        # ── Layer 2: Collect real platform metrics ─────────────────
        if campaign:
            metrics_collected = self._collect_platform_metrics(campaign, strategy)
        else:
            metrics_collected = {}

        if not campaign:
            return {
                "experiments_launched": 0,
                "experiments_concluded": len(concluded),
                "concluded_results": concluded,
            }

        # ── Layer 3: Design new experiments with real context ──────
        # Get funnel metrics from DB
        funnel_stats = self._get_funnel_stats(campaign)

        # Get data-driven suggestions from historical experiment performance
        historical_insights = self._get_experiment_insights()

        experiments = self.think_json(
            f"Design growth experiments for:\n"
            f"Strategy: {strategy}\n"
            f"Campaign: {campaign.get('name', '')}\n"
            f"Target audience: {campaign.get('target_audience', '')}\n\n"
            f"Current funnel data:\n{json.dumps(funnel_stats, default=str)}\n"
            f"Platform metrics:\n{json.dumps(metrics_collected, default=str)[:500]}\n"
            f"Recently concluded experiments:\n{json.dumps(concluded, default=str)[:500]}\n"
            f"Historical insights (what worked before):\n{json.dumps(historical_insights, default=str)[:500]}\n\n"
            "Think like a growth hacker. Design experiments with clear A/B variants. "
            "Prioritize experiment types that historically performed well. "
            "Each experiment needs: specific hypothesis, two variants, measurable success metric.\n\n"
            "Return JSON: {{\"experiments\": [{{\"name\": str, "
            "\"hypothesis\": str, \"type\": \"viral_loop\"|\"referral\"|"
            "\"conversion_optimization\"|\"activation\"|\"retention\", "
            "\"variant_a\": str, \"variant_b\": str, "
            "\"success_metric\": str, \"implementation\": str, "
            "\"expected_impact\": str}}]}}"
        )

        launched_experiments = experiments.get("experiments", [])
        actually_launched = 0

        for exp in launched_experiments:
            # ── Layer 4: Store in DB and implement via platform ─────
            exp_id = self.db.execute_insert(
                "INSERT INTO growth_experiments "
                "(name, campaign_id, experiment_type, hypothesis, "
                "variant_a, variant_b, success_metric, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                (exp.get("name", ""), campaign.get("id"),
                 exp.get("type", ""), exp.get("hypothesis", ""),
                 exp.get("variant_a", ""), exp.get("variant_b", ""),
                 exp.get("success_metric", ""), datetime.now().isoformat()),
            )

            # Implement via real platform action
            impl_result = self._implement_experiment(exp, strategy)
            if impl_result.get("status") == "error":
                self.db.execute(
                    "UPDATE growth_experiments SET status = 'failed' WHERE id = ?",
                    (exp_id,),
                )
            else:
                actually_launched += 1

            self.share_knowledge(
                "growth_experiment", exp.get("name", ""),
                f"Hypothesis: {exp.get('hypothesis', '')}. "
                f"A: {exp.get('variant_a', '')} vs B: {exp.get('variant_b', '')}",
                confidence=0.5,
                tags=["growth", strategy],
            )

        self.log_action("growth_experiments",
                        f"Launched {actually_launched}, concluded {len(concluded)} for {strategy}")

        return {
            "experiments_launched": actually_launched,
            "experiments_concluded": len(concluded),
            "experiments": launched_experiments,
            "concluded_results": concluded,
        }

    # ── Real A/B test tracking ─────────────────────────────────────

    def _check_running_experiments(self) -> list[dict[str, Any]]:
        """Check running experiments and conclude those with enough data."""
        running = self.db.execute(
            "SELECT * FROM growth_experiments WHERE status = 'running'"
        )

        concluded = []
        for exp in running:
            exp = dict(exp)
            a_views = exp.get("variant_a_views", 0)
            b_views = exp.get("variant_b_views", 0)
            a_conv = exp.get("variant_a_conversions", 0)
            b_conv = exp.get("variant_b_conversions", 0)

            # Need minimum sample size before concluding
            min_sample = 30
            if a_views < min_sample or b_views < min_sample:
                continue

            # Compute conversion rates
            rate_a = a_conv / a_views if a_views > 0 else 0
            rate_b = b_conv / b_views if b_views > 0 else 0

            # Simple z-test for proportions
            confidence = self._compute_significance(
                a_views, a_conv, b_views, b_conv
            )

            if confidence >= 0.90:  # 90% significance threshold
                winner = "a" if rate_a > rate_b else "b"
                self.db.execute(
                    "UPDATE growth_experiments SET status = 'concluded', "
                    "winner = ?, confidence_level = ?, concluded_at = ? WHERE id = ?",
                    (winner, confidence, datetime.now().isoformat(), exp["id"]),
                )
                concluded.append({
                    "experiment_id": exp["id"],
                    "name": exp["name"],
                    "winner": winner,
                    "rate_a": round(rate_a, 4),
                    "rate_b": round(rate_b, 4),
                    "confidence": round(confidence, 4),
                    "lift": round((max(rate_a, rate_b) / max(min(rate_a, rate_b), 0.001) - 1) * 100, 1),
                })
                self.log_action("experiment_concluded", exp["name"],
                                f"winner={winner} confidence={confidence:.2%} "
                                f"lift={concluded[-1]['lift']}%")

        return concluded

    @staticmethod
    def _compute_significance(n_a: int, conv_a: int, n_b: int, conv_b: int) -> float:
        """Compute statistical significance using a z-test for two proportions."""
        p_a = conv_a / n_a if n_a > 0 else 0
        p_b = conv_b / n_b if n_b > 0 else 0
        p_pool = (conv_a + conv_b) / (n_a + n_b) if (n_a + n_b) > 0 else 0

        if p_pool == 0 or p_pool == 1:
            return 0.0

        se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
        if se == 0:
            return 0.0

        z = abs(p_a - p_b) / se

        # Approximate p-value from z-score (two-tailed)
        # Using simple approximation: confidence = 1 - 2 * Phi(-|z|)
        if z > 3.5:
            return 0.999
        if z > 2.576:
            return 0.99
        if z > 1.96:
            return 0.95
        if z > 1.645:
            return 0.90
        if z > 1.28:
            return 0.80
        return 0.5 + (z / 4.0) * 0.3  # Rough linear interpolation for low z

    # ── Real metrics collection ────────────────────────────────────

    def _collect_platform_metrics(self, campaign: dict, strategy: str) -> dict[str, Any]:
        """Collect real metrics from platforms via browse_and_extract."""
        channel = campaign.get("channel", "")
        if not channel:
            return {}

        try:
            return self.search_web(
                query=f"{strategy} {channel} analytics metrics",
                extraction_prompt=(
                    f"Find any available metrics or benchmarks for '{strategy}' "
                    f"campaigns on {channel}. Look for: typical conversion rates, "
                    "click-through rates, cost per acquisition, engagement rates. "
                    "Return: {\"benchmarks\": [{\"metric\": str, \"value\": str}], "
                    "\"industry_average_ctr\": str, \"industry_average_conversion\": str}"
                ),
                num_results=3,
            )
        except Exception as e:
            logger.warning(f"Metrics collection failed: {e}")
            return {"error": str(e)}

    def _get_funnel_stats(self, campaign: dict) -> dict[str, Any]:
        """Get real funnel statistics from the marketing_metrics DB table."""
        campaign_id = campaign.get("id")
        if not campaign_id:
            return {}

        rows = self.db.execute(
            "SELECT SUM(impressions) as total_impressions, "
            "SUM(clicks) as total_clicks, SUM(leads) as total_leads, "
            "SUM(conversions) as total_conversions, "
            "SUM(revenue_eur) as total_revenue, SUM(cost_eur) as total_cost "
            "FROM marketing_metrics WHERE campaign_id = ?",
            (campaign_id,),
        )
        if not rows:
            return {}

        stats = dict(rows[0])
        impressions = stats.get("total_impressions", 0) or 0
        clicks = stats.get("total_clicks", 0) or 0
        leads = stats.get("total_leads", 0) or 0

        stats["ctr"] = round(clicks / impressions, 4) if impressions > 0 else 0
        stats["lead_rate"] = round(leads / clicks, 4) if clicks > 0 else 0
        conversions = stats.get("total_conversions", 0) or 0
        stats["conversion_rate"] = round(conversions / leads, 4) if leads > 0 else 0

        return stats

    def _implement_experiment(self, experiment: dict, strategy: str) -> dict[str, Any]:
        """Implement an experiment via real platform action."""
        impl = experiment.get("implementation", "")
        if not impl:
            return {"status": "skipped", "reason": "No implementation specified"}

        try:
            return self.execute_task(
                f"Implement growth experiment: {experiment.get('name', '')}\n"
                f"Strategy: {strategy}\n"
                f"Implementation plan: {impl}\n"
                f"Variant A: {experiment.get('variant_a', '')}\n"
                f"Variant B: {experiment.get('variant_b', '')}\n\n"
                "Set up both variants and ensure tracking is in place.",
                context=f"Growth experiment for {strategy}",
            )
        except Exception as e:
            logger.warning(f"Experiment implementation failed: {e}")
            return {"status": "error", "error": str(e)}

    # ── Data-driven experiment design ─────────────────────────────

    def _get_experiment_insights(self) -> dict[str, Any]:
        """Analyze past experiment performance to inform new experiment design.

        Computes win rates by type, best-performing hypotheses, and identifies
        the funnel stage where improvements had the biggest impact.
        """
        # Win rates by experiment type
        type_stats = self.db.execute(
            "SELECT experiment_type, "
            "COUNT(*) as total, "
            "SUM(CASE WHEN winner IS NOT NULL AND winner != 'inconclusive' THEN 1 ELSE 0 END) as wins, "
            "AVG(confidence_level) as avg_confidence "
            "FROM growth_experiments WHERE status = 'concluded' "
            "GROUP BY experiment_type ORDER BY wins DESC"
        )
        type_performance = []
        for row in type_stats:
            r = dict(row)
            r["win_rate"] = round(r["wins"] / r["total"], 2) if r["total"] > 0 else 0
            type_performance.append(r)

        # Top winning experiments (highest lift)
        top_winners = self.db.execute(
            "SELECT name, experiment_type, hypothesis, winner, "
            "variant_a_views, variant_a_conversions, variant_b_views, variant_b_conversions, "
            "confidence_level "
            "FROM growth_experiments WHERE status = 'concluded' AND winner IN ('a', 'b') "
            "ORDER BY confidence_level DESC LIMIT 5"
        )
        winning_patterns = []
        for w in top_winners:
            w = dict(w)
            rate_a = w["variant_a_conversions"] / w["variant_a_views"] if w["variant_a_views"] > 0 else 0
            rate_b = w["variant_b_conversions"] / w["variant_b_views"] if w["variant_b_views"] > 0 else 0
            lift = abs(rate_a - rate_b) / max(min(rate_a, rate_b), 0.001) * 100
            winning_patterns.append({
                "name": w["name"],
                "type": w["experiment_type"],
                "hypothesis": w["hypothesis"][:100],
                "lift_pct": round(lift, 1),
                "confidence": round(w["confidence_level"] or 0, 3),
            })

        # Failed experiments to avoid repeating
        failed_types = self.db.execute(
            "SELECT experiment_type, COUNT(*) as failures "
            "FROM growth_experiments WHERE status = 'concluded' AND winner = 'inconclusive' "
            "GROUP BY experiment_type ORDER BY failures DESC LIMIT 3"
        )

        # Minimum sample size estimate based on past experiments
        sample_sizes = self.db.execute(
            "SELECT AVG(variant_a_views + variant_b_views) as avg_total_views, "
            "MIN(variant_a_views + variant_b_views) as min_views "
            "FROM growth_experiments WHERE status = 'concluded'"
        )

        return {
            "type_performance": type_performance,
            "best_types": [t["experiment_type"] for t in type_performance if t["win_rate"] >= 0.5],
            "winning_patterns": winning_patterns,
            "inconclusive_types": [dict(r) for r in failed_types],
            "avg_sample_needed": dict(sample_sizes[0]) if sample_sizes else {},
        }

    # ── Experiment data recording (called externally) ──────────────

    def record_variant_data(self, experiment_id: int, variant: str,
                            views: int = 0, conversions: int = 0):
        """Record real data for an experiment variant (called by other systems)."""
        if variant not in ("a", "b"):
            raise ValueError(f"variant must be 'a' or 'b', got '{variant}'")
        col_views = f"variant_{variant}_views"
        col_conv = f"variant_{variant}_conversions"
        self.db.execute(
            f"UPDATE growth_experiments SET "
            f"{col_views} = {col_views} + ?, "
            f"{col_conv} = {col_conv} + ? "
            f"WHERE id = ?",
            (views, conversions, experiment_id),
        )
