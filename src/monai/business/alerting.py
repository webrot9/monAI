"""Alerting Rules Engine — configurable threshold-based alerts.

Evaluates rules against live metrics and fires alerts via Telegram
(or other channels). Supports deduplication, cooldowns, and severity
levels to prevent alert fatigue.

Rules are stored in SQLite so they persist across restarts and can be
managed via the dashboard API.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

ALERTING_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    metric TEXT NOT NULL,
    operator TEXT NOT NULL,          -- gt, lt, gte, lte, eq, neq
    threshold REAL NOT NULL,
    severity TEXT DEFAULT 'warning', -- info, warning, critical
    cooldown_minutes INTEGER DEFAULT 60,
    enabled INTEGER DEFAULT 1,
    message_template TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alert_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL REFERENCES alert_rules(id),
    rule_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    metric_value REAL,
    threshold REAL,
    message TEXT,
    delivered INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_alert_history_rule
    ON alert_history(rule_id, created_at);
CREATE INDEX IF NOT EXISTS idx_alert_history_severity
    ON alert_history(severity, created_at);
"""

# Operator mapping
_OPS = {
    "gt": lambda v, t: v > t,
    "lt": lambda v, t: v < t,
    "gte": lambda v, t: v >= t,
    "lte": lambda v, t: v <= t,
    "eq": lambda v, t: v == t,
    "neq": lambda v, t: v != t,
}

# Default rules installed on first run
_DEFAULT_RULES = [
    {
        "name": "budget_critical",
        "metric": "budget.balance",
        "operator": "lte",
        "threshold": 0,
        "severity": "critical",
        "cooldown_minutes": 120,
        "message_template": "BUDGET EXHAUSTED — balance is €{value:.2f}",
    },
    {
        "name": "budget_low",
        "metric": "budget.days_until_broke",
        "operator": "lt",
        "threshold": 3,
        "severity": "warning",
        "cooldown_minutes": 360,
        "message_template": "Low budget: {value:.0f} days remaining",
    },
    {
        "name": "daily_revenue",
        "metric": "today.revenue",
        "operator": "gt",
        "threshold": 0,
        "severity": "info",
        "cooldown_minutes": 1440,
        "message_template": "Revenue today: €{value:.2f}",
    },
    {
        "name": "high_risk_audit",
        "metric": "audit.high_risk_count",
        "operator": "gt",
        "threshold": 0,
        "severity": "warning",
        "cooldown_minutes": 60,
        "message_template": "{value:.0f} high-risk audit events in last hour",
    },
    {
        "name": "reconciliation_mismatch",
        "metric": "reconciliation.unmatched_gl",
        "operator": "gt",
        "threshold": 0,
        "severity": "warning",
        "cooldown_minutes": 1440,
        "message_template": "{value:.0f} unmatched GL entries in reconciliation",
    },
]


class AlertingEngine:
    """Evaluates alert rules against live metrics and fires notifications."""

    def __init__(self, db: Database):
        self.db = db
        self._init_schema()
        self._install_defaults()

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(ALERTING_SCHEMA)

    def _install_defaults(self) -> None:
        """Install default rules if none exist."""
        rows = self.db.execute("SELECT COUNT(*) as cnt FROM alert_rules")
        if rows[0]["cnt"] > 0:
            return
        for rule in _DEFAULT_RULES:
            self.add_rule(**rule)

    # ── Rule Management ──────────────────────────────────────────

    def add_rule(
        self,
        name: str,
        metric: str,
        operator: str,
        threshold: float,
        severity: str = "warning",
        cooldown_minutes: int = 60,
        message_template: str = "",
    ) -> int:
        """Add a new alert rule. Returns the rule ID."""
        if operator not in _OPS:
            raise ValueError(f"Unknown operator: {operator}. Use: {list(_OPS.keys())}")
        return self.db.execute_insert(
            "INSERT INTO alert_rules "
            "(name, metric, operator, threshold, severity, cooldown_minutes, "
            "message_template, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (name, metric, operator, threshold, severity, cooldown_minutes,
             message_template),
        )

    def update_rule(self, rule_id: int, **kwargs: Any) -> None:
        """Update fields on an existing rule."""
        allowed = {"name", "metric", "operator", "threshold", "severity",
                    "cooldown_minutes", "message_template", "enabled"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        self.db.execute(
            f"UPDATE alert_rules SET {set_clause} WHERE id = ?",
            (*updates.values(), rule_id),
        )

    def delete_rule(self, rule_id: int) -> None:
        self.db.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))

    def get_rules(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM alert_rules ORDER BY severity, name")
        return [dict(r) for r in rows]

    # ── Metric Collection ────────────────────────────────────────

    def collect_metrics(self, dashboard_data: dict[str, Any]) -> dict[str, float]:
        """Extract numeric metrics from dashboard data for rule evaluation.

        Supports dotted paths like 'budget.balance', 'today.revenue'.
        """
        metrics: dict[str, float] = {}

        def _extract(obj: Any, prefix: str = "") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    key = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, (int, float)) and v == v:  # skip NaN
                        metrics[key] = float(v)
                    elif isinstance(v, dict):
                        _extract(v, key)

        _extract(dashboard_data)

        # Derived metrics from audit trail
        try:
            rows = self.db.execute(
                "SELECT COUNT(*) as cnt FROM audit_trail "
                "WHERE risk_level IN ('high', 'critical') "
                "AND created_at >= datetime('now', '-1 hour')"
            )
            metrics["audit.high_risk_count"] = float(rows[0]["cnt"]) if rows else 0.0
        except Exception:
            pass

        # Reconciliation metrics
        try:
            rows = self.db.execute(
                "SELECT unmatched_gl, unmatched_webhooks, amount_mismatches "
                "FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
            )
            if rows:
                r = rows[0]
                metrics["reconciliation.unmatched_gl"] = float(r["unmatched_gl"])
                metrics["reconciliation.unmatched_webhooks"] = float(r["unmatched_webhooks"])
                metrics["reconciliation.amount_mismatches"] = float(r["amount_mismatches"])
        except Exception:
            pass

        return metrics

    # ── Rule Evaluation ──────────────────────────────────────────

    def evaluate(self, dashboard_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Evaluate all enabled rules against current metrics.

        Returns list of fired alerts (already deduped by cooldown).
        """
        metrics = self.collect_metrics(dashboard_data)
        rules = self.db.execute(
            "SELECT * FROM alert_rules WHERE enabled = 1"
        )

        fired: list[dict[str, Any]] = []
        for rule in rules:
            rule = dict(rule)
            metric_key = rule["metric"]
            if metric_key not in metrics:
                continue

            value = metrics[metric_key]
            op_fn = _OPS.get(rule["operator"])
            if not op_fn:
                continue

            if not op_fn(value, rule["threshold"]):
                continue

            # Check cooldown
            if self._in_cooldown(rule["id"], rule["cooldown_minutes"]):
                continue

            # Build message
            template = rule.get("message_template") or (
                f"{rule['name']}: {metric_key} = {{value:.2f}} "
                f"(threshold: {{threshold:.2f}})"
            )
            try:
                message = template.format(
                    value=value,
                    threshold=rule["threshold"],
                    metric=metric_key,
                    name=rule["name"],
                )
            except (KeyError, ValueError):
                message = f"{rule['name']}: {metric_key} = {value}"

            alert = {
                "rule_id": rule["id"],
                "rule_name": rule["name"],
                "severity": rule["severity"],
                "metric": metric_key,
                "metric_value": value,
                "threshold": rule["threshold"],
                "message": message,
            }
            fired.append(alert)

            # Record in history
            self.db.execute_insert(
                "INSERT INTO alert_history "
                "(rule_id, rule_name, severity, metric_value, threshold, message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rule["id"], rule["name"], rule["severity"],
                 value, rule["threshold"], message),
            )

        return fired

    def _in_cooldown(self, rule_id: int, cooldown_minutes: int) -> bool:
        """Check if a rule fired recently (within cooldown window)."""
        rows = self.db.execute(
            "SELECT created_at FROM alert_history "
            "WHERE rule_id = ? "
            "AND created_at >= datetime('now', ?)"
            "ORDER BY created_at DESC LIMIT 1",
            (rule_id, f"-{cooldown_minutes} minutes"),
        )
        return len(rows) > 0

    # ── Alert History ────────────────────────────────────────────

    def get_recent_alerts(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM alert_history ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def mark_delivered(self, alert_id: int) -> None:
        self.db.execute(
            "UPDATE alert_history SET delivered = 1 WHERE id = ?",
            (alert_id,),
        )

    def get_summary(self, days: int = 7) -> dict[str, Any]:
        """Alert summary for the last N days."""
        rows = self.db.execute(
            "SELECT severity, COUNT(*) as cnt FROM alert_history "
            "WHERE created_at >= datetime('now', ?) GROUP BY severity",
            (f"-{days} days",),
        )
        by_severity = {r["severity"]: r["cnt"] for r in rows}

        total = self.db.execute(
            "SELECT COUNT(*) as cnt FROM alert_history "
            "WHERE created_at >= datetime('now', ?)",
            (f"-{days} days",),
        )

        return {
            "total": total[0]["cnt"] if total else 0,
            "by_severity": by_severity,
            "days": days,
        }
