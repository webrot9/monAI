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
    trigger TEXT NOT NULL,               -- low_sales, negative_review, competitor_gap, scheduled, customer_feedback
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
    metric_name TEXT NOT NULL,           -- sales_count, revenue, refund_rate, review_score, conversion_rate, customer_rating, nps
    metric_value REAL NOT NULL,
    period TEXT NOT NULL,                -- daily, weekly, monthly, cycle
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    product_name TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    features TEXT,                       -- JSON list of features
    pricing TEXT,                        -- pricing info string
    rating TEXT,                         -- user rating string
    differentiator TEXT,                 -- key differentiator
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(strategy, product_name, competitor_name)
);

CREATE TABLE IF NOT EXISTS competitor_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,            -- pricing, features, rating
    old_value TEXT,
    new_value TEXT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (competitor_id) REFERENCES competitors(id)
);
"""

# Evaluation thresholds for product iteration
MIN_REVIEW_SAMPLES = 3          # Minimum reviews before evaluating iteration
LOW_CUSTOMER_RATING = 3.5       # Flag products with avg rating below this
HIGH_REFUND_RATE = 0.1          # Flag if refund rate > 10%


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
        self._ensure_payment_columns()

    def _ensure_payment_columns(self) -> None:
        """Add refund_reason column to payments table if missing."""
        try:
            self.db.execute("ALTER TABLE payments ADD COLUMN refund_reason TEXT")
        except Exception:
            pass  # Column already exists or payments table doesn't exist yet

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
        except Exception as e:
            logger.warning("Failed to fetch product review metrics: %s", e)

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
        except Exception as e:
            logger.warning("Failed to fetch strategy revenue data: %s", e)

        # Get refund data (with reasons if available)
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
                # Calculate refund rate
                try:
                    total_sales = self.db.execute(
                        "SELECT COUNT(*) as cnt FROM payments "
                        "WHERE strategy = ? AND status IN ('completed', 'refunded')",
                        (refd["strategy"],),
                    )
                    total = total_sales[0]["cnt"] if total_sales else 0
                    if total > 0:
                        refund_rate = refd["refund_count"] / total
                        self._record_metric(
                            refd["strategy"], "_aggregate",
                            "refund_rate", refund_rate,
                        )
                except Exception:
                    pass
                metrics.append(refd)
        except Exception as e:
            logger.warning("Failed to fetch refund data: %s", e)

        # Get customer feedback data (ratings, NPS)
        try:
            customer_data = self.db.execute(
                "SELECT strategy, product_name, "
                "AVG(customer_rating) as avg_rating, "
                "AVG(nps_score) as avg_nps, "
                "SUM(support_tickets) as total_tickets, "
                "COUNT(customer_rating) as feedback_count "
                "FROM product_reviews "
                "WHERE customer_rating IS NOT NULL "
                "GROUP BY strategy, product_name"
            )
            for cd in customer_data:
                cdd = dict(cd)
                if cdd["avg_rating"] is not None:
                    self._record_metric(
                        cdd["strategy"], cdd["product_name"],
                        "customer_rating", cdd["avg_rating"],
                    )
                if cdd["avg_nps"] is not None:
                    self._record_metric(
                        cdd["strategy"], cdd["product_name"],
                        "nps_score", cdd["avg_nps"],
                    )
                if cdd["total_tickets"] and cdd["total_tickets"] > 0:
                    self._record_metric(
                        cdd["strategy"], cdd["product_name"],
                        "support_tickets", cdd["total_tickets"],
                    )
                metrics.append(cdd)
        except Exception as e:
            logger.debug("Customer feedback data not available: %s", e)

        # Collect refund reasons for analysis
        try:
            refund_reasons = self.db.execute(
                "SELECT strategy, refund_reason, COUNT(*) as cnt "
                "FROM payments WHERE status = 'refunded' AND refund_reason IS NOT NULL "
                "AND refund_reason != '' "
                "GROUP BY strategy, refund_reason ORDER BY cnt DESC LIMIT 20"
            )
            for rr in refund_reasons:
                metrics.append({"type": "refund_reason", **dict(rr)})
        except Exception as e:
            logger.debug("Refund reasons not available: %s", e)

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
        except Exception as e:
            logger.warning("Failed to check zero-revenue strategies: %s", e)

        # Check products with low customer ratings
        try:
            low_rated = self.db.execute(
                "SELECT strategy, product_name, "
                "AVG(customer_rating) as avg_rating, "
                "COUNT(customer_rating) as rating_count "
                "FROM product_reviews "
                "WHERE customer_rating IS NOT NULL "
                "GROUP BY strategy, product_name "
                f"HAVING avg_rating < {LOW_CUSTOMER_RATING} AND rating_count >= {MIN_REVIEW_SAMPLES} "
                "ORDER BY avg_rating ASC LIMIT 5"
            )
            existing = {(u["strategy"], u["product_name"]) for u in underperformers}
            for lr in low_rated:
                key = (lr["strategy"], lr["product_name"])
                if key not in existing:
                    underperformers.append({
                        "strategy": lr["strategy"],
                        "product_name": lr["product_name"],
                        "avg_score": lr["avg_rating"] / 5.0,  # Normalize to 0-1
                        "trigger": "low_customer_rating",
                        "customer_rating": lr["avg_rating"],
                    })
        except Exception as e:
            logger.debug("Customer rating check not available: %s", e)

        # Check strategies with high refund rates
        try:
            high_refunds = self.db.execute(
                "SELECT strategy, "
                "SUM(CASE WHEN status = 'refunded' THEN 1 ELSE 0 END) as refunds, "
                "COUNT(*) as total "
                "FROM payments "
                "WHERE status IN ('completed', 'refunded') "
                "GROUP BY strategy "
                f"HAVING CAST(refunds AS REAL) / total > {HIGH_REFUND_RATE} "
                "AND total >= 5"
            )
            existing = {(u["strategy"], u["product_name"]) for u in underperformers}
            for hr in high_refunds:
                key = (hr["strategy"], "_high_refunds")
                if key not in existing:
                    refund_rate = hr["refunds"] / hr["total"]
                    underperformers.append({
                        "strategy": hr["strategy"],
                        "product_name": "_high_refunds",
                        "avg_score": 1.0 - refund_rate,
                        "trigger": "high_refund_rate",
                        "refund_rate": refund_rate,
                    })
        except Exception as e:
            logger.debug("Refund rate check not available: %s", e)

        return underperformers

    def _iterate_product(self, product: dict[str, Any]) -> dict[str, Any]:
        """Run one improvement iteration on an underperforming product."""
        strategy = product.get("strategy", "unknown")
        product_name = product.get("product_name", "unknown")
        trigger = product.get("trigger", "low_quality")

        self.log_action("iterate_start", f"{strategy}/{product_name} (trigger: {trigger})")

        # Get existing review feedback (internal + customer)
        review_feedback = []
        customer_feedback = []
        try:
            existing_reviews = self.db.execute(
                "SELECT issues, suggestions, quality_score, "
                "customer_rating, customer_feedback, nps_score, support_tickets "
                "FROM product_reviews "
                "WHERE strategy = ? AND product_name = ? "
                "ORDER BY created_at DESC LIMIT 5",
                (strategy, product_name),
            )
            for r in existing_reviews:
                rd = dict(r)
                issues = json.loads(rd.get("issues", "[]") or "[]")
                suggestions = json.loads(rd.get("suggestions", "[]") or "[]")
                review_feedback.extend(issues)
                review_feedback.extend(suggestions)
                # Collect customer voice
                if rd.get("customer_feedback"):
                    customer_feedback.append({
                        "rating": rd.get("customer_rating"),
                        "feedback": rd["customer_feedback"],
                        "nps": rd.get("nps_score"),
                    })
        except Exception as e:
            logger.debug("Could not fetch review feedback (table may not exist): %s", e)

        # Get refund reasons for this product's strategy
        refund_reasons = []
        try:
            reasons = self.db.execute(
                "SELECT refund_reason, COUNT(*) as cnt "
                "FROM payments WHERE strategy = ? AND status = 'refunded' "
                "AND refund_reason IS NOT NULL AND refund_reason != '' "
                "GROUP BY refund_reason ORDER BY cnt DESC LIMIT 5",
                (strategy,),
            )
            refund_reasons = [dict(r) for r in reasons]
        except Exception:
            pass

        # Run competitor analysis via web search
        competitor_data = self._analyze_competitors(strategy, product_name)

        # Generate improvement plan via LLM (includes customer voice + refund data)
        customer_section = ""
        if customer_feedback:
            customer_section = (
                f"\n\nCUSTOMER FEEDBACK ({len(customer_feedback)} reviews):\n"
                f"{json.dumps(customer_feedback[:5], default=str)}"
            )
        refund_section = ""
        if refund_reasons:
            refund_section = (
                f"\n\nREFUND REASONS:\n"
                f"{json.dumps(refund_reasons, default=str)}"
            )

        improvement_plan = self.think_json(
            f"You are a product strategist. A {strategy} product '{product_name}' "
            f"is underperforming.\n\n"
            f"TRIGGER: {trigger}\n"
            f"QUALITY SCORE: {product.get('avg_score', 'N/A')}\n"
            f"CUSTOMER RATING: {product.get('customer_rating', 'N/A')}\n"
            f"REFUND RATE: {product.get('refund_rate', 'N/A')}\n\n"
            f"INTERNAL REVIEW FEEDBACK:\n"
            f"{json.dumps(review_feedback[:10], default=str)}\n"
            f"{customer_section}{refund_section}\n\n"
            f"COMPETITOR ANALYSIS:\n"
            f"{json.dumps(competitor_data, default=str)[:2000]}\n\n"
            "Generate a specific, actionable improvement plan. "
            "Prioritize issues reported by REAL CUSTOMERS over internal reviews.\n\n"
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
        """Analyze competitors for a specific strategy/product via web search.

        Results are persisted to the competitors table for historical tracking.
        Changes in competitor pricing/features/ratings are logged to competitor_history.
        """
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

        # Persist competitors to DB for historical tracking
        self._persist_competitors(strategy, product_name, competitor_data)

        return competitor_data

    def _persist_competitors(
        self, strategy: str, product_name: str, competitor_data: dict[str, Any],
    ) -> None:
        """Store competitor data with change tracking."""
        competitors = competitor_data.get("competitors", [])
        if not isinstance(competitors, list):
            return

        for comp in competitors:
            if not isinstance(comp, dict) or not comp.get("name"):
                continue

            comp_name = str(comp["name"])[:200]
            features_json = json.dumps(comp.get("features", []))
            pricing = str(comp.get("pricing", ""))[:200]
            rating = str(comp.get("rating", ""))[:100]
            differentiator = str(comp.get("differentiator", ""))[:500]

            # Check if competitor already exists
            existing = self.db.execute(
                "SELECT id, features, pricing, rating, differentiator "
                "FROM competitors "
                "WHERE strategy = ? AND product_name = ? AND competitor_name = ?",
                (strategy, product_name, comp_name),
            )

            if existing:
                # Update existing — track changes
                old = dict(existing[0])
                comp_id = old["id"]
                changes = []

                if old.get("pricing") != pricing and pricing:
                    changes.append(("pricing", old.get("pricing", ""), pricing))
                if old.get("rating") != rating and rating:
                    changes.append(("rating", old.get("rating", ""), rating))
                if old.get("features") != features_json and features_json != "[]":
                    changes.append(("features", old.get("features", ""), features_json))
                if old.get("differentiator") != differentiator and differentiator:
                    changes.append(("differentiator", old.get("differentiator", ""), differentiator))

                # Record changes in history
                for field_name, old_val, new_val in changes:
                    self.db.execute_insert(
                        "INSERT INTO competitor_history "
                        "(competitor_id, field_name, old_value, new_value) "
                        "VALUES (?, ?, ?, ?)",
                        (comp_id, field_name, old_val, new_val),
                    )

                # Update competitor record
                if changes:
                    self.db.execute(
                        "UPDATE competitors SET features = ?, pricing = ?, "
                        "rating = ?, differentiator = ? WHERE id = ?",
                        (features_json, pricing, rating, differentiator, comp_id),
                    )
            else:
                # Insert new competitor
                try:
                    self.db.execute_insert(
                        "INSERT INTO competitors "
                        "(strategy, product_name, competitor_name, features, "
                        "pricing, rating, differentiator) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (strategy, product_name, comp_name, features_json,
                         pricing, rating, differentiator),
                    )
                except Exception as e:
                    logger.debug("Failed to insert competitor %s: %s", comp_name, e)

    def get_competitor_trends(
        self, strategy: str, product_name: str,
    ) -> list[dict[str, Any]]:
        """Get competitor data with historical changes for a product."""
        competitors = self.db.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM competitor_history WHERE competitor_id = c.id) as change_count "
            "FROM competitors c "
            "WHERE c.strategy = ? AND c.product_name = ? "
            "ORDER BY c.discovered_at DESC",
            (strategy, product_name),
        )
        results = []
        for comp in competitors:
            cd = dict(comp)
            # Get recent changes
            changes = self.db.execute(
                "SELECT field_name, old_value, new_value, changed_at "
                "FROM competitor_history WHERE competitor_id = ? "
                "ORDER BY changed_at DESC LIMIT 10",
                (cd["id"],),
            )
            cd["recent_changes"] = [dict(ch) for ch in changes]
            results.append(cd)
        return results

    def _evaluate_past_iterations(self) -> list[dict[str, Any]]:
        """Check if previous improvement iterations actually helped.

        Uses multiple metrics (quality score, customer rating, revenue, refund rate)
        and requires minimum sample size before making a determination.
        """
        pending_eval = self.db.execute(
            "SELECT * FROM product_iterations "
            "WHERE status = 'applied' AND performance_after IS NULL "
            "AND applied_at < datetime('now', '-3 days') "
            "LIMIT 5"
        )

        results = []
        for iteration in pending_eval:
            it = dict(iteration)

            # Get reviews after improvement
            current_reviews = self.db.execute(
                "SELECT AVG(quality_score) as avg_score, "
                "AVG(customer_rating) as avg_customer_rating, "
                "COUNT(*) as review_count "
                "FROM product_reviews "
                "WHERE strategy = ? AND product_name = ? "
                "AND created_at > ?",
                (it["strategy"], it["product_name"], it["applied_at"]),
            )

            review_count = 0
            if current_reviews and current_reviews[0]["review_count"]:
                review_count = current_reviews[0]["review_count"]

            # Require minimum sample before evaluating
            if review_count < MIN_REVIEW_SAMPLES:
                logger.debug(
                    "Iteration %s: only %d reviews (need %d), skipping eval",
                    it["id"], review_count, MIN_REVIEW_SAMPLES,
                )
                continue

            before = json.loads(it.get("performance_before", "{}") or "{}")
            before_score = before.get("quality_score", 0)

            after_data: dict[str, Any] = {}
            improvement_signals = 0
            decline_signals = 0

            # Quality score comparison
            after_score = current_reviews[0]["avg_score"]
            if after_score is not None:
                after_data["quality_score"] = after_score
                if after_score > before_score:
                    improvement_signals += 1
                elif after_score < before_score * 0.95:
                    decline_signals += 1

            # Customer rating comparison
            avg_customer = current_reviews[0]["avg_customer_rating"]
            if avg_customer is not None:
                after_data["customer_rating"] = avg_customer
                before_rating = before.get("customer_rating")
                if before_rating and avg_customer > before_rating:
                    improvement_signals += 1
                elif before_rating and avg_customer < before_rating:
                    decline_signals += 1

            # Revenue comparison (if available)
            try:
                revenue_after = self.db.execute(
                    "SELECT SUM(amount) as rev FROM payments "
                    "WHERE strategy = ? AND status = 'completed' "
                    "AND created_at > ?",
                    (it["strategy"], it["applied_at"]),
                )
                if revenue_after and revenue_after[0]["rev"]:
                    after_data["revenue_since"] = revenue_after[0]["rev"]
            except Exception:
                pass

            # Refund rate after iteration
            try:
                refund_data = self.db.execute(
                    "SELECT "
                    "SUM(CASE WHEN status = 'refunded' THEN 1 ELSE 0 END) as refunds, "
                    "COUNT(*) as total "
                    "FROM payments "
                    "WHERE strategy = ? AND created_at > ? "
                    "AND status IN ('completed', 'refunded')",
                    (it["strategy"], it["applied_at"]),
                )
                if refund_data and refund_data[0]["total"] and refund_data[0]["total"] >= 3:
                    refund_rate = (refund_data[0]["refunds"] or 0) / refund_data[0]["total"]
                    after_data["refund_rate"] = refund_rate
                    if refund_rate < HIGH_REFUND_RATE:
                        improvement_signals += 1
                    elif refund_rate > HIGH_REFUND_RATE * 1.5:
                        decline_signals += 1
            except Exception:
                pass

            # Overall verdict
            improved = improvement_signals > decline_signals and improvement_signals > 0

            self.db.execute(
                "UPDATE product_iterations SET performance_after = ? WHERE id = ?",
                (json.dumps(after_data, default=str), it["id"]),
            )

            result = {
                "strategy": it["strategy"],
                "product": it["product_name"],
                "iteration": it["iteration_number"],
                "before": before,
                "after": after_data,
                "improved": improved,
                "signals": {"improvement": improvement_signals, "decline": decline_signals},
                "sample_size": review_count,
            }
            results.append(result)

            if improved:
                self.log_action(
                    "iteration_success",
                    f"{it['strategy']}/{it['product_name']}: "
                    f"quality {before_score:.2f}→{after_data.get('quality_score', '?')}, "
                    f"+{improvement_signals} signals, {review_count} reviews",
                )
            else:
                self.log_action(
                    "iteration_stale",
                    f"{it['strategy']}/{it['product_name']}: "
                    f"no improvement ({improvement_signals}↑ vs {decline_signals}↓ signals)",
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
