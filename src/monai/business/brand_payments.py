"""Brand Payments — per-brand anonymous payment collection and profit sweeping.

Each brand operates its own payment accounts under its own identity.
Payments are NEVER traceable to the creator. Identity separation is absolute.

Supported collection methods:
- Crypto wallets (Monero preferred, Bitcoin acceptable)
- Stripe Connect (under brand's business identity)
- PayPal Business (under brand's business identity)
- Platform payouts (Gumroad, Lemon Squeezy, etc.)

Profit sweep: brand accounts → mixing/privacy layer → creator wallet.
Everything is LEGAL. Identity separation, not law evasion.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

BRAND_PAYMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS brand_payment_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    provider TEXT NOT NULL,            -- crypto_xmr, crypto_btc, stripe, paypal,
                                       -- gumroad, lemonsqueezy, platform_payout
    account_type TEXT NOT NULL,        -- collection (from customers), sweep (to creator)
    account_id TEXT NOT NULL,          -- wallet address, account email, connect ID
    label TEXT,                        -- human-readable label
    currency TEXT DEFAULT 'EUR',
    balance REAL DEFAULT 0,           -- tracked balance (updated by sweep checks)
    identity_id TEXT,                  -- references identity used to register this account
    status TEXT DEFAULT 'active',      -- active, pending_setup, suspended, closed
    metadata TEXT,                     -- JSON: API keys, webhook URLs, config
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(brand, provider, account_id)
);

CREATE TABLE IF NOT EXISTS brand_payments_received (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    account_id INTEGER REFERENCES brand_payment_accounts(id),
    lead_id INTEGER,                   -- references pipeline_leads(id) for attribution
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    product TEXT,                       -- what was sold
    customer_email TEXT,               -- for receipt/follow-up (if provided)
    payment_ref TEXT,                  -- tx hash, Stripe charge ID, etc.
    status TEXT DEFAULT 'completed',   -- pending, completed, refunded, disputed
    metadata TEXT,                     -- JSON: platform-specific data
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS brand_profit_sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    from_account_id INTEGER REFERENCES brand_payment_accounts(id),
    to_account_id INTEGER REFERENCES brand_payment_accounts(id),
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    sweep_method TEXT NOT NULL,        -- crypto_xmr, crypto_btc_coinjoin, crypto_btc_direct
    tx_reference TEXT,                 -- transaction hash
    status TEXT DEFAULT 'pending',     -- pending, mixing, completed, failed
    metadata TEXT,                     -- JSON: mixing details, intermediate steps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- Platform fees tracking (Stripe 2.9%+€0.30, Gumroad 10%, etc.)
CREATE TABLE IF NOT EXISTS platform_fees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    provider TEXT NOT NULL,            -- stripe, gumroad, btcpay, etc.
    payment_id INTEGER REFERENCES brand_payments_received(id),
    gross_amount REAL NOT NULL,
    fee_amount REAL NOT NULL,
    fee_currency TEXT DEFAULT 'EUR',
    fee_type TEXT DEFAULT 'transaction', -- transaction, payout, subscription
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Performance indexes for brand payments
CREATE INDEX IF NOT EXISTS idx_bpr_brand ON brand_payments_received(brand);
CREATE INDEX IF NOT EXISTS idx_bpr_status ON brand_payments_received(brand, status);
CREATE INDEX IF NOT EXISTS idx_bpr_payment_ref ON brand_payments_received(payment_ref);
CREATE INDEX IF NOT EXISTS idx_bps_brand ON brand_profit_sweeps(brand, status);
CREATE INDEX IF NOT EXISTS idx_bpa_brand ON brand_payment_accounts(brand, account_type, status);
CREATE INDEX IF NOT EXISTS idx_bpf_brand ON platform_fees(brand, provider);
""";

# Collection methods available to brands
COLLECTION_METHODS = [
    {
        "provider": "crypto_xmr",
        "name": "Monero (XMR)",
        "anonymity": "maximum",
        "description": "Privacy by default. Untraceable transactions. Best option.",
        "setup": "Generate XMR wallet per brand",
    },
    {
        "provider": "crypto_btc",
        "name": "Bitcoin (BTC)",
        "anonymity": "medium",
        "description": "Pseudonymous. Use with CoinJoin for sweep.",
        "setup": "Generate BTC wallet per brand",
    },
    {
        "provider": "stripe",
        "name": "Stripe Connect",
        "anonymity": "low",
        "description": "Card payments. Requires brand business identity + KYC.",
        "setup": "Register Stripe account under brand identity",
    },
    {
        "provider": "paypal",
        "name": "PayPal Business",
        "anonymity": "low",
        "description": "PayPal checkout. Requires brand business identity.",
        "setup": "Register PayPal business under brand identity",
    },
    {
        "provider": "gumroad",
        "name": "Gumroad",
        "anonymity": "medium",
        "description": "Digital product sales. Platform handles payments.",
        "setup": "Create Gumroad account under brand identity",
    },
    {
        "provider": "lemonsqueezy",
        "name": "Lemon Squeezy",
        "anonymity": "medium",
        "description": "SaaS and digital product payments. Merchant of record.",
        "setup": "Create Lemon Squeezy account under brand identity",
    },
]

# Sweep methods ranked by privacy (brand → creator)
SWEEP_METHODS = [
    {
        "method": "crypto_xmr",
        "name": "Monero Direct",
        "privacy": "maximum",
        "description": "XMR to XMR — fully private by protocol design.",
    },
    {
        "method": "crypto_btc_coinjoin",
        "name": "Bitcoin + CoinJoin",
        "privacy": "high",
        "description": "BTC through CoinJoin mixing before transfer.",
    },
    {
        "method": "crypto_btc_direct",
        "name": "Bitcoin Direct",
        "privacy": "medium",
        "description": "Direct BTC transfer. On-chain traceable.",
    },
]


class BrandPayments:
    """Per-brand payment collection and anonymous profit sweeping."""

    def __init__(self, db: Database):
        self.db = db
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(BRAND_PAYMENTS_SCHEMA)

    # ── Account Management ────────────────────────────────────

    def add_collection_account(self, brand: str, provider: str,
                               account_id: str, label: str = "",
                               currency: str = "EUR",
                               identity_id: str = "",
                               metadata: dict | None = None) -> int:
        """Register a payment collection account for a brand."""
        return self.db.execute_insert(
            "INSERT OR IGNORE INTO brand_payment_accounts "
            "(brand, provider, account_type, account_id, label, "
            "currency, identity_id, metadata) "
            "VALUES (?, ?, 'collection', ?, ?, ?, ?, ?)",
            (brand, provider, account_id, label, currency,
             identity_id, json.dumps(metadata) if metadata else None),
        )

    def add_sweep_account(self, brand: str, provider: str,
                          account_id: str, label: str = "",
                          metadata: dict | None = None) -> int:
        """Register a sweep destination (creator's anonymous wallet)."""
        return self.db.execute_insert(
            "INSERT OR IGNORE INTO brand_payment_accounts "
            "(brand, provider, account_type, account_id, label, metadata) "
            "VALUES (?, ?, 'sweep', ?, ?, ?)",
            (brand, provider, account_id, label,
             json.dumps(metadata) if metadata else None),
        )

    def get_collection_accounts(self, brand: str) -> list[dict[str, Any]]:
        """Get active collection accounts for a brand."""
        rows = self.db.execute(
            "SELECT * FROM brand_payment_accounts "
            "WHERE brand = ? AND account_type = 'collection' "
            "AND status = 'active'",
            (brand,),
        )
        return [dict(r) for r in rows]

    def get_sweep_accounts(self, brand: str) -> list[dict[str, Any]]:
        """Get sweep destination accounts for a brand."""
        rows = self.db.execute(
            "SELECT * FROM brand_payment_accounts "
            "WHERE brand = ? AND account_type = 'sweep' "
            "AND status = 'active'",
            (brand,),
        )
        return [dict(r) for r in rows]

    def update_account_balance(self, account_id: int,
                               balance: float) -> None:
        """Update tracked balance for an account."""
        self.db.execute(
            "UPDATE brand_payment_accounts SET balance = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (balance, account_id),
        )

    def deactivate_account(self, account_id: int) -> None:
        """Deactivate a payment account."""
        self.db.execute(
            "UPDATE brand_payment_accounts SET status = 'closed', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (account_id,),
        )

    # ── Payment Reception ─────────────────────────────────────

    def record_payment(self, brand: str, account_id: int,
                       amount: float, product: str = "",
                       customer_email: str = "",
                       payment_ref: str = "",
                       lead_id: int | None = None,
                       currency: str = "EUR",
                       metadata: dict | None = None) -> int:
        """Record an incoming payment for a brand."""
        # Round to 2 decimal places to avoid floating-point drift in SQLite REAL
        amount_rounded = float(Decimal(str(amount)).quantize(Decimal("0.01")))
        pay_id = self.db.execute_insert(
            "INSERT INTO brand_payments_received "
            "(brand, account_id, lead_id, amount, currency, product, "
            "customer_email, payment_ref, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (brand, account_id, lead_id, amount_rounded, currency, product,
             customer_email, payment_ref,
             json.dumps(metadata) if metadata else None),
        )
        logger.info(f"Payment received: {brand} — {currency} {amount_rounded:.2f} for {product}")
        return pay_id

    def get_payments(self, brand: str,
                     status: str | None = None,
                     limit: int = 100) -> list[dict[str, Any]]:
        """Get payments received by a brand."""
        query = "SELECT * FROM brand_payments_received WHERE brand = ?"
        params: list = [brand]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.execute(query, tuple(params))]

    def get_brand_revenue(self, brand: str) -> dict[str, Any]:
        """Total revenue for a brand."""
        rows = self.db.execute(
            "SELECT COUNT(*) as transactions, "
            "COALESCE(SUM(amount), 0) as total_revenue, "
            "COALESCE(AVG(amount), 0) as avg_payment "
            "FROM brand_payments_received "
            "WHERE brand = ? AND status IN ('completed', 'pending')",
            (brand,),
        )
        return dict(rows[0]) if rows else {
            "transactions": 0, "total_revenue": 0, "avg_payment": 0,
        }

    def refund_payment(self, payment_id: int) -> dict[str, Any]:
        """Mark a payment as refunded."""
        self.db.execute(
            "UPDATE brand_payments_received SET status = 'refunded' "
            "WHERE id = ?",
            (payment_id,),
        )
        return {"status": "refunded", "payment_id": payment_id}

    # ── Profit Sweeping ───────────────────────────────────────

    def get_sweepable_balance(self, brand: str, currency: str | None = None) -> float:
        """Calculate how much can be swept from a brand's accounts.

        Uses a single atomic query to avoid race conditions between
        reading received payments and swept amounts.

        Args:
            brand: Brand name.
            currency: If provided, only count payments in this currency.
        """
        if currency:
            rows = self.db.execute(
                "SELECT MAX(0, "
                "  COALESCE((SELECT SUM(amount) FROM brand_payments_received "
                "            WHERE brand = ? AND status = 'completed' AND currency = ?), 0) - "
                "  COALESCE((SELECT SUM(amount) FROM brand_profit_sweeps "
                "            WHERE brand = ? AND status IN ('completed', 'pending', 'mixing') "
                "            AND currency = ?), 0)"
                ") as balance",
                (brand, currency, brand, currency),
            )
        else:
            rows = self.db.execute(
                "SELECT MAX(0, "
                "  COALESCE((SELECT SUM(amount) FROM brand_payments_received "
                "            WHERE brand = ? AND status = 'completed'), 0) - "
                "  COALESCE((SELECT SUM(amount) FROM brand_profit_sweeps "
                "            WHERE brand = ? AND status IN ('completed', 'pending', 'mixing')), 0)"
                ") as balance",
                (brand, brand),
            )
        return rows[0]["balance"] if rows else 0.0

    def get_sweepable_by_currency(self, brand: str) -> dict[str, float]:
        """Get sweepable balance per currency for a brand."""
        rows = self.db.execute(
            "SELECT currency, COALESCE(SUM(amount), 0) as received "
            "FROM brand_payments_received "
            "WHERE brand = ? AND status = 'completed' "
            "GROUP BY currency",
            (brand,),
        )
        received = {r["currency"]: r["received"] for r in rows}

        rows = self.db.execute(
            "SELECT currency, COALESCE(SUM(amount), 0) as swept "
            "FROM brand_profit_sweeps "
            "WHERE brand = ? AND status IN ('completed', 'pending', 'mixing') "
            "GROUP BY currency",
            (brand,),
        )
        swept = {r["currency"]: r["swept"] for r in rows}

        result = {}
        for cur in set(received) | set(swept):
            bal = received.get(cur, 0) - swept.get(cur, 0)
            if bal > 0:
                result[cur] = bal
        return result

    def initiate_sweep(self, brand: str, from_account_id: int,
                       to_account_id: int, amount: float,
                       sweep_method: str = "crypto_xmr",
                       metadata: dict | None = None) -> int:
        """Initiate a profit sweep from brand to creator."""
        sweep_id = self.db.execute_insert(
            "INSERT INTO brand_profit_sweeps "
            "(brand, from_account_id, to_account_id, amount, "
            "sweep_method, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (brand, from_account_id, to_account_id, amount,
             sweep_method, json.dumps(metadata) if metadata else None),
        )
        logger.info(
            f"Sweep initiated: {brand} — {amount:.2f} via {sweep_method}"
        )
        return sweep_id

    def complete_sweep(self, sweep_id: int,
                       tx_reference: str = "") -> None:
        """Mark a sweep as completed."""
        self.db.execute(
            "UPDATE brand_profit_sweeps SET status = 'completed', "
            "tx_reference = ?, completed_at = ? WHERE id = ?",
            (tx_reference, datetime.now().isoformat(), sweep_id),
        )

    def mark_sweep_mixing(self, sweep_id: int) -> None:
        """Mark a sweep as in the mixing phase (CoinJoin etc.)."""
        self.db.execute(
            "UPDATE brand_profit_sweeps SET status = 'mixing' "
            "WHERE id = ?",
            (sweep_id,),
        )

    def fail_sweep(self, sweep_id: int, reason: str = "") -> None:
        """Mark a sweep as failed."""
        self.db.execute(
            "UPDATE brand_profit_sweeps SET status = 'failed', "
            "metadata = json_set(COALESCE(metadata, '{}'), '$.error', ?) "
            "WHERE id = ?",
            (reason, sweep_id),
        )

    def get_sweep_history(self, brand: str | None = None,
                          limit: int = 50) -> list[dict[str, Any]]:
        """Get sweep history, optionally filtered by brand."""
        query = "SELECT * FROM brand_profit_sweeps"
        params: list = []
        if brand:
            query += " WHERE brand = ?"
            params.append(brand)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.execute(query, tuple(params))]

    # ── Analytics ─────────────────────────────────────────────

    def get_all_brands_revenue(self) -> list[dict[str, Any]]:
        """Revenue summary across all brands."""
        rows = self.db.execute(
            "SELECT brand, "
            "COUNT(*) as transactions, "
            "COALESCE(SUM(amount), 0) as total_revenue, "
            "COALESCE(AVG(amount), 0) as avg_payment "
            "FROM brand_payments_received "
            "WHERE status IN ('completed', 'pending') "
            "GROUP BY brand ORDER BY total_revenue DESC"
        )
        return [dict(r) for r in rows]

    def get_revenue_by_provider(self, brand: str | None = None) -> list[dict[str, Any]]:
        """Revenue broken down by payment provider."""
        query = (
            "SELECT bpr.brand, bpa.provider, "
            "COUNT(*) as transactions, "
            "COALESCE(SUM(bpr.amount), 0) as total_revenue "
            "FROM brand_payments_received bpr "
            "JOIN brand_payment_accounts bpa ON bpr.account_id = bpa.id "
            "WHERE bpr.status IN ('completed', 'pending') "
        )
        params: tuple = ()
        if brand:
            query += "AND bpr.brand = ? "
            params = (brand,)
        query += "GROUP BY bpr.brand, bpa.provider"
        return [dict(r) for r in self.db.execute(query, params)]

    def get_total_swept(self, brand: str | None = None) -> float:
        """Total amount swept to creator."""
        query = (
            "SELECT COALESCE(SUM(amount), 0) as total "
            "FROM brand_profit_sweeps WHERE status = 'completed'"
        )
        params: tuple = ()
        if brand:
            query += " AND brand = ?"
            params = (brand,)
        rows = self.db.execute(query, params)
        return rows[0]["total"] if rows else 0.0

    # ── Platform Fee Tracking ─────────────────────────────────

    # Standard platform fee rates — fee always in SAME currency as payment
    PLATFORM_FEE_RATES = {
        "stripe": {"rate": 0.029, "fixed": 0.30},
        "gumroad": {"rate": 0.10, "fixed": 0.0},
        "lemonsqueezy": {"rate": 0.05, "fixed": 0.50},
        "paypal": {"rate": 0.029, "fixed": 0.30},
    }

    def record_platform_fee(self, brand: str, provider: str,
                            payment_id: int, gross_amount: float,
                            fee_amount: float | None = None,
                            fee_currency: str = "EUR") -> int:
        """Record a platform fee for a payment.

        If fee_amount is not provided, calculates from standard rates.
        Fee currency always matches the payment currency (fee_currency param).
        """
        if fee_amount is None:
            rates = self.PLATFORM_FEE_RATES.get(provider)
            if rates:
                gross = Decimal(str(gross_amount))
                fee_amount = float(
                    (gross * Decimal(str(rates["rate"])) + Decimal(str(rates["fixed"])))
                    .quantize(Decimal("0.01"))
                )
            else:
                fee_amount = 0.0
        else:
            # Round provided fee to 2 decimals
            fee_amount = float(Decimal(str(fee_amount)).quantize(Decimal("0.01")))

        gross_rounded = float(Decimal(str(gross_amount)).quantize(Decimal("0.01")))

        return self.db.execute_insert(
            "INSERT INTO platform_fees "
            "(brand, provider, payment_id, gross_amount, fee_amount, fee_currency) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (brand, provider, payment_id, gross_rounded, fee_amount, fee_currency),
        )

    def get_total_fees(self, brand: str | None = None) -> float:
        """Get total platform fees paid."""
        query = "SELECT COALESCE(SUM(fee_amount), 0) as total FROM platform_fees"
        params: tuple = ()
        if brand:
            query += " WHERE brand = ?"
            params = (brand,)
        rows = self.db.execute(query, params)
        return rows[0]["total"] if rows else 0.0

    def get_net_revenue(self, brand: str) -> float:
        """Get net revenue (gross - fees) for a brand."""
        gross = self.get_brand_revenue(brand).get("total_revenue", 0)
        fees = self.get_total_fees(brand)
        return max(0.0, gross - fees)

    def get_collection_methods(self) -> list[dict[str, str]]:
        """Available collection methods for brands."""
        return COLLECTION_METHODS

    def get_sweep_methods(self) -> list[dict[str, str]]:
        """Available sweep methods ranked by privacy."""
        return SWEEP_METHODS
