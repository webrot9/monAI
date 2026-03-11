"""Audit trail — queryable log of all agent actions.

Every significant action in the system is logged here with:
  - Who (agent_name)
  - What (action type + description)
  - When (timestamp)
  - Context (entity, brand, strategy, risk level)
  - Outcome (success/failure, details)

Supports querying by agent, action type, time range, brand, and risk level.
Integrates with the existing agent_log table (extended with new columns).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_trail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    action_type TEXT NOT NULL,       -- api_call, payment, content, config, deploy, system
    action TEXT NOT NULL,            -- specific action description
    details TEXT,                    -- JSON context/parameters
    result TEXT,                     -- outcome description
    success INTEGER DEFAULT 1,      -- 1=success, 0=failure
    brand TEXT,
    strategy_id INTEGER,
    risk_level TEXT DEFAULT 'low',   -- low, medium, high, critical
    metadata TEXT,                   -- additional JSON data
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_trail(agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_type ON audit_trail(action_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_brand ON audit_trail(brand, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_risk ON audit_trail(risk_level, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_trail(created_at DESC);
"""

# Actions that are considered high-risk and should trigger alerts
HIGH_RISK_ACTIONS = {
    "payment_sent", "fund_transfer", "account_created", "api_key_provisioned",
    "llc_formation", "contract_signed", "config_changed", "deploy_production",
}


class AuditTrail:
    """Queryable audit log for all agent actions.

    Every significant system event is recorded with enough context
    to reconstruct what happened and why.
    """

    def __init__(self, db: Database):
        self.db = db
        self._init_schema()

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(AUDIT_SCHEMA)

    def log(
        self,
        agent_name: str,
        action_type: str,
        action: str,
        *,
        details: dict[str, Any] | str | None = None,
        result: str = "",
        success: bool = True,
        brand: str = "",
        strategy_id: int | None = None,
        risk_level: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Record an action in the audit trail.

        Args:
            agent_name: Which agent performed the action.
            action_type: Category (api_call, payment, content, config, deploy, system).
            action: Specific action description.
            details: Parameters/context (dict or string).
            result: Outcome description.
            success: Whether the action succeeded.
            brand: Brand this relates to.
            strategy_id: Strategy ID if applicable.
            risk_level: Override risk level. Auto-detected if None.
            metadata: Additional structured data.

        Returns:
            Audit entry ID.
        """
        if risk_level is None:
            risk_level = self._assess_risk(action_type, action)

        details_str = json.dumps(details) if isinstance(details, dict) else (details or "")
        metadata_str = json.dumps(metadata) if metadata else None

        entry_id = self.db.execute_insert(
            "INSERT INTO audit_trail "
            "(agent_name, action_type, action, details, result, success, "
            "brand, strategy_id, risk_level, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_name, action_type, action, details_str, result,
                1 if success else 0, brand, strategy_id, risk_level,
                metadata_str,
            ),
        )

        if risk_level in ("high", "critical"):
            logger.warning(
                f"AUDIT [{risk_level.upper()}]: {agent_name} → {action_type}/{action}"
            )

        return entry_id

    def _assess_risk(self, action_type: str, action: str) -> str:
        """Auto-detect risk level based on action type and name."""
        action_lower = action.lower()
        if action_lower in HIGH_RISK_ACTIONS or action_type == "payment":
            return "high"
        if action_type in ("deploy", "config"):
            return "medium"
        return "low"

    # ── Queries ────────────────────────────────────────────────

    def get_recent(self, limit: int = 50,
                   agent_name: str | None = None,
                   action_type: str | None = None,
                   brand: str | None = None,
                   risk_level: str | None = None,
                   success: bool | None = None) -> list[dict[str, Any]]:
        """Query recent audit entries with optional filters."""
        query = "SELECT * FROM audit_trail WHERE 1=1"
        params: list[Any] = []

        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        if action_type:
            query += " AND action_type = ?"
            params.append(action_type)
        if brand:
            query += " AND brand = ?"
            params.append(brand)
        if risk_level:
            query += " AND risk_level = ?"
            params.append(risk_level)
        if success is not None:
            query += " AND success = ?"
            params.append(1 if success else 0)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def get_by_date_range(self, start: str, end: str,
                          **filters: Any) -> list[dict[str, Any]]:
        """Get entries within a date range."""
        query = (
            "SELECT * FROM audit_trail "
            "WHERE DATE(created_at) >= ? AND DATE(created_at) <= ?"
        )
        params: list[Any] = [start, end]

        for key in ("agent_name", "action_type", "brand", "risk_level"):
            if key in filters and filters[key]:
                query += f" AND {key} = ?"
                params.append(filters[key])

        query += " ORDER BY created_at DESC"
        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def get_agent_summary(self, days: int = 7) -> list[dict[str, Any]]:
        """Per-agent action counts for the last N days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT agent_name, action_type, "
            "COUNT(*) as total, "
            "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes, "
            "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failures "
            "FROM audit_trail WHERE created_at >= ? "
            "GROUP BY agent_name, action_type "
            "ORDER BY total DESC",
            (cutoff,),
        )
        return [dict(r) for r in rows]

    def get_high_risk_entries(self, days: int = 7) -> list[dict[str, Any]]:
        """Get high/critical risk entries from the last N days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT * FROM audit_trail "
            "WHERE risk_level IN ('high', 'critical') AND created_at >= ? "
            "ORDER BY created_at DESC",
            (cutoff,),
        )
        return [dict(r) for r in rows]

    def get_failures(self, days: int = 7,
                     limit: int = 50) -> list[dict[str, Any]]:
        """Get failed actions from the last N days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT * FROM audit_trail "
            "WHERE success = 0 AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        return [dict(r) for r in rows]

    def count_actions(self, agent_name: str | None = None,
                      days: int | None = None) -> int:
        """Count total audit entries, optionally filtered."""
        query = "SELECT COUNT(*) as cnt FROM audit_trail WHERE 1=1"
        params: list[Any] = []
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        if days:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            query += " AND created_at >= ?"
            params.append(cutoff)
        rows = self.db.execute(query, tuple(params))
        return rows[0]["cnt"]

    def format_telegram_report(self, days: int = 7) -> str:
        """Format audit summary for Telegram notification."""
        summary = self.get_agent_summary(days)
        high_risk = self.get_high_risk_entries(days)
        failures = self.get_failures(days)

        total = sum(s["total"] for s in summary)
        total_failures = sum(s["failures"] for s in summary)

        lines = [
            f"*Audit Report (last {days}d)*",
            "```",
            f"Total actions:    {total}",
            f"Failures:         {total_failures}",
            f"High-risk events: {len(high_risk)}",
            "```",
        ]

        if summary:
            lines.append("\n*Top Agents:*")
            for s in summary[:5]:
                status = f"({s['failures']} fails)" if s["failures"] else ""
                lines.append(
                    f"- {s['agent_name']}/{s['action_type']}: "
                    f"{s['total']} actions {status}"
                )

        if high_risk:
            lines.append(f"\n*High-Risk Events ({len(high_risk)}):*")
            for e in high_risk[:5]:
                lines.append(
                    f"- [{e['risk_level'].upper()}] {e['agent_name']}: {e['action']}"
                )

        return "\n".join(lines)
