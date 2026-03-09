"""Payment processing — how agents receive money and transfer profits to the creator.

Two-layer payment model:
1. Agent receives payments from clients under its own business identity
   (PayPal, Stripe, bank transfer, crypto, platform payouts, etc.)
2. Agent transfers profits to the creator through legitimate channels
   that maintain identity separation (crypto, intermediary accounts, etc.)

Everything is LEGAL. No money laundering, no tax evasion.
The goal is identity separation, not law evasion.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.config import Config
from monai.db.database import Database

logger = logging.getLogger(__name__)

# Extend DB schema for payment tracking
PAYMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,           -- receiving, transfer_out
    provider TEXT NOT NULL,       -- stripe, paypal, wise, crypto_btc, crypto_eth, platform_payout
    account_id TEXT NOT NULL,     -- account identifier (email, wallet address, etc.)
    status TEXT DEFAULT 'active', -- active, pending_setup, suspended
    metadata TEXT,                -- JSON: additional config
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS profit_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    from_account_id INTEGER REFERENCES payment_accounts(id),
    to_method TEXT NOT NULL,      -- how profits were transferred to creator
    status TEXT DEFAULT 'pending', -- pending, completed, failed
    tx_reference TEXT,            -- transaction ID/hash
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);
"""

# Transfer methods ranked by anonymity (creator-agent separation)
TRANSFER_METHODS = [
    {
        "method": "crypto_monero",
        "name": "Monero (XMR)",
        "anonymity": "high",
        "description": "Privacy-focused cryptocurrency. Near-untraceable transactions.",
        "requires": "Creator provides XMR wallet address",
    },
    {
        "method": "crypto_btc_mixed",
        "name": "Bitcoin (via mixer/CoinJoin)",
        "anonymity": "medium-high",
        "description": "Bitcoin with transaction mixing for privacy.",
        "requires": "Creator provides BTC wallet address",
    },
    {
        "method": "crypto_btc",
        "name": "Bitcoin (direct)",
        "anonymity": "medium",
        "description": "Direct Bitcoin transfer. Pseudonymous but traceable on-chain.",
        "requires": "Creator provides BTC wallet address",
    },
    {
        "method": "prepaid_card",
        "name": "Virtual Prepaid Card",
        "anonymity": "medium",
        "description": "Load funds onto virtual prepaid card, share card details.",
        "requires": "Agent obtains virtual card service",
    },
    {
        "method": "wise_transfer",
        "name": "Wise (TransferWise)",
        "anonymity": "low-medium",
        "description": "International transfer through Wise. Requires agent's own Wise account.",
        "requires": "Agent Wise account + creator bank details",
    },
]


class PaymentManager:
    """Manages payment reception and profit transfer."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(PAYMENTS_SCHEMA)

    # ── Receiving Payments ───────────────────────────────────────

    def add_receiving_account(self, provider: str, account_id: str,
                              metadata: dict | None = None) -> int:
        """Register a payment account where clients pay the agent."""
        return self.db.execute_insert(
            "INSERT INTO payment_accounts (type, provider, account_id, metadata) "
            "VALUES ('receiving', ?, ?, ?)",
            (provider, account_id, json.dumps(metadata) if metadata else None),
        )

    def get_receiving_accounts(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM payment_accounts WHERE type = 'receiving' AND status = 'active'"
        )
        return [dict(r) for r in rows]

    # ── Transfer to Creator ──────────────────────────────────────

    def add_transfer_account(self, provider: str, account_id: str,
                             metadata: dict | None = None) -> int:
        """Register a method for transferring profits to the creator."""
        return self.db.execute_insert(
            "INSERT INTO payment_accounts (type, provider, account_id, metadata) "
            "VALUES ('transfer_out', ?, ?, ?)",
            (provider, account_id, json.dumps(metadata) if metadata else None),
        )

    def record_profit_transfer(self, amount: float, to_method: str,
                                from_account_id: int | None = None,
                                tx_reference: str = "",
                                metadata: dict | None = None) -> int:
        """Record a profit transfer to the creator."""
        tid = self.db.execute_insert(
            "INSERT INTO profit_transfers (amount, currency, from_account_id, "
            "to_method, tx_reference, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            (amount, self.config.currency, from_account_id, to_method,
             tx_reference, json.dumps(metadata) if metadata else None),
        )
        # Also record as a transaction in the main ledger
        self.db.execute_insert(
            "INSERT INTO transactions (type, category, amount, currency, description) "
            "VALUES ('expense', 'profit_transfer', ?, ?, ?)",
            (amount, self.config.currency, f"Profit transfer via {to_method}: {tx_reference}"),
        )
        logger.info(f"Profit transfer recorded: €{amount:.2f} via {to_method}")
        return tid

    def mark_transfer_completed(self, transfer_id: int, tx_reference: str = ""):
        self.db.execute(
            "UPDATE profit_transfers SET status = 'completed', completed_at = ?, "
            "tx_reference = COALESCE(?, tx_reference) WHERE id = ?",
            (datetime.now().isoformat(), tx_reference or None, transfer_id),
        )

    def get_transfer_history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM profit_transfers ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def get_total_transferred(self) -> float:
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM profit_transfers "
            "WHERE status = 'completed'"
        )
        return rows[0]["total"]

    def get_available_methods(self) -> list[dict[str, str]]:
        """Get available transfer methods ranked by anonymity."""
        return TRANSFER_METHODS

    def get_transferable_balance(self) -> float:
        """How much profit is available to transfer to the creator."""
        from monai.business.finance import Finance
        finance = Finance(self.db)
        net_profit = finance.get_net_profit()
        already_transferred = self.get_total_transferred()
        return max(0.0, net_profit - already_transferred)
