"""Corporate entity management — LLC + Contractor structure.

Multi-layer financial structure for creator anonymity:

    Layer 1: Brands (customer-facing)
        - Each brand has its own Stripe/Gumroad/LS accounts
        - Customers see only the brand, never the LLC or creator

    Layer 2: Holding LLC (Wyoming/NM)
        - Owns all brands
        - Has its own bank account (Mercury, Relay, etc.)
        - Platform payouts flow here
        - Appears as "XYZ Holdings LLC" — no public member disclosure

    Layer 3: Contractor (the creator)
        - Invoices the LLC for "management consulting services"
        - LLC pays contractor monthly via bank transfer
        - Creator appears as external contractor, not owner
        - Complete separation between brand revenue and personal income

Tax note: The creator reports contractor income normally.
The LLC is the shield between public-facing brands and private identity.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

CORPORATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS corporate_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                   -- "XYZ Holdings LLC"
    entity_type TEXT NOT NULL,            -- llc_us, llc_uk, srl_it, sole_prop
    jurisdiction TEXT NOT NULL,           -- US-WY, US-NM, UK, IT
    registered_agent TEXT,               -- Registered agent service name
    ein_or_tax_id TEXT,                  -- EIN (US), UTR (UK), P.IVA (IT)
    formation_date TEXT,
    status TEXT DEFAULT 'active',         -- active, pending_formation, dissolved
    bank_name TEXT,                       -- Mercury, Relay, Wise, etc.
    bank_account_id TEXT,                 -- Last 4 digits or reference
    bank_routing TEXT,                    -- For incoming wires
    metadata TEXT,                        -- JSON: formation docs, agent details
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS corporate_brand_ownership (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES corporate_entities(id),
    brand TEXT NOT NULL,
    ownership_type TEXT DEFAULT 'full',   -- full, partial, dba
    dba_name TEXT,                        -- "doing business as" name if different
    registered_date TEXT,
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(entity_id, brand)
);

CREATE TABLE IF NOT EXISTS contractor_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias TEXT NOT NULL,                  -- Professional alias / business name
    entity_id INTEGER REFERENCES corporate_entities(id),  -- Which LLC they contract for
    service_description TEXT NOT NULL,    -- "Management consulting and technical advisory"
    rate_type TEXT DEFAULT 'monthly',     -- monthly, hourly, percentage, milestone
    rate_amount REAL DEFAULT 0,           -- Monthly retainer or hourly rate
    rate_percentage REAL DEFAULT 0,       -- If percentage-based (% of revenue)
    payment_method TEXT DEFAULT 'bank_transfer',  -- bank_transfer, wise, paypal
    payment_details TEXT,                 -- JSON: bank info, Wise email, etc.
    tax_id TEXT,                          -- Contractor's tax ID for invoicing
    status TEXT DEFAULT 'active',
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contractor_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contractor_id INTEGER NOT NULL REFERENCES contractor_profiles(id),
    entity_id INTEGER NOT NULL REFERENCES corporate_entities(id),
    invoice_number TEXT NOT NULL UNIQUE,
    period_start TEXT NOT NULL,           -- Billing period start
    period_end TEXT NOT NULL,             -- Billing period end
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    description TEXT,                     -- "Management consulting services — March 2026"
    line_items TEXT,                      -- JSON array of line items
    status TEXT DEFAULT 'draft',          -- draft, sent, paid, overdue, cancelled
    due_date TEXT,
    paid_date TEXT,
    payment_ref TEXT,                     -- Wire reference / transaction ID
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS llc_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES corporate_entities(id),
    category TEXT NOT NULL,            -- hardware, software, hosting, travel, education, services
    description TEXT NOT NULL,          -- "MacBook Pro 16-inch M4"
    vendor TEXT,                        -- "Apple", "AWS", "WeWork"
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    receipt_ref TEXT,                   -- Receipt URL or file reference
    purchase_date TEXT NOT NULL,
    is_recurring INTEGER DEFAULT 0,     -- 1 if recurring subscription
    recurrence_period TEXT,             -- monthly, yearly
    beneficiary TEXT DEFAULT 'creator', -- Who actually uses this
    tax_deductible INTEGER DEFAULT 1,   -- 1 if deductible as business expense
    status TEXT DEFAULT 'completed',    -- pending, completed, refunded
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tax_obligations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER REFERENCES corporate_entities(id),
    obligation_type TEXT NOT NULL,      -- form_5472, annual_report_wy, piva_acconto, piva_saldo
    jurisdiction TEXT NOT NULL,         -- US, US-WY, IT
    description TEXT NOT NULL,          -- "Form 5472 — Annual Information Return"
    due_date TEXT NOT NULL,
    filing_period TEXT,                 -- "2025", "2026-Q1"
    amount_due REAL DEFAULT 0,         -- Fee or tax amount
    currency TEXT DEFAULT 'USD',
    status TEXT DEFAULT 'pending',      -- pending, filed, paid, overdue, na
    filed_date TEXT,
    confirmation_ref TEXT,             -- Filing confirmation number
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fund_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_type TEXT NOT NULL,              -- platform_payout, llc_to_contractor, brand_to_llc
    source_type TEXT NOT NULL,            -- brand, platform, llc, contractor
    source_id TEXT NOT NULL,              -- brand name, platform name, entity ID
    dest_type TEXT NOT NULL,
    dest_id TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    reference TEXT,                       -- Transaction reference
    status TEXT DEFAULT 'completed',      -- pending, completed, failed
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""";


class CorporateManager:
    """Manages LLC entities, brand ownership, and contractor billing."""

    def __init__(self, db: Database):
        self.db = db
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(CORPORATE_SCHEMA)

    # ── Entity Management ──────────────────────────────────────

    def create_entity(self, name: str, entity_type: str,
                      jurisdiction: str, **kwargs: Any) -> int:
        """Register a new corporate entity (LLC, SRL, etc.)."""
        entity_id = self.db.execute_insert(
            "INSERT INTO corporate_entities "
            "(name, entity_type, jurisdiction, registered_agent, "
            "ein_or_tax_id, formation_date, bank_name, bank_account_id, "
            "bank_routing, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name, entity_type, jurisdiction,
                kwargs.get("registered_agent", ""),
                kwargs.get("ein_or_tax_id", ""),
                kwargs.get("formation_date", datetime.now().strftime("%Y-%m-%d")),
                kwargs.get("bank_name", ""),
                kwargs.get("bank_account_id", ""),
                kwargs.get("bank_routing", ""),
                json.dumps(kwargs.get("metadata", {})),
            ),
        )
        logger.info(f"Corporate entity created: {name} ({entity_type}) in {jurisdiction}")
        return entity_id

    def get_entity(self, entity_id: int) -> dict[str, Any] | None:
        rows = self.db.execute(
            "SELECT * FROM corporate_entities WHERE id = ?", (entity_id,)
        )
        return dict(rows[0]) if rows else None

    def get_all_entities(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM corporate_entities WHERE status = 'active'"
        )]

    def get_primary_entity(self) -> dict[str, Any] | None:
        """Get the main holding LLC (first active entity)."""
        rows = self.db.execute(
            "SELECT * FROM corporate_entities WHERE status = 'active' "
            "ORDER BY created_at ASC LIMIT 1"
        )
        return dict(rows[0]) if rows else None

    def update_entity_bank(self, entity_id: int, bank_name: str,
                           account_id: str, routing: str = "") -> None:
        self.db.execute(
            "UPDATE corporate_entities SET bank_name = ?, bank_account_id = ?, "
            "bank_routing = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (bank_name, account_id, routing, entity_id),
        )

    # ── Brand Ownership ────────────────────────────────────────

    def assign_brand(self, entity_id: int, brand: str,
                     ownership_type: str = "full",
                     dba_name: str = "") -> int:
        """Assign brand ownership to an entity."""
        return self.db.execute_insert(
            "INSERT OR IGNORE INTO corporate_brand_ownership "
            "(entity_id, brand, ownership_type, dba_name, registered_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (entity_id, brand, ownership_type, dba_name,
             datetime.now().strftime("%Y-%m-%d")),
        )

    def get_entity_brands(self, entity_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM corporate_brand_ownership WHERE entity_id = ?",
            (entity_id,),
        )]

    def get_brand_entity(self, brand: str) -> dict[str, Any] | None:
        """Find which entity owns a brand."""
        rows = self.db.execute(
            "SELECT ce.* FROM corporate_entities ce "
            "JOIN corporate_brand_ownership cbo ON ce.id = cbo.entity_id "
            "WHERE cbo.brand = ? AND ce.status = 'active'",
            (brand,),
        )
        return dict(rows[0]) if rows else None

    # ── Contractor Management ──────────────────────────────────

    def create_contractor(self, alias: str, entity_id: int,
                          service_description: str = "Management consulting and technical advisory",
                          rate_type: str = "percentage",
                          rate_amount: float = 0,
                          rate_percentage: float = 90.0,
                          payment_method: str = "bank_transfer",
                          payment_details: dict | None = None,
                          tax_id: str = "") -> int:
        """Register the creator as an external contractor."""
        return self.db.execute_insert(
            "INSERT INTO contractor_profiles "
            "(alias, entity_id, service_description, rate_type, rate_amount, "
            "rate_percentage, payment_method, payment_details, tax_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                alias, entity_id, service_description, rate_type,
                rate_amount, rate_percentage, payment_method,
                json.dumps(payment_details or {}), tax_id,
            ),
        )

    def get_contractor(self, contractor_id: int) -> dict[str, Any] | None:
        rows = self.db.execute(
            "SELECT * FROM contractor_profiles WHERE id = ?", (contractor_id,)
        )
        return dict(rows[0]) if rows else None

    def get_active_contractor(self, entity_id: int) -> dict[str, Any] | None:
        """Get the active contractor for an entity."""
        rows = self.db.execute(
            "SELECT * FROM contractor_profiles "
            "WHERE entity_id = ? AND status = 'active' LIMIT 1",
            (entity_id,),
        )
        return dict(rows[0]) if rows else None

    # ── Contractor Invoicing ───────────────────────────────────

    def generate_invoice(self, contractor_id: int, entity_id: int,
                         period_start: str, period_end: str,
                         brand_revenues: list[dict[str, Any]] | None = None,
                         ) -> dict[str, Any]:
        """Generate a contractor invoice for a billing period.

        Calculates amount based on contractor rate and brand revenues.
        If rate_type is 'percentage', takes rate_percentage of total revenue.
        If 'monthly', uses the fixed rate_amount.
        """
        contractor = self.get_contractor(contractor_id)
        if not contractor:
            return {"error": "Contractor not found"}

        entity = self.get_entity(entity_id)
        if not entity:
            return {"error": "Entity not found"}

        # Calculate invoice amount
        if contractor["rate_type"] == "percentage":
            # Sum revenue for this entity's brands in the period
            total_revenue = self._get_period_revenue(entity_id, period_start, period_end)
            amount = total_revenue * (contractor["rate_percentage"] / 100)
            line_items = [{
                "description": f"Consulting services ({contractor['rate_percentage']}% of revenue)",
                "quantity": 1,
                "unit_price": amount,
                "total": amount,
                "detail": f"Based on €{total_revenue:.2f} total brand revenue",
            }]
        elif contractor["rate_type"] == "monthly":
            amount = contractor["rate_amount"]
            line_items = [{
                "description": "Monthly management consulting retainer",
                "quantity": 1,
                "unit_price": amount,
                "total": amount,
            }]
        else:
            amount = contractor["rate_amount"]
            line_items = [{
                "description": contractor["service_description"],
                "quantity": 1,
                "unit_price": amount,
                "total": amount,
            }]

        if amount <= 0:
            return {"error": "No revenue to invoice for this period", "amount": 0}

        # Add brand-level breakdown if available
        if brand_revenues:
            for br in brand_revenues:
                line_items.append({
                    "description": f"  - Brand '{br['brand']}': €{br['revenue']:.2f}",
                    "quantity": 0, "unit_price": 0, "total": 0,
                    "type": "detail_line",
                })

        # Generate invoice number
        invoice_number = self._next_invoice_number(contractor_id)
        due_date = (datetime.strptime(period_end, "%Y-%m-%d") +
                    timedelta(days=15)).strftime("%Y-%m-%d")

        description = (
            f"{contractor['service_description']} — "
            f"{period_start} to {period_end}"
        )

        invoice_id = self.db.execute_insert(
            "INSERT INTO contractor_invoices "
            "(contractor_id, entity_id, invoice_number, period_start, "
            "period_end, amount, description, line_items, due_date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')",
            (
                contractor_id, entity_id, invoice_number,
                period_start, period_end, amount,
                description, json.dumps(line_items), due_date,
            ),
        )

        logger.info(
            f"Invoice generated: {invoice_number} for €{amount:.2f} "
            f"({contractor['alias']} → {entity['name']})"
        )

        return {
            "id": invoice_id,
            "invoice_number": invoice_number,
            "amount": amount,
            "currency": "EUR",
            "period": f"{period_start} — {period_end}",
            "due_date": due_date,
            "status": "draft",
            "line_items": line_items,
            "contractor": contractor["alias"],
            "entity": entity["name"],
        }

    def mark_invoice_sent(self, invoice_id: int) -> None:
        self.db.execute(
            "UPDATE contractor_invoices SET status = 'sent', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (invoice_id,),
        )

    def mark_invoice_paid(self, invoice_id: int,
                          payment_ref: str = "") -> None:
        """Mark invoice as paid and record the fund flow."""
        invoice = self._get_invoice(invoice_id)
        if not invoice:
            return

        self.db.execute(
            "UPDATE contractor_invoices SET status = 'paid', "
            "paid_date = ?, payment_ref = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (datetime.now().strftime("%Y-%m-%d"), payment_ref, invoice_id),
        )

        # Record the fund flow
        self.record_fund_flow(
            flow_type="llc_to_contractor",
            source_type="llc",
            source_id=str(invoice["entity_id"]),
            dest_type="contractor",
            dest_id=str(invoice["contractor_id"]),
            amount=invoice["amount"],
            reference=payment_ref or invoice["invoice_number"],
        )

    def get_pending_invoices(self, entity_id: int | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT ci.*, cp.alias as contractor_alias, ce.name as entity_name "
            "FROM contractor_invoices ci "
            "JOIN contractor_profiles cp ON ci.contractor_id = cp.id "
            "JOIN corporate_entities ce ON ci.entity_id = ce.id "
            "WHERE ci.status IN ('draft', 'sent')"
        )
        params: tuple = ()
        if entity_id:
            query += " AND ci.entity_id = ?"
            params = (entity_id,)
        query += " ORDER BY ci.created_at DESC"
        return [dict(r) for r in self.db.execute(query, params)]

    def get_invoice_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT ci.*, cp.alias as contractor_alias, ce.name as entity_name "
            "FROM contractor_invoices ci "
            "JOIN contractor_profiles cp ON ci.contractor_id = cp.id "
            "JOIN corporate_entities ce ON ci.entity_id = ce.id "
            "ORDER BY ci.created_at DESC LIMIT ?",
            (limit,),
        )]

    def _get_invoice(self, invoice_id: int) -> dict[str, Any] | None:
        rows = self.db.execute(
            "SELECT * FROM contractor_invoices WHERE id = ?", (invoice_id,)
        )
        return dict(rows[0]) if rows else None

    def _next_invoice_number(self, contractor_id: int) -> str:
        rows = self.db.execute(
            "SELECT COUNT(*) as count FROM contractor_invoices "
            "WHERE contractor_id = ?",
            (contractor_id,),
        )
        count = (rows[0]["count"] if rows else 0) + 1
        return f"CONT-{datetime.now().strftime('%Y%m')}-{count:04d}"

    def _get_period_revenue(self, entity_id: int,
                            period_start: str, period_end: str) -> float:
        """Get total revenue for all brands owned by an entity in a period."""
        rows = self.db.execute(
            "SELECT COALESCE(SUM(bpr.amount), 0) as total "
            "FROM brand_payments_received bpr "
            "JOIN corporate_brand_ownership cbo ON bpr.brand = cbo.brand "
            "WHERE cbo.entity_id = ? "
            "AND bpr.status = 'completed' "
            "AND date(bpr.created_at) >= ? AND date(bpr.created_at) <= ?",
            (entity_id, period_start, period_end),
        )
        return rows[0]["total"] if rows else 0.0

    # ── Fund Flow Tracking ─────────────────────────────────────

    def record_fund_flow(self, flow_type: str, source_type: str,
                         source_id: str, dest_type: str, dest_id: str,
                         amount: float, currency: str = "EUR",
                         reference: str = "",
                         metadata: dict | None = None) -> int:
        """Record a fund flow between entities in the system."""
        return self.db.execute_insert(
            "INSERT INTO fund_flows "
            "(flow_type, source_type, source_id, dest_type, dest_id, "
            "amount, currency, reference, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                flow_type, source_type, source_id, dest_type, dest_id,
                amount, currency, reference,
                json.dumps(metadata) if metadata else None,
            ),
        )

    def record_platform_payout(self, brand: str, platform: str,
                               amount: float, reference: str = "") -> int:
        """Record when a platform (Stripe, Gumroad) pays out to LLC bank."""
        entity = self.get_brand_entity(brand)
        entity_id = str(entity["id"]) if entity else "unknown"

        return self.record_fund_flow(
            flow_type="platform_payout",
            source_type="brand",
            source_id=brand,
            dest_type="llc",
            dest_id=entity_id,
            amount=amount,
            reference=reference,
            metadata={"platform": platform},
        )

    def get_fund_flows(self, flow_type: str | None = None,
                       limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM fund_flows"
        params: list = []
        if flow_type:
            query += " WHERE flow_type = ?"
            params.append(flow_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.execute(query, tuple(params))]

    def get_total_paid_to_contractor(self, contractor_id: int | None = None) -> float:
        """Total amount paid to contractor across all invoices."""
        query = (
            "SELECT COALESCE(SUM(amount), 0) as total "
            "FROM contractor_invoices WHERE status = 'paid'"
        )
        params: tuple = ()
        if contractor_id:
            query += " AND contractor_id = ?"
            params = (contractor_id,)
        rows = self.db.execute(query, params)
        return rows[0]["total"] if rows else 0.0

    # ── Expense Management (LLC buys for creator) ─────────────

    def record_expense(self, entity_id: int, category: str,
                       description: str, amount: float,
                       vendor: str = "", purchase_date: str = "",
                       is_recurring: bool = False,
                       recurrence_period: str = "",
                       receipt_ref: str = "",
                       currency: str = "EUR",
                       metadata: dict | None = None) -> int:
        """Record an expense made by the LLC for the creator's benefit."""
        if not purchase_date:
            purchase_date = datetime.now().strftime("%Y-%m-%d")

        expense_id = self.db.execute_insert(
            "INSERT INTO llc_expenses "
            "(entity_id, category, description, vendor, amount, currency, "
            "receipt_ref, purchase_date, is_recurring, recurrence_period, "
            "metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entity_id, category, description, vendor, amount, currency,
             receipt_ref, purchase_date, 1 if is_recurring else 0,
             recurrence_period, json.dumps(metadata or {})),
        )

        # Record as fund flow
        self.record_fund_flow(
            flow_type="llc_expense",
            source_type="llc", source_id=str(entity_id),
            dest_type="vendor", dest_id=vendor or "unknown",
            amount=amount, currency=currency,
            reference=receipt_ref,
            metadata={"category": category, "description": description},
        )

        logger.info(f"Expense recorded: €{amount:.2f} — {description} ({category})")
        return expense_id

    def get_expenses(self, entity_id: int | None = None,
                     category: str = "",
                     period_start: str = "",
                     period_end: str = "",
                     limit: int = 100) -> list[dict[str, Any]]:
        """Get expenses with optional filters."""
        query = "SELECT * FROM llc_expenses WHERE 1=1"
        params: list = []

        if entity_id:
            query += " AND entity_id = ?"
            params.append(entity_id)
        if category:
            query += " AND category = ?"
            params.append(category)
        if period_start:
            query += " AND purchase_date >= ?"
            params.append(period_start)
        if period_end:
            query += " AND purchase_date <= ?"
            params.append(period_end)

        query += " ORDER BY purchase_date DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.execute(query, tuple(params))]

    def get_expense_total(self, entity_id: int,
                          period_start: str = "",
                          period_end: str = "") -> float:
        """Total expenses for an entity, optionally filtered by period."""
        query = (
            "SELECT COALESCE(SUM(amount), 0) as total FROM llc_expenses "
            "WHERE entity_id = ? AND status = 'completed'"
        )
        params: list = [entity_id]
        if period_start:
            query += " AND purchase_date >= ?"
            params.append(period_start)
        if period_end:
            query += " AND purchase_date <= ?"
            params.append(period_end)
        rows = self.db.execute(query, tuple(params))
        return rows[0]["total"] if rows else 0.0

    def get_recurring_expenses(self, entity_id: int) -> list[dict[str, Any]]:
        """Get active recurring expenses (subscriptions, services)."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM llc_expenses "
            "WHERE entity_id = ? AND is_recurring = 1 AND status = 'completed' "
            "ORDER BY purchase_date DESC",
            (entity_id,),
        )]

    def get_expense_summary_by_category(self, entity_id: int,
                                         period_start: str = "",
                                         period_end: str = "") -> dict[str, float]:
        """Expense breakdown by category."""
        query = (
            "SELECT category, COALESCE(SUM(amount), 0) as total "
            "FROM llc_expenses WHERE entity_id = ? AND status = 'completed'"
        )
        params: list = [entity_id]
        if period_start:
            query += " AND purchase_date >= ?"
            params.append(period_start)
        if period_end:
            query += " AND purchase_date <= ?"
            params.append(period_end)
        query += " GROUP BY category ORDER BY total DESC"
        rows = self.db.execute(query, tuple(params))
        return {r["category"]: r["total"] for r in rows}

    # ── Tax Compliance ──────────────────────────────────────────

    def add_tax_obligation(self, obligation_type: str, jurisdiction: str,
                           description: str, due_date: str,
                           entity_id: int | None = None,
                           filing_period: str = "",
                           amount_due: float = 0,
                           currency: str = "USD") -> int:
        """Register a tax filing obligation."""
        return self.db.execute_insert(
            "INSERT INTO tax_obligations "
            "(entity_id, obligation_type, jurisdiction, description, "
            "due_date, filing_period, amount_due, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entity_id, obligation_type, jurisdiction, description,
             due_date, filing_period, amount_due, currency),
        )

    def mark_obligation_filed(self, obligation_id: int,
                              confirmation_ref: str = "") -> None:
        self.db.execute(
            "UPDATE tax_obligations SET status = 'filed', "
            "filed_date = ?, confirmation_ref = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (datetime.now().strftime("%Y-%m-%d"), confirmation_ref,
             obligation_id),
        )

    def mark_obligation_paid(self, obligation_id: int,
                             confirmation_ref: str = "") -> None:
        self.db.execute(
            "UPDATE tax_obligations SET status = 'paid', "
            "filed_date = COALESCE(filed_date, ?), confirmation_ref = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (datetime.now().strftime("%Y-%m-%d"), confirmation_ref,
             obligation_id),
        )

    def get_pending_obligations(self, jurisdiction: str = "") -> list[dict[str, Any]]:
        """Get upcoming/overdue tax obligations."""
        query = (
            "SELECT * FROM tax_obligations "
            "WHERE status IN ('pending', 'overdue')"
        )
        params: list = []
        if jurisdiction:
            query += " AND jurisdiction = ?"
            params.append(jurisdiction)
        query += " ORDER BY due_date ASC"
        return [dict(r) for r in self.db.execute(query, tuple(params))]

    def get_overdue_obligations(self) -> list[dict[str, Any]]:
        """Get obligations past their due date."""
        today = datetime.now().strftime("%Y-%m-%d")
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM tax_obligations "
            "WHERE status = 'pending' AND due_date < ?",
            (today,),
        )]

    def setup_annual_obligations(self, entity_id: int, year: int) -> list[int]:
        """Create standard annual tax obligations for a US LLC.

        Covers: Form 5472, Wyoming annual report, registered agent renewal.
        """
        entity = self.get_entity(entity_id)
        if not entity:
            return []

        obligations = []

        # Form 5472 + pro-forma 1120 (due April 15)
        obligations.append(self.add_tax_obligation(
            obligation_type="form_5472",
            jurisdiction="US",
            description=f"Form 5472 + pro-forma 1120 — {entity['name']} — {year}",
            due_date=f"{year + 1}-04-15",
            entity_id=entity_id,
            filing_period=str(year),
            amount_due=0,  # No tax, just filing
            currency="USD",
        ))

        # Wyoming annual report (due on anniversary month)
        formation = entity.get("formation_date", "")
        month = "01"
        if formation:
            try:
                month = formation.split("-")[1]
            except (IndexError, ValueError):
                pass
        obligations.append(self.add_tax_obligation(
            obligation_type="annual_report_wy",
            jurisdiction="US-WY",
            description=f"Wyoming Annual Report — {entity['name']} — {year}",
            due_date=f"{year}-{month}-01",
            entity_id=entity_id,
            filing_period=str(year),
            amount_due=60.0,
            currency="USD",
        ))

        # Registered agent renewal
        obligations.append(self.add_tax_obligation(
            obligation_type="registered_agent_renewal",
            jurisdiction="US-WY",
            description=f"Registered agent renewal — {entity['name']} — {year}",
            due_date=f"{year}-01-15",
            entity_id=entity_id,
            filing_period=str(year),
            amount_due=150.0,
            currency="USD",
        ))

        logger.info(f"Created {len(obligations)} tax obligations for {entity['name']} — {year}")
        return obligations

    def setup_piva_obligations(self, year: int,
                               contractor_id: int | None = None) -> list[int]:
        """Create Italian P.IVA forfettario tax obligations for a year.

        Covers: acconto (advance) payments and saldo (balance).
        """
        obligations = []

        # Acconto IRPEF 1st installment (June 30)
        obligations.append(self.add_tax_obligation(
            obligation_type="piva_acconto_1",
            jurisdiction="IT",
            description=f"P.IVA acconto 1° rata IRPEF — {year}",
            due_date=f"{year}-06-30",
            filing_period=str(year),
            currency="EUR",
        ))

        # Acconto IRPEF 2nd installment (November 30)
        obligations.append(self.add_tax_obligation(
            obligation_type="piva_acconto_2",
            jurisdiction="IT",
            description=f"P.IVA acconto 2° rata IRPEF — {year}",
            due_date=f"{year}-11-30",
            filing_period=str(year),
            currency="EUR",
        ))

        # Saldo IRPEF (June 30 of following year)
        obligations.append(self.add_tax_obligation(
            obligation_type="piva_saldo",
            jurisdiction="IT",
            description=f"P.IVA saldo IRPEF — {year}",
            due_date=f"{year + 1}-06-30",
            filing_period=str(year),
            currency="EUR",
        ))

        # INPS contributi gestione separata (same deadlines as IRPEF)
        obligations.append(self.add_tax_obligation(
            obligation_type="inps_acconto_1",
            jurisdiction="IT",
            description=f"INPS gestione separata acconto 1° — {year}",
            due_date=f"{year}-06-30",
            filing_period=str(year),
            currency="EUR",
        ))

        obligations.append(self.add_tax_obligation(
            obligation_type="inps_acconto_2",
            jurisdiction="IT",
            description=f"INPS gestione separata acconto 2° — {year}",
            due_date=f"{year}-11-30",
            filing_period=str(year),
            currency="EUR",
        ))

        # Dichiarazione Redditi PF (November 30 of following year)
        obligations.append(self.add_tax_obligation(
            obligation_type="dichiarazione_redditi",
            jurisdiction="IT",
            description=f"Dichiarazione redditi PF — {year}",
            due_date=f"{year + 1}-11-30",
            filing_period=str(year),
            currency="EUR",
        ))

        logger.info(f"Created {len(obligations)} P.IVA obligations for {year}")
        return obligations

    # ── Financial Summary ──────────────────────────────────────

    def get_financial_summary(self) -> dict[str, Any]:
        """Full financial picture across the corporate structure."""
        entities = self.get_all_entities()
        total_revenue = 0.0
        total_paid_out = 0.0
        entity_details = []

        for entity in entities:
            brands = self.get_entity_brands(entity["id"])
            brand_names = [b["brand"] for b in brands]

            # Revenue from all brands under this entity
            revenue = 0.0
            for brand in brand_names:
                rows = self.db.execute(
                    "SELECT COALESCE(SUM(amount), 0) as total "
                    "FROM brand_payments_received "
                    "WHERE brand = ? AND status = 'completed'",
                    (brand,),
                )
                revenue += rows[0]["total"] if rows else 0

            # Paid to contractor
            paid = self.get_total_paid_to_contractor()

            total_revenue += revenue
            total_paid_out += paid

            entity_details.append({
                "entity": entity["name"],
                "jurisdiction": entity["jurisdiction"],
                "brands": brand_names,
                "total_revenue": revenue,
                "total_paid_to_contractor": paid,
                "retained_in_llc": revenue - paid,
            })

        # Pending invoices
        pending = self.get_pending_invoices()
        pending_total = sum(inv["amount"] for inv in pending)

        # Total expenses across all entities
        total_expenses = sum(
            self.get_expense_total(e["id"]) for e in entities
        )

        # Tax obligations
        pending_tax = self.get_pending_obligations()
        overdue_tax = self.get_overdue_obligations()

        return {
            "entities": entity_details,
            "total_revenue": total_revenue,
            "total_paid_to_contractor": total_paid_out,
            "total_expenses_via_llc": total_expenses,
            "total_retained_in_llc": total_revenue - total_paid_out - total_expenses,
            "pending_invoices": len(pending),
            "pending_invoice_total": pending_total,
            "pending_tax_obligations": len(pending_tax),
            "overdue_tax_obligations": len(overdue_tax),
            "creator_effective_income": total_paid_out + total_expenses,
        }
