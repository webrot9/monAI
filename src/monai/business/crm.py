"""CRM module — manages contacts, leads, and client pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from monai.db.database import Database

STAGES = ("lead", "prospect", "contacted", "negotiating", "client", "churned")


class CRM:
    def __init__(self, db: Database):
        self.db = db

    def add_contact(self, name: str, email: str = "", company: str = "",
                    platform: str = "", platform_id: str = "",
                    source_strategy: str = "", notes: str = "") -> int:
        return self.db.execute_insert(
            "INSERT INTO contacts (name, email, company, platform, platform_id, "
            "source_strategy, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, email, company, platform, platform_id, source_strategy, notes),
        )

    def update_stage(self, contact_id: int, stage: str):
        if stage not in STAGES:
            raise ValueError(f"Invalid stage: {stage}. Must be one of {STAGES}")
        self.db.execute(
            "UPDATE contacts SET stage = ?, updated_at = ? WHERE id = ?",
            (stage, datetime.now().isoformat(), contact_id),
        )

    def get_contacts_by_stage(self, stage: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM contacts WHERE stage = ? ORDER BY updated_at DESC", (stage,)
        )
        return [dict(r) for r in rows]

    def get_contact(self, contact_id: int) -> dict[str, Any] | None:
        rows = self.db.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,))
        return dict(rows[0]) if rows else None

    def search_contacts(self, query: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM contacts WHERE name LIKE ? OR email LIKE ? OR company LIKE ?",
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        )
        return [dict(r) for r in rows]

    def get_pipeline_summary(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT stage, COUNT(*) as count FROM contacts GROUP BY stage"
        )
        return {row["stage"]: row["count"] for row in rows}
