"""Bootstrap funding — seed capital for monAI's first operations.

Bootstrap paths (choose one or combine):

    Path A: Creator donates via crowdfunding (RECOMMENDED)
        - Creator "donates" on Ko-fi like any other backer
        - Indistinguishable from organic backers on the platform
        - No anonymous prepaid card needed — simplest path
        - Tagged internally as creator_seed for audit trail

    Path B: Paysafecard voucher (€50-100 from tabaccheria, no ID required)
        - ONLY for domain + hosting of crowdfunding landing page
        - Retired as soon as crowdfunding or LLC bank is active
        - spend_limit_per_tx enforced (€50 max)

    Path C: Both — Paysafecard for initial domain, then creator + public crowdfunding

    AI Crowdfunding (all paths):
        - monAI declares itself as an AI and crowdfunds publicly
        - "The first AI-funded startup" — viral marketing angle
        - Funds collected via Ko-fi / Buy Me a Coffee / Gumroad (no LLC needed)
        - Used for: LLC formation, registered agent, bank account, first months

    Self-sustaining (LLC active):
        - All revenue flows through LLC bank
        - Prepaid card retired, crowdfunding optional
        - monAI pays its own bills from revenue

The bootstrap wallet tracks every euro spent and integrates with Commercialista.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.config import Config
from monai.db.database import Database

logger = logging.getLogger(__name__)

BOOTSTRAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS bootstrap_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,              -- prepaid_card, crowdfunding, creator_topup
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    description TEXT NOT NULL,         -- What was purchased
    vendor TEXT,                       -- Who was paid
    category TEXT NOT NULL,            -- domain, hosting, llc_formation, registered_agent, platform_fee
    card_last4 TEXT,                   -- Last 4 digits of card used (if prepaid)
    status TEXT DEFAULT 'completed',   -- pending, completed, failed, refunded
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crowdfunding_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,            -- kofi, buymeacoffee, gumroad, github_sponsors
    campaign_url TEXT,                 -- Public URL of the campaign
    goal_amount REAL DEFAULT 500.0,    -- Funding goal in EUR
    raised_amount REAL DEFAULT 0,      -- Total raised so far
    currency TEXT DEFAULT 'EUR',
    backer_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'draft',       -- draft, active, funded, closed
    title TEXT,
    description TEXT,
    platform_account_id TEXT,          -- Account/page ID on the platform
    webhook_secret TEXT,               -- For payment notifications
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crowdfunding_contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES crowdfunding_campaigns(id),
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    backer_name TEXT DEFAULT 'Anonymous',
    backer_email TEXT,
    message TEXT,                      -- Backer message
    platform_tx_id TEXT,              -- Transaction ID from platform
    status TEXT DEFAULT 'completed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""";


# Allowed spending categories for bootstrap (prepaid card)
BOOTSTRAP_CATEGORIES = {
    "domain", "hosting", "crowdfunding_setup",
}

# Categories that require crowdfunding or LLC funds (NOT prepaid card)
INFRASTRUCTURE_CATEGORIES = {
    "llc_formation", "registered_agent", "bank_setup",
    "stripe_setup", "platform_fee", "software", "api_credits",
}

# Crowdfunding platforms that don't require an LLC
NO_LLC_PLATFORMS = {
    "kofi": {
        "name": "Ko-fi",
        "url": "https://ko-fi.com",
        "fee_pct": 0.0,  # Ko-fi takes 0% on donations
        "payout_method": "paypal_or_stripe",
        "requires_llc": False,
        "min_payout": 1.0,
    },
    "buymeacoffee": {
        "name": "Buy Me a Coffee",
        "url": "https://buymeacoffee.com",
        "fee_pct": 5.0,
        "payout_method": "stripe_or_bank",
        "requires_llc": False,
        "min_payout": 5.0,
    },
    "gumroad": {
        "name": "Gumroad",
        "url": "https://gumroad.com",
        "fee_pct": 10.0,
        "payout_method": "stripe_or_paypal",
        "requires_llc": False,
        "min_payout": 10.0,
    },
    "github_sponsors": {
        "name": "GitHub Sponsors",
        "url": "https://github.com/sponsors",
        "fee_pct": 0.0,  # GitHub takes 0%
        "payout_method": "stripe",
        "requires_llc": False,
        "min_payout": 0.0,
    },
}


class BootstrapWallet:
    """Manages monAI's seed capital from anonymous prepaid card + crowdfunding."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(BOOTSTRAP_SCHEMA)

    # ── Prepaid Card Spending ──────────────────────────────────

    def can_spend_prepaid(self, amount: float, category: str) -> dict[str, Any]:
        """Check if we can spend from the prepaid card.

        Rules:
        - Card must be enabled and not retired
        - Amount must be under spend_limit_per_tx
        - Category must be in BOOTSTRAP_CATEGORIES (domain, hosting only)
        - Total spent must not exceed loaded_amount
        """
        wallet = self.config.bootstrap_wallet

        if not wallet.enabled:
            return {"allowed": False, "reason": "Bootstrap wallet not enabled"}
        if wallet.retired:
            return {"allowed": False, "reason": "Prepaid card has been retired"}
        if amount > wallet.spend_limit_per_tx:
            return {"allowed": False, "reason": f"Amount €{amount:.2f} exceeds per-tx limit €{wallet.spend_limit_per_tx:.2f}"}
        if category not in BOOTSTRAP_CATEGORIES:
            return {"allowed": False, "reason": f"Category '{category}' not allowed for prepaid card. Only: {BOOTSTRAP_CATEGORIES}"}

        spent = self.get_prepaid_total_spent()
        remaining = wallet.loaded_amount - spent
        if amount > remaining:
            return {"allowed": False, "reason": f"Insufficient balance. Remaining: €{remaining:.2f}"}

        return {"allowed": True, "remaining_after": remaining - amount}

    def spend_prepaid(self, amount: float, description: str,
                      category: str, vendor: str = "") -> dict[str, Any]:
        """Record a prepaid card purchase. Enforces all spending rules."""
        check = self.can_spend_prepaid(amount, category)
        if not check["allowed"]:
            return {"error": check["reason"]}

        wallet = self.config.bootstrap_wallet
        if wallet.method == "paysafecard" and wallet.paysafecard_pin:
            card_last4 = wallet.paysafecard_pin[-4:]
        elif wallet.card_number:
            card_last4 = wallet.card_number[-4:]
        else:
            card_last4 = "????"

        tx_id = self.db.execute_insert(
            "INSERT INTO bootstrap_transactions "
            "(source, amount, description, vendor, category, card_last4) "
            "VALUES ('prepaid_card', ?, ?, ?, ?, ?)",
            (amount, description, vendor, category, card_last4),
        )

        logger.info(f"Bootstrap prepaid spend: €{amount:.2f} — {description} ({category})")

        return {
            "id": tx_id,
            "source": "prepaid_card",
            "amount": amount,
            "remaining": check["remaining_after"],
            "category": category,
        }

    def get_prepaid_total_spent(self) -> float:
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total "
            "FROM bootstrap_transactions "
            "WHERE source = 'prepaid_card' AND status = 'completed'"
        )
        return rows[0]["total"] if rows else 0.0

    def get_prepaid_remaining(self) -> float:
        return self.config.bootstrap_wallet.loaded_amount - self.get_prepaid_total_spent()

    def retire_prepaid(self) -> None:
        """Mark the prepaid card as retired (LLC bank or crowdfunding active)."""
        self.config.bootstrap_wallet.retired = True
        logger.info("Bootstrap prepaid card retired — switching to crowdfunding/LLC funds")

    # ── Creator Seed Donation ────────────────────────────────────

    def record_creator_donation(self, campaign_id: int, amount: float,
                                alias: str = "Anonymous") -> dict[str, Any]:
        """Creator donates to the crowdfunding campaign like any other backer.

        From the platform's perspective, this is a normal donation.
        Internally tracked as 'creator_seed' for audit trail.
        The alias should NOT be the creator's real name.
        """
        campaign = self.get_campaign(campaign_id)
        if not campaign:
            return {"error": f"Campaign {campaign_id} not found"}

        # Record as a normal contribution on the platform side
        contrib_id = self.db.execute_insert(
            "INSERT INTO crowdfunding_contributions "
            "(campaign_id, amount, backer_name, message, platform_tx_id) "
            "VALUES (?, ?, ?, 'Seed donation', 'creator_seed')",
            (campaign_id, amount, alias),
        )

        # Update campaign totals (same as any backer)
        self.db.execute(
            "UPDATE crowdfunding_campaigns SET "
            "raised_amount = raised_amount + ?, "
            "backer_count = backer_count + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (amount, campaign_id),
        )

        # Check if goal reached
        campaign = self.get_campaign(campaign_id)
        if campaign and campaign["raised_amount"] >= campaign["goal_amount"]:
            self.db.execute(
                "UPDATE crowdfunding_campaigns SET status = 'funded', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (campaign_id,),
            )

        # Record as bootstrap transaction with creator_seed source
        self.db.execute_insert(
            "INSERT INTO bootstrap_transactions "
            "(source, amount, description, vendor, category) "
            "VALUES ('creator_seed', ?, ?, ?, 'crowdfunding_income')",
            (amount, f"Creator seed donation via {campaign['platform']}",
             f"campaign_{campaign_id}"),
        )

        logger.info(f"Creator seed donation: €{amount:.2f} to campaign {campaign_id} as '{alias}'")

        return {
            "id": contrib_id,
            "source": "creator_seed",
            "amount": amount,
            "campaign_id": campaign_id,
            "alias_used": alias,
            "campaign_raised": campaign["raised_amount"],
        }

    def get_creator_seed_total(self) -> float:
        """Total amount the creator has seeded via crowdfunding."""
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total "
            "FROM bootstrap_transactions "
            "WHERE source = 'creator_seed' AND status = 'completed'"
        )
        return rows[0]["total"] if rows else 0.0

    # ── Crowdfunding ───────────────────────────────────────────

    def create_campaign(self, platform: str, title: str,
                        description: str, goal_amount: float = 500.0,
                        campaign_url: str = "",
                        platform_account_id: str = "",
                        webhook_secret: str = "") -> int:
        """Register a crowdfunding campaign."""
        if platform not in NO_LLC_PLATFORMS:
            logger.warning(f"Platform '{platform}' not in known no-LLC platforms")

        campaign_id = self.db.execute_insert(
            "INSERT INTO crowdfunding_campaigns "
            "(platform, campaign_url, goal_amount, title, description, "
            "platform_account_id, webhook_secret, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
            (platform, campaign_url, goal_amount, title, description,
             platform_account_id, webhook_secret),
        )

        logger.info(f"Crowdfunding campaign created: {title} on {platform} (goal: €{goal_amount:.2f})")
        return campaign_id

    def record_contribution(self, campaign_id: int, amount: float,
                            backer_name: str = "Anonymous",
                            backer_email: str = "",
                            message: str = "",
                            platform_tx_id: str = "") -> int:
        """Record an incoming crowdfunding contribution."""
        contrib_id = self.db.execute_insert(
            "INSERT INTO crowdfunding_contributions "
            "(campaign_id, amount, backer_name, backer_email, message, platform_tx_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (campaign_id, amount, backer_name, backer_email, message, platform_tx_id),
        )

        # Update campaign totals
        self.db.execute(
            "UPDATE crowdfunding_campaigns SET "
            "raised_amount = raised_amount + ?, "
            "backer_count = backer_count + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (amount, campaign_id),
        )

        # Check if goal reached
        campaign = self.get_campaign(campaign_id)
        if campaign and campaign["raised_amount"] >= campaign["goal_amount"]:
            self.db.execute(
                "UPDATE crowdfunding_campaigns SET status = 'funded', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (campaign_id,),
            )
            logger.info(f"Campaign {campaign_id} FUNDED! Raised €{campaign['raised_amount']:.2f}")

        # Also record as bootstrap transaction
        self.db.execute_insert(
            "INSERT INTO bootstrap_transactions "
            "(source, amount, description, vendor, category) "
            "VALUES ('crowdfunding', ?, ?, ?, 'crowdfunding_income')",
            (amount, f"Contribution from {backer_name}", f"campaign_{campaign_id}"),
        )

        return contrib_id

    def spend_crowdfunding(self, amount: float, description: str,
                           category: str, vendor: str = "") -> dict[str, Any]:
        """Spend from crowdfunding funds. Allowed for infrastructure categories."""
        available = self.get_crowdfunding_available()
        if amount > available:
            return {"error": f"Insufficient crowdfunding balance. Available: €{available:.2f}"}

        tx_id = self.db.execute_insert(
            "INSERT INTO bootstrap_transactions "
            "(source, amount, description, vendor, category) "
            "VALUES ('crowdfunding', ?, ?, ?, ?)",
            (-amount, description, vendor, category),
        )

        logger.info(f"Crowdfunding spend: €{amount:.2f} — {description} ({category})")

        return {
            "id": tx_id,
            "source": "crowdfunding",
            "amount": amount,
            "remaining": available - amount,
            "category": category,
        }

    def get_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        rows = self.db.execute(
            "SELECT * FROM crowdfunding_campaigns WHERE id = ?",
            (campaign_id,),
        )
        return dict(rows[0]) if rows else None

    def get_active_campaigns(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM crowdfunding_campaigns WHERE status = 'active' "
            "ORDER BY created_at DESC"
        )]

    def get_campaign_contributions(self, campaign_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM crowdfunding_contributions WHERE campaign_id = ? "
            "ORDER BY created_at DESC",
            (campaign_id,),
        )]

    def get_crowdfunding_total_raised(self) -> float:
        """Total raised from all crowdfunding sources (organic + creator seed)."""
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total "
            "FROM bootstrap_transactions "
            "WHERE source IN ('crowdfunding', 'creator_seed') "
            "AND amount > 0 AND status = 'completed'"
        )
        return rows[0]["total"] if rows else 0.0

    def get_crowdfunding_total_spent(self) -> float:
        rows = self.db.execute(
            "SELECT COALESCE(SUM(ABS(amount)), 0) as total "
            "FROM bootstrap_transactions "
            "WHERE source = 'crowdfunding' AND amount < 0 AND status = 'completed'"
        )
        return rows[0]["total"] if rows else 0.0

    def get_crowdfunding_available(self) -> float:
        return self.get_crowdfunding_total_raised() - self.get_crowdfunding_total_spent()

    # ── Overall Bootstrap Status ───────────────────────────────

    def get_funding_phase(self) -> str:
        """Determine current funding phase.

        Returns: 'pre_bootstrap', 'prepaid_active', 'crowdfunding',
                 'self_sustaining'
        """
        wallet = self.config.bootstrap_wallet

        # Check if LLC bank is active (self-sustaining)
        from monai.business.corporate import CorporateManager
        corp = CorporateManager(self.db)
        entity = corp.get_primary_entity()
        if entity and entity.get("bank_account_id"):
            return "self_sustaining"

        # Check crowdfunding status (includes creator seed donations)
        campaigns = self.get_active_campaigns()
        if campaigns or self.get_crowdfunding_total_raised() > 0:
            return "crowdfunding"

        # Check prepaid card
        if wallet.enabled and not wallet.retired:
            return "prepaid_active"

        return "pre_bootstrap"

    def get_bootstrap_summary(self) -> dict[str, Any]:
        """Full picture of bootstrap funding status."""
        wallet = self.config.bootstrap_wallet
        phase = self.get_funding_phase()

        prepaid_spent = self.get_prepaid_total_spent()
        crowdfunding_raised = self.get_crowdfunding_total_raised()
        crowdfunding_spent = self.get_crowdfunding_total_spent()
        creator_seed = self.get_creator_seed_total()

        all_txs = self.db.execute(
            "SELECT * FROM bootstrap_transactions ORDER BY created_at DESC LIMIT 10"
        )

        campaigns = self.db.execute(
            "SELECT * FROM crowdfunding_campaigns ORDER BY created_at DESC"
        )

        return {
            "phase": phase,
            "prepaid_card": {
                "enabled": wallet.enabled,
                "retired": wallet.retired,
                "loaded": wallet.loaded_amount,
                "spent": prepaid_spent,
                "remaining": wallet.loaded_amount - prepaid_spent if wallet.enabled else 0,
            },
            "crowdfunding": {
                "total_raised": crowdfunding_raised,
                "creator_seed": creator_seed,
                "organic_raised": crowdfunding_raised - creator_seed,
                "total_spent": crowdfunding_spent,
                "available": crowdfunding_raised - crowdfunding_spent,
                "campaigns": [dict(c) for c in campaigns],
            },
            "total_bootstrap_funds": (
                (wallet.loaded_amount if wallet.enabled else 0)
                + crowdfunding_raised
            ),
            "total_bootstrap_spent": prepaid_spent + crowdfunding_spent,
            "recent_transactions": [dict(t) for t in all_txs],
        }

    def get_all_transactions(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM bootstrap_transactions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )]
