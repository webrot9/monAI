"""Email Marketing — per-brand campaigns, sequences, and list management.

Builds on the existing comms.py SMTP layer. Adds:
- Subscriber lists per brand
- Email campaigns (broadcast)
- Drip sequences (automated follow-ups)
- Open/click tracking
- Unsubscribe management
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

EMAIL_MARKETING_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    email TEXT NOT NULL,
    name TEXT,
    source TEXT,                        -- website, social, referral, import
    lead_id INTEGER,                    -- references pipeline_leads(id)
    tags TEXT,                          -- JSON list of tags for segmentation
    status TEXT DEFAULT 'active',       -- active, unsubscribed, bounced, complained
    subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    unsubscribed_at TIMESTAMP,
    UNIQUE(brand, email)
);

CREATE TABLE IF NOT EXISTS email_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_html TEXT,
    body_text TEXT,
    from_name TEXT,
    from_email TEXT,
    segment_tags TEXT,                  -- JSON: only send to subscribers with these tags
    status TEXT DEFAULT 'draft',        -- draft, scheduled, sending, sent, cancelled
    scheduled_for TIMESTAMP,
    sent_count INTEGER DEFAULT 0,
    open_count INTEGER DEFAULT 0,
    click_count INTEGER DEFAULT 0,
    unsubscribe_count INTEGER DEFAULT 0,
    bounce_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    name TEXT NOT NULL,                 -- e.g. "welcome_series", "nurture_7day"
    trigger_event TEXT,                 -- what starts the sequence: subscribe, purchase, tag_added
    status TEXT DEFAULT 'active',       -- active, paused, archived
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_sequence_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL REFERENCES email_sequences(id),
    step_number INTEGER NOT NULL,
    delay_hours INTEGER DEFAULT 0,     -- hours after previous step (or trigger)
    subject TEXT NOT NULL,
    body_html TEXT,
    body_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES email_subscribers(id),
    campaign_id INTEGER,               -- NULL if from sequence
    sequence_step_id INTEGER,          -- NULL if from campaign
    subject TEXT NOT NULL,
    status TEXT DEFAULT 'queued',       -- queued, sent, opened, clicked, bounced, failed
    opened_at TIMESTAMP,
    clicked_at TIMESTAMP,
    sent_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class EmailMarketing:
    """Per-brand email marketing: lists, campaigns, sequences, tracking."""

    def __init__(self, db: Database):
        self.db = db
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(EMAIL_MARKETING_SCHEMA)

    # ── Subscriber Management ────────────────────────────────

    def add_subscriber(self, brand: str, email: str, name: str = "",
                       source: str = "", lead_id: int | None = None,
                       tags: list[str] | None = None) -> int:
        """Add a subscriber to a brand's list."""
        return self.db.execute_insert(
            "INSERT OR IGNORE INTO email_subscribers "
            "(brand, email, name, source, lead_id, tags) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (brand, email, name, source, lead_id,
             json.dumps(tags) if tags else None),
        )

    def unsubscribe(self, brand: str, email: str) -> dict[str, Any]:
        """Unsubscribe an email from a brand's list."""
        self.db.execute(
            "UPDATE email_subscribers SET status = 'unsubscribed', "
            "unsubscribed_at = CURRENT_TIMESTAMP "
            "WHERE brand = ? AND email = ?",
            (brand, email),
        )
        return {"status": "unsubscribed", "brand": brand, "email": email}

    def get_active_subscribers(self, brand: str,
                               tags: list[str] | None = None) -> list[dict[str, Any]]:
        """Get active subscribers, optionally filtered by tags."""
        query = (
            "SELECT * FROM email_subscribers "
            "WHERE brand = ? AND status = 'active'"
        )
        params: list = [brand]

        if tags:
            # Filter by any matching tag
            tag_conditions = []
            for tag in tags:
                tag_conditions.append("tags LIKE ?")
                params.append(f"%{tag}%")
            query += " AND (" + " OR ".join(tag_conditions) + ")"

        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def get_subscriber_count(self, brand: str) -> dict[str, int]:
        """Count subscribers by status for a brand."""
        rows = self.db.execute(
            "SELECT status, COUNT(*) as count "
            "FROM email_subscribers WHERE brand = ? GROUP BY status",
            (brand,),
        )
        return {r["status"]: r["count"] for r in rows}

    def tag_subscriber(self, brand: str, email: str,
                       new_tags: list[str]) -> None:
        """Add tags to a subscriber."""
        rows = self.db.execute(
            "SELECT tags FROM email_subscribers "
            "WHERE brand = ? AND email = ?",
            (brand, email),
        )
        if not rows:
            return

        existing = json.loads(rows[0]["tags"] or "[]")
        merged = list(set(existing + new_tags))
        self.db.execute(
            "UPDATE email_subscribers SET tags = ? "
            "WHERE brand = ? AND email = ?",
            (json.dumps(merged), brand, email),
        )

    # ── Campaigns ────────────────────────────────────────────

    def create_campaign(self, brand: str, name: str, subject: str,
                        body_html: str = "", body_text: str = "",
                        from_name: str = "", from_email: str = "",
                        segment_tags: list[str] | None = None) -> int:
        """Create an email campaign."""
        return self.db.execute_insert(
            "INSERT INTO email_campaigns "
            "(brand, name, subject, body_html, body_text, "
            "from_name, from_email, segment_tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (brand, name, subject, body_html, body_text,
             from_name, from_email,
             json.dumps(segment_tags) if segment_tags else None),
        )

    def schedule_campaign(self, campaign_id: int,
                          scheduled_for: str | None = None) -> dict[str, Any]:
        """Schedule a campaign for sending."""
        schedule_time = scheduled_for or datetime.now().isoformat()
        self.db.execute(
            "UPDATE email_campaigns SET status = 'scheduled', "
            "scheduled_for = ? WHERE id = ?",
            (schedule_time, campaign_id),
        )
        return {"status": "scheduled", "campaign_id": campaign_id}

    def queue_campaign_sends(self, campaign_id: int) -> int:
        """Create send records for all matching subscribers."""
        rows = self.db.execute(
            "SELECT * FROM email_campaigns WHERE id = ?", (campaign_id,)
        )
        if not rows:
            return 0

        campaign = dict(rows[0])
        segment_tags = json.loads(campaign["segment_tags"] or "[]")

        subscribers = self.get_active_subscribers(
            campaign["brand"], tags=segment_tags if segment_tags else None
        )

        queued = 0
        for sub in subscribers:
            self.db.execute_insert(
                "INSERT INTO email_sends "
                "(subscriber_id, campaign_id, subject, status) "
                "VALUES (?, ?, ?, 'queued')",
                (sub["id"], campaign_id, campaign["subject"]),
            )
            queued += 1

        self.db.execute(
            "UPDATE email_campaigns SET status = 'sending', "
            "sent_count = ? WHERE id = ?",
            (queued, campaign_id),
        )
        return queued

    def mark_send_sent(self, send_id: int) -> None:
        """Mark an email send as sent."""
        self.db.execute(
            "UPDATE email_sends SET status = 'sent', "
            "sent_at = CURRENT_TIMESTAMP WHERE id = ?",
            (send_id,),
        )

    def mark_send_opened(self, send_id: int) -> None:
        """Record an email open."""
        self.db.execute(
            "UPDATE email_sends SET status = 'opened', "
            "opened_at = CURRENT_TIMESTAMP WHERE id = ?",
            (send_id,),
        )

    def mark_send_clicked(self, send_id: int) -> None:
        """Record an email click."""
        self.db.execute(
            "UPDATE email_sends SET status = 'clicked', "
            "clicked_at = CURRENT_TIMESTAMP WHERE id = ?",
            (send_id,),
        )

    def get_campaign_stats(self, campaign_id: int) -> dict[str, Any]:
        """Get stats for a campaign."""
        rows = self.db.execute(
            "SELECT status, COUNT(*) as count "
            "FROM email_sends WHERE campaign_id = ? GROUP BY status",
            (campaign_id,),
        )
        stats = {r["status"]: r["count"] for r in rows}
        total = sum(stats.values())
        return {
            "total": total,
            "sent": stats.get("sent", 0) + stats.get("opened", 0) + stats.get("clicked", 0),
            "opened": stats.get("opened", 0) + stats.get("clicked", 0),
            "clicked": stats.get("clicked", 0),
            "bounced": stats.get("bounced", 0),
            "open_rate": (stats.get("opened", 0) + stats.get("clicked", 0)) / total if total else 0,
            "click_rate": stats.get("clicked", 0) / total if total else 0,
        }

    # ── Sequences ────────────────────────────────────────────

    def create_sequence(self, brand: str, name: str,
                        trigger_event: str = "subscribe") -> int:
        """Create an automated email sequence."""
        return self.db.execute_insert(
            "INSERT INTO email_sequences (brand, name, trigger_event) "
            "VALUES (?, ?, ?)",
            (brand, name, trigger_event),
        )

    def add_sequence_step(self, sequence_id: int, step_number: int,
                          delay_hours: int, subject: str,
                          body_html: str = "",
                          body_text: str = "") -> int:
        """Add a step to a sequence."""
        return self.db.execute_insert(
            "INSERT INTO email_sequence_steps "
            "(sequence_id, step_number, delay_hours, subject, "
            "body_html, body_text) VALUES (?, ?, ?, ?, ?, ?)",
            (sequence_id, step_number, delay_hours, subject,
             body_html, body_text),
        )

    def get_sequence_steps(self, sequence_id: int) -> list[dict[str, Any]]:
        """Get all steps in a sequence."""
        rows = self.db.execute(
            "SELECT * FROM email_sequence_steps "
            "WHERE sequence_id = ? ORDER BY step_number",
            (sequence_id,),
        )
        return [dict(r) for r in rows]

    def get_pending_sequence_sends(self, brand: str) -> list[dict[str, Any]]:
        """Get queued sequence emails ready to send."""
        rows = self.db.execute(
            "SELECT es.*, ss.subject, ss.body_html, ss.body_text, "
            "sub.email, sub.name "
            "FROM email_sends es "
            "JOIN email_sequence_steps ss ON es.sequence_step_id = ss.id "
            "JOIN email_subscribers sub ON es.subscriber_id = sub.id "
            "WHERE es.status = 'queued' AND sub.brand = ?",
            (brand,),
        )
        return [dict(r) for r in rows]

    # ── Cross-Brand Analytics ────────────────────────────────

    def get_all_brands_stats(self) -> list[dict[str, Any]]:
        """Email marketing stats across all brands."""
        rows = self.db.execute(
            "SELECT s.brand, "
            "COUNT(DISTINCT s.id) as subscribers, "
            "COUNT(DISTINCT CASE WHEN s.status = 'active' THEN s.id END) as active, "
            "COUNT(DISTINCT c.id) as campaigns "
            "FROM email_subscribers s "
            "LEFT JOIN email_campaigns c ON s.brand = c.brand "
            "GROUP BY s.brand"
        )
        return [dict(r) for r in rows]
