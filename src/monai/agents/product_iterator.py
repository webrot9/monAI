"""Product iteration engine — continuously improves products based on real data.

Monitors product performance (sales, reviews, engagement), analyzes competitors,
identifies gaps, and triggers automatic improvement cycles. This is the "project
improvement" layer that ensures monAI's products get better over time.

Flow:
1. Collect performance data (sales velocity, refund rate, review scores)
2. Identify underperformers vs. portfolio average
3. For underperformers: run competitor analysis → find gaps → generate improvements
4. Feed improvements back to the strategy agent for rebuilding
5. Track iteration history to measure improvement over time
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

ITERATOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_iterations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    product_name TEXT NOT NULL,
    iteration_number INTEGER NOT NULL DEFAULT 1,
    trigger TEXT NOT NULL,               -- low_sales, negative_review, competitor_gap, scheduled
    analysis TEXT,                       -- JSON: what was found
    improvements TEXT,                   -- JSON: what changes to make
    status TEXT DEFAULT 'pending',       -- pending, in_progress, applied, skipped
    performance_before TEXT,             -- JSON: metrics snapshot before
    performance_after TEXT,              -- JSON: metrics snapshot after (filled later)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS product_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    product_name TEXT NOT NULL,
    metric_name TEXT NOT NULL,           -- sales_count, revenue, refund_rate, review_score, conversion_rate
    metric_value REAL NOT NULL,
    period TEXT NOT NULL,                -- daily, weekly, monthly
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class ProductIterator(BaseAgent):
    """Monitors product performance and drives continuous improvement.

    Works across ALL strategies that produce products or content.
    Uses real sales data, competitor analysis, and quality metrics
    to identify what needs improvement and generate specific action items.
    """

    name = "product_iterator"
    description = (
        "Continuous product improvement engine. Monitors sales/engagement, "
        "analyzes competitors, identifies gaps, and triggers improvement cycles."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        with db.connect() as conn:
            conn.executescript(ITERATOR_SCHEMA)

    def plan(self) -> list[str]:
        return ["analyze_performance"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Run one iteration cycle across all strategies."""
        self.log_action("run_start", "Starting product iteration cycle")
        results: dict[str, Any] = {}

        # Step 1: Collect performance metrics across strategies
        metrics = self._collect_performance_metrics()
        results["metrics_collected"] = len(metrics)

        # Step 2: Identify underperformers
        underperformers = self._identify_underperformers(metrics)
        results["underperformers"] = len(underperformers)

        # Step 3: For each underperformer, run competitor analysis + improvement
        iterations: list[dict[str, Any]] = []
        for product in underperformers[:3]:  # Max 3 per cycle to control costs
            iteration = self._iterate_product(product)
            iterations.append(iteration)
        results["iterations"] = iterations

        # Step 4: Check if previous iterations improved performance
        evaluations = self._evaluate_past_iterations()
        results["evaluations"] = evaluations

        self.log_action("run_complete", json.dumps(results, default=str)[:500])
        return results

    def _collect_performance_metrics(self) -> list[dict[str, Any]]:
        """Collect performance data from product_reviews and finance tables."""
        metrics = []

        # Get product review scores
        try:
            reviews = self.db.execute(
                "SELECT strategy, product_name, quality_score, verdict, "
                "humanizer_score, factcheck_accuracy, usability_score "
                "FROM product_reviews ORDER BY created_at DESC LIMIT 50"
            )
            for r in reviews:
                rd = dict(r)
                self._record_metric(
                    rd["strategy"], rd["product_name"],
                    "quality_score", rd["quality_score"],
                )
                metrics.append(rd)
        except Exception:
            pass

        # Get strategy revenue data
        try:
            strategy_revenue = self.db.execute(
                "SELECT strategy, SUM(amount) as total_revenue, COUNT(*) as sales_count "
                "FROM payments WHERE status = 'completed' "
                "GROUP BY strategy"
            )
            for sr in strategy_revenue:
                srd = dict(sr)
                self._record_metric(
                    srd["strategy"], "_aggregate",
                    "revenue", srd["total_revenue"],
                )
                self._record_metric(
                    srd["strategy"], "_aggregate",
                    "sales_count", srd["sales_count"],
                )
                metrics.append(srd)
        except Exception:
            pass

        # Get refund data
        try:
            refunds = self.db.execute(
                "SELECT strategy, COUNT(*) as refund_count, SUM(amount) as refund_total "
                "FROM payments WHERE status = 'refunded' GROUP BY strategy"
            )
            for ref in refunds:
                refd = dict(ref)
                self._record_metric(
                    refd["strategy"], "_aggregate",
                    "refund_count", refd["refund_count"],
                )
                metrics.append(refd)
        except Exception:
            pass

        return metrics

    def _record_metric(self, strategy: str, product_name: str,
                       metric_name: str, value: float) -> None:
        """Record a performance metric for trend tracking."""
        self.db.execute_insert(
            "INSERT INTO product_performance "
            "(strategy, product_name, metric_name, metric_value, period) "
            "VALUES (?, ?, ?, ?, 'cycle')",
            (strategy, product_name, metric_name, value),
        )

    def _identify_underperformers(
        self, metrics: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Identify products that are underperforming vs portfolio average."""
        # Get review scores grouped by product
        try:
            product_scores = self.db.execute(
                "SELECT strategy, product_name, "
                "AVG(quality_score) as avg_score, "
                "COUNT(*) as review_count, "
                "MAX(created_at) as last_review "
                "FROM product_reviews "
                "GROUP BY strategy, product_name "
                "HAVING avg_score < 0.7 OR verdict = 'needs_revision' "
                "ORDER BY avg_score ASC LIMIT 10"
            )
            underperformers = [dict(r) for r in product_scores]
        except Exception:
            underperformers = []

        # Also check strategies with zero sales after being active
        try:
            zero_revenue = self.db.execute(
                "SELECT s.name as strategy FROM strategies s "
                "LEFT JOIN payments p ON s.name = p.strategy AND p.status = 'completed' "
                "WHERE s.status = 'active' "
                "GROUP BY s.name "
                "HAVING COUNT(p.id) = 0"
            )
            for zr in zero_revenue:
                underperformers.append({
                    "strategy": zr["strategy"],
                    "product_name": "_no_sales",
                    "avg_score": 0.0,
                    "trigger": "zero_sales",
                })
        except Exception:
            pass

        return underperformers

    def _iterate_product(self, product: dict[str, Any]) -> dict[str, Any]:
        """Run one improvement iteration on an underperforming product."""
        strategy = product.get("strategy", "unknown")
        product_name = product.get("product_name", "unknown")
        trigger = product.get("trigger", "low_quality")

        self.log_action("iterate_start", f"{strategy}/{product_name} (trigger: {trigger})")

        # Get existing review feedback
        review_feedback = []
        try:
            existing_reviews = self.db.execute(
                "SELECT issues, suggestions, quality_score FROM product_reviews "
                "WHERE strategy = ? AND product_name = ? "
                "ORDER BY created_at DESC LIMIT 3",
                (strategy, product_name),
            )
            for r in existing_reviews:
                rd = dict(r)
                issues = json.loads(rd.get("issues", "[]") or "[]")
                suggestions = json.loads(rd.get("suggestions", "[]") or "[]")
                review_feedback.extend(issues)
                review_feedback.extend(suggestions)
        except Exception:
            pass  # product_reviews table may not exist yet

        # Run competitor analysis via web search
        competitor_data = self._analyze_competitors(strategy, product_name)

        # Generate improvement plan via LLM
        improvement_plan = self.think_json(
            f"You are a product strategist. A {strategy} product '{product_name}' "
            f"is underperforming.\n\n"
            f"TRIGGER: {trigger}\n"
            f"QUALITY SCORE: {product.get('avg_score', 'N/A')}\n\n"
            f"REVIEW FEEDBACK:\n"
            f"{json.dumps(review_feedback[:10], default=str)}\n\n"
            f"COMPETITOR ANALYSIS:\n"
            f"{json.dumps(competitor_data, default=str)[:2000]}\n\n"
            "Generate a specific, actionable improvement plan.\n\n"
            "Return: {\"improvements\": [{\"area\": str, \"current_issue\": str, "
            "\"specific_change\": str, \"expected_impact\": str, "
            "\"priority\": int}], "
            "\"rebuild_recommended\": bool, \"rebuild_reason\": str}"
        )

        # Get iteration count
        existing_iterations = self.db.execute(
            "SELECT MAX(iteration_number) as max_iter FROM product_iterations "
            "WHERE strategy = ? AND product_name = ?",
            (strategy, product_name),
        )
        iter_num = 1
        if existing_iterations and existing_iterations[0]["max_iter"]:
            iter_num = existing_iterations[0]["max_iter"] + 1

        # Store the iteration
        performance_before = {
            "quality_score": product.get("avg_score", 0),
            "trigger": trigger,
        }
        self.db.execute_insert(
            "INSERT INTO product_iterations "
            "(strategy, product_name, iteration_number, trigger, "
            "analysis, improvements, performance_before, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                strategy, product_name, iter_num, trigger,
                json.dumps(competitor_data, default=str),
                json.dumps(improvement_plan, default=str),
                json.dumps(performance_before),
            ),
        )

        self.log_action(
            "iteration_planned",
            f"{strategy}/{product_name} iter#{iter_num}: "
            f"{len(improvement_plan.get('improvements', []))} improvements, "
            f"rebuild={'yes' if improvement_plan.get('rebuild_recommended') else 'no'}",
        )

        return {
            "strategy": strategy,
            "product_name": product_name,
            "iteration": iter_num,
            "improvements": len(improvement_plan.get("improvements", [])),
            "rebuild_recommended": improvement_plan.get("rebuild_recommended", False),
        }

    def _analyze_competitors(
        self, strategy: str, product_name: str,
    ) -> dict[str, Any]:
        """Analyze competitors for a specific strategy/product via web search."""
        strategy_search_map = {
            "digital_products": f"best {product_name} alternatives Gumroad 2026",
            "micro_saas": f"{product_name} competitors features pricing 2026",
            "saas": f"{product_name} vs alternatives comparison 2026",
            "course_creation": f"best online courses {product_name} topic Udemy 2026",
            "telegram_bots": f"popular Telegram bots like {product_name} 2026",
            "affiliate": f"best affiliate review sites {product_name} niche 2026",
            "content_sites": f"top content sites {product_name} keyword 2026",
            "newsletter": f"top newsletters {product_name} niche 2026",
            "print_on_demand": f"trending {product_name} designs POD 2026",
        }

        query = strategy_search_map.get(
            strategy,
            f"competitors for {product_name} {strategy} 2026",
        )

        competitor_data = self.search_web(
            query,
            "Extract competitor product names, their key features, pricing, "
            "user ratings, and any differentiators mentioned. Only include "
            "REAL data visible on the page. Do NOT make up any information. "
            "Return as JSON: {\"competitors\": [{\"name\": str, \"features\": [str], "
            "\"pricing\": str, \"rating\": str, \"differentiator\": str}]}"
        )

        return competitor_data

    def _evaluate_past_iterations(self) -> list[dict[str, Any]]:
        """Check if previous improvement iterations actually helped."""
        pending_eval = self.db.execute(
            "SELECT * FROM product_iterations "
            "WHERE status = 'applied' AND performance_after IS NULL "
            "AND applied_at < datetime('now', '-3 days') "
            "LIMIT 5"
        )

        results = []
        for iteration in pending_eval:
            it = dict(iteration)
            # Get current quality score
            current_reviews = self.db.execute(
                "SELECT AVG(quality_score) as avg_score FROM product_reviews "
                "WHERE strategy = ? AND product_name = ? "
                "AND created_at > ?",
                (it["strategy"], it["product_name"], it["applied_at"]),
            )

            if current_reviews and current_reviews[0]["avg_score"] is not None:
                after_score = current_reviews[0]["avg_score"]
                before = json.loads(it.get("performance_before", "{}") or "{}")
                before_score = before.get("quality_score", 0)

                improved = after_score > before_score
                self.db.execute(
                    "UPDATE product_iterations SET performance_after = ? WHERE id = ?",
                    (json.dumps({"quality_score": after_score}), it["id"]),
                )

                result = {
                    "strategy": it["strategy"],
                    "product": it["product_name"],
                    "iteration": it["iteration_number"],
                    "before": before_score,
                    "after": after_score,
                    "improved": improved,
                }
                results.append(result)

                if improved:
                    self.log_action(
                        "iteration_success",
                        f"{it['strategy']}/{it['product_name']}: "
                        f"score {before_score:.2f} → {after_score:.2f}",
                    )
                else:
                    self.log_action(
                        "iteration_stale",
                        f"{it['strategy']}/{it['product_name']}: "
                        f"no improvement ({before_score:.2f} → {after_score:.2f})",
                    )

        return results

    def get_pending_improvements(self, strategy: str) -> list[dict[str, Any]]:
        """Get pending improvement plans for a strategy.

        Strategy agents call this to check if they should rebuild/update
        any of their products based on iteration analysis.
        """
        rows = self.db.execute(
            "SELECT * FROM product_iterations "
            "WHERE strategy = ? AND status = 'pending' "
            "ORDER BY created_at ASC",
            (strategy,),
        )
        return [dict(r) for r in rows]

    def mark_applied(self, iteration_id: int) -> None:
        """Mark an iteration as applied by the strategy agent."""
        self.db.execute(
            "UPDATE product_iterations SET status = 'applied', "
            "applied_at = ? WHERE id = ?",
            (datetime.now().isoformat(), iteration_id),
        )

    def get_iteration_summary(self) -> dict[str, Any]:
        """Get summary of all iterations across strategies."""
        rows = self.db.execute(
            "SELECT strategy, status, COUNT(*) as count "
            "FROM product_iterations GROUP BY strategy, status"
        )
        summary: dict[str, dict[str, int]] = {}
        for r in rows:
            strategy = r["strategy"]
            if strategy not in summary:
                summary[strategy] = {}
            summary[strategy][r["status"]] = r["count"]
        return summary
