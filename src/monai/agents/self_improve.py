"""Agent self-improvement framework.

Agents can improve themselves by:
1. Analyzing their own performance metrics
2. Identifying weaknesses and failure patterns
3. Generating improved strategies/prompts
4. Testing improvements against ethics and quality checks
5. Deploying improvements only if they pass all checks

Constraints:
- Ethics are NEVER relaxed — improvements must pass ethics tests
- Cost must stay within budget
- Changes are logged and reversible
- The orchestrator must approve major changes
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

SELF_IMPROVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_improvements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    improvement_type TEXT NOT NULL,   -- prompt, strategy, tool, workflow
    description TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    performance_before TEXT,         -- JSON metrics
    performance_after TEXT,          -- JSON metrics (null until verified)
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed, testing, approved, deployed, reverted
    ethics_passed INTEGER,           -- null until tested
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deployed_at TIMESTAMP
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


class SelfImprover:
    """Manages agent self-improvement within ethical and budgetary constraints."""

    def __init__(self, config: Config, db: Database, llm: LLM):
        self.config = config
        self.db = db
        self.llm = llm

        with db.connect() as conn:
            conn.executescript(SELF_IMPROVE_SCHEMA)

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
        # Get metrics ordered chronologically (oldest first)
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
        # Get all metrics for this agent
        all_metrics = self.db.execute(
            "SELECT metric_name, AVG(metric_value) as avg_val, "
            "MIN(metric_value) as min_val, MAX(metric_value) as max_val, "
            "COUNT(*) as count "
            "FROM agent_metrics WHERE agent_name = ? GROUP BY metric_name",
            (agent_name,),
        )

        # Get recent failures from agent log
        try:
            failures = self.db.execute(
                "SELECT action, details, created_at FROM agent_log "
                "WHERE agent_name = ? AND (action LIKE '%error%' OR action LIKE '%fail%') "
                "ORDER BY created_at DESC LIMIT 10",
                (agent_name,),
            )
        except Exception:
            failures = []

        # Get lessons learned (table may not exist if SharedMemory hasn't been initialized)
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

        return {
            "agent": agent_name,
            "metrics": metrics_summary,
            "failure_patterns": failure_patterns,
            "lessons": lesson_list,
            "data_richness": "good" if len(all_metrics) >= 3 else "sparse",
        }

    # ── Improvement Proposals ─────────────────────────────────────

    def propose_improvement(self, agent_name: str, improvement_type: str,
                            description: str, old_value: str = "",
                            new_value: str = "",
                            performance_before: dict | None = None) -> int:
        """Record a proposed improvement for an agent."""
        return self.db.execute_insert(
            "INSERT INTO agent_improvements "
            "(agent_name, improvement_type, description, old_value, new_value, "
            "performance_before, status) VALUES (?, ?, ?, ?, ?, ?, 'proposed')",
            (agent_name, improvement_type, description, old_value, new_value,
             json.dumps(performance_before or {})),
        )

    def generate_improvements(self, agent_name: str) -> list[dict[str, Any]]:
        """Use LLM to analyze performance and suggest improvements."""
        analysis = self.analyze_performance(agent_name)

        if analysis["data_richness"] == "sparse":
            return []  # Not enough data to improve on

        response = self.llm.chat_json(
            [
                {"role": "system", "content": (
                    "You are a performance optimization expert. Analyze agent performance "
                    "data and suggest concrete improvements. Each suggestion must be:\n"
                    "1. Specific and actionable\n"
                    "2. Measurable (how to verify improvement)\n"
                    "3. Within ethical bounds (NEVER suggest relaxing ethics)\n"
                    "4. Cost-conscious (prefer free/cheap improvements)\n"
                    "Return: {\"improvements\": [{\"type\": str, \"description\": str, "
                    "\"expected_impact\": str, \"risk\": str}]}"
                )},
                {"role": "user", "content": json.dumps(analysis, default=str)},
            ],
            temperature=0.5,
        )

        improvements = response.get("improvements", [])

        # Record each proposal
        for imp in improvements:
            self.propose_improvement(
                agent_name,
                imp.get("type", "strategy"),
                imp.get("description", ""),
                performance_before=analysis.get("metrics"),
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
