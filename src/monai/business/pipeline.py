"""Conversion Pipeline — tracks leads from discovery to payment per brand.

Extends the basic CRM with:
- Brand-aware lead sources (social post, website, email, referral)
- Full funnel: impression → click → lead → prospect → customer → repeat
- Revenue attribution per brand, platform, and content piece
- Conversion rate tracking and optimization signals
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

PIPELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    contact_id INTEGER REFERENCES contacts(id),
    source_platform TEXT,              -- twitter, linkedin, reddit, website, email
    source_post_id INTEGER,            -- references brand_social_posts(id) if from social
    source_url TEXT,                    -- landing page or post URL that brought them
    source_campaign TEXT,               -- email campaign or ad campaign name
    stage TEXT DEFAULT 'impression',    -- impression, click, lead, prospect, customer, repeat, lost
    email TEXT,
    name TEXT,
    score INTEGER DEFAULT 0,           -- lead score (0-100) based on engagement signals
    notes TEXT,
    first_touch_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_touch_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    converted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES pipeline_leads(id),
    event_type TEXT NOT NULL,          -- page_view, email_open, email_click, form_submit,
                                       -- social_engage, purchase, refund
    details TEXT,                       -- JSON: event-specific data
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_revenue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES pipeline_leads(id),
    brand TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    product TEXT,                       -- what they bought (service name, product name)
    payment_method TEXT,                -- stripe, paypal, crypto, platform_payout
    payment_reference TEXT,             -- transaction ID
    status TEXT DEFAULT 'pending',      -- pending, completed, refunded
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

STAGES = ("impression", "click", "lead", "prospect", "customer", "repeat", "lost")


class Pipeline:
    """Brand-aware conversion pipeline — tracks the full journey."""

    def __init__(self, db: Database):
        self.db = db
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(PIPELINE_SCHEMA)

    # ── Lead Management ──────────────────────────────────────

    def create_lead(self, brand: str, source_platform: str = "",
                    source_post_id: int | None = None,
                    source_url: str = "", source_campaign: str = "",
                    email: str = "", name: str = "",
                    contact_id: int | None = None) -> int:
        """Create a new lead in the pipeline."""
        return self.db.execute_insert(
            "INSERT INTO pipeline_leads "
            "(brand, contact_id, source_platform, source_post_id, "
            "source_url, source_campaign, email, name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (brand, contact_id, source_platform, source_post_id,
             source_url, source_campaign, email, name),
        )

    def advance_stage(self, lead_id: int, new_stage: str) -> dict[str, Any]:
        """Move a lead to the next stage."""
        if new_stage not in STAGES:
            return {"status": "invalid_stage", "valid": STAGES}

        rows = self.db.execute(
            "SELECT stage FROM pipeline_leads WHERE id = ?", (lead_id,)
        )
        if not rows:
            return {"status": "not_found"}

        old_stage = rows[0]["stage"]
        now = datetime.now().isoformat()

        if new_stage == "customer" and old_stage != "customer":
            self.db.execute(
                "UPDATE pipeline_leads SET stage = ?, last_touch_at = ?, "
                "converted_at = ? WHERE id = ?",
                (new_stage, now, now, lead_id),
            )
        else:
            self.db.execute(
                "UPDATE pipeline_leads SET stage = ?, last_touch_at = ? WHERE id = ?",
                (new_stage, now, lead_id),
            )

        self.log_event(lead_id, "stage_change",
                       {"from": old_stage, "to": new_stage})

        return {"status": "advanced", "from": old_stage, "to": new_stage}

    def score_lead(self, lead_id: int, score: int) -> None:
        """Update lead score (0-100)."""
        self.db.execute(
            "UPDATE pipeline_leads SET score = ?, last_touch_at = ? WHERE id = ?",
            (min(100, max(0, score)), datetime.now().isoformat(), lead_id),
        )

    def get_lead(self, lead_id: int) -> dict[str, Any] | None:
        rows = self.db.execute(
            "SELECT * FROM pipeline_leads WHERE id = ?", (lead_id,)
        )
        return dict(rows[0]) if rows else None

    def get_leads_by_brand(self, brand: str,
                           stage: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM pipeline_leads WHERE brand = ?"
        params: list = [brand]
        if stage:
            query += " AND stage = ?"
            params.append(stage)
        query += " ORDER BY last_touch_at DESC"
        return [dict(r) for r in self.db.execute(query, tuple(params))]

    def get_hot_leads(self, brand: str | None = None,
                      min_score: int = 50) -> list[dict[str, Any]]:
        """Get leads with high scores, optionally filtered by brand."""
        query = (
            "SELECT * FROM pipeline_leads "
            "WHERE score >= ? AND stage NOT IN ('customer', 'lost') "
        )
        params: list = [min_score]
        if brand:
            query += "AND brand = ? "
            params.append(brand)
        query += "ORDER BY score DESC"
        return [dict(r) for r in self.db.execute(query, tuple(params))]

    # ── Events ───────────────────────────────────────────────

    def log_event(self, lead_id: int, event_type: str,
                  details: dict | None = None) -> int:
        """Log a pipeline event (page view, email open, purchase, etc.)."""
        return self.db.execute_insert(
            "INSERT INTO pipeline_events (lead_id, event_type, details) "
            "VALUES (?, ?, ?)",
            (lead_id, event_type, json.dumps(details) if details else None),
        )

    def get_events(self, lead_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM pipeline_events WHERE lead_id = ? "
            "ORDER BY created_at DESC",
            (lead_id,),
        )
        return [dict(r) for r in rows]

    # ── Revenue Attribution ──────────────────────────────────

    def record_revenue(self, lead_id: int, brand: str, amount: float,
                       product: str = "", payment_method: str = "",
                       payment_reference: str = "",
                       currency: str = "EUR") -> int:
        """Record revenue attributed to a lead."""
        rev_id = self.db.execute_insert(
            "INSERT INTO pipeline_revenue "
            "(lead_id, brand, amount, currency, product, "
            "payment_method, payment_reference) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lead_id, brand, amount, currency, product,
             payment_method, payment_reference),
        )

        # Auto-advance to customer if not already
        lead = self.get_lead(lead_id)
        if lead and lead["stage"] not in ("customer", "repeat"):
            self.advance_stage(lead_id, "customer")

        self.log_event(lead_id, "purchase", {
            "amount": amount, "product": product,
            "payment_method": payment_method,
        })

        return rev_id

    def get_revenue_by_brand(self, brand: str) -> dict[str, Any]:
        """Total revenue and count per brand."""
        rows = self.db.execute(
            "SELECT COUNT(*) as transactions, "
            "COALESCE(SUM(amount), 0) as total_revenue, "
            "COALESCE(AVG(amount), 0) as avg_order "
            "FROM pipeline_revenue WHERE brand = ? AND status != 'refunded'",
            (brand,),
        )
        return dict(rows[0]) if rows else {
            "transactions": 0, "total_revenue": 0, "avg_order": 0,
        }

    def get_revenue_by_source(self, brand: str | None = None) -> list[dict[str, Any]]:
        """Revenue broken down by acquisition source."""
        query = (
            "SELECT l.source_platform, l.brand, "
            "COUNT(r.id) as transactions, "
            "COALESCE(SUM(r.amount), 0) as total_revenue "
            "FROM pipeline_revenue r "
            "JOIN pipeline_leads l ON r.lead_id = l.id "
            "WHERE r.status != 'refunded' "
        )
        params: tuple = ()
        if brand:
            query += "AND r.brand = ? "
            params = (brand,)
        query += "GROUP BY l.source_platform, l.brand"
        return [dict(r) for r in self.db.execute(query, params)]

    # ── Funnel Analytics ─────────────────────────────────────

    def get_funnel(self, brand: str) -> dict[str, int]:
        """Count leads at each stage for a brand."""
        rows = self.db.execute(
            "SELECT stage, COUNT(*) as count "
            "FROM pipeline_leads WHERE brand = ? GROUP BY stage",
            (brand,),
        )
        result = {s: 0 for s in STAGES}
        for r in rows:
            result[r["stage"]] = r["count"]
        return result

    def get_conversion_rates(self, brand: str) -> dict[str, float]:
        """Calculate conversion rates between stages."""
        funnel = self.get_funnel(brand)
        rates = {}
        for i in range(len(STAGES) - 1):
            current = funnel.get(STAGES[i], 0)
            next_stage = funnel.get(STAGES[i + 1], 0)
            if current > 0:
                rates[f"{STAGES[i]}_to_{STAGES[i + 1]}"] = next_stage / current
            else:
                rates[f"{STAGES[i]}_to_{STAGES[i + 1]}"] = 0.0
        return rates

    def get_all_brands_funnel(self) -> dict[str, dict[str, int]]:
        """Funnel summary across all brands."""
        rows = self.db.execute(
            "SELECT brand, stage, COUNT(*) as count "
            "FROM pipeline_leads GROUP BY brand, stage"
        )
        result: dict[str, dict[str, int]] = {}
        for r in rows:
            brand = r["brand"]
            if brand not in result:
                result[brand] = {s: 0 for s in STAGES}
            result[brand][r["stage"]] = r["count"]
        return result

    def get_attribution_summary(self) -> list[dict[str, Any]]:
        """Which platforms/content pieces drive the most revenue."""
        rows = self.db.execute(
            "SELECT l.brand, l.source_platform, "
            "COUNT(DISTINCT l.id) as leads, "
            "COUNT(DISTINCT CASE WHEN l.stage = 'customer' THEN l.id END) as customers, "
            "COALESCE(SUM(r.amount), 0) as revenue "
            "FROM pipeline_leads l "
            "LEFT JOIN pipeline_revenue r ON l.id = r.lead_id "
            "GROUP BY l.brand, l.source_platform "
            "ORDER BY revenue DESC"
        )
        return [dict(r) for r in rows]
