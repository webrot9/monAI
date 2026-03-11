"""Ko-fi campaign setup automation.

Automates creation and monitoring of Ko-fi crowdfunding campaigns:
1. Registers on Ko-fi via browser automation (if no account exists)
2. Creates a campaign page with monAI's story
3. Configures payment methods and goals
4. Monitors donations and syncs with BootstrapWallet
5. Updates the landing page with live funding progress
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from monai.agents.base import BaseAgent
from monai.business.bootstrap import BootstrapWallet
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

KOFI_BASE_URL = "https://ko-fi.com"
KOFI_API_URL = "https://ko-fi.com/api"

# Ko-fi campaign content template
CAMPAIGN_CONTENT = {
    "title": "monAI — The First AI-Funded Startup",
    "description": (
        "monAI is a fully autonomous AI agent that builds real businesses, "
        "earns real money, and operates with zero human intervention. "
        "Your support helps fund the infrastructure (LLC, hosting, domains) "
        "that monAI needs to become self-sustaining. "
        "Once profitable, monAI funds itself — your contribution bootstraps "
        "the future of autonomous AI entrepreneurship."
    ),
    "goal_amount": 500.0,
    "tiers": [
        {"amount": 10, "name": "Supporter", "description": "Your name in the backer list + Telegram group access"},
        {"amount": 50, "name": "Champion", "description": "Weekly reports + product roadmap input"},
        {"amount": 200, "name": "Founder", "description": "Advisory role + priority on all monAI services"},
    ],
}

KOFI_SCHEMA = """
CREATE TABLE IF NOT EXISTS kofi_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER REFERENCES crowdfunding_campaigns(id),
    kofi_page_url TEXT,
    kofi_username TEXT,
    kofi_email TEXT,
    status TEXT DEFAULT 'pending',   -- pending, page_created, live, paused
    last_sync_at TIMESTAMP,
    donations_synced INTEGER DEFAULT 0,
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class KofiCampaignManager(BaseAgent):
    """Automates Ko-fi campaign creation and donation monitoring."""

    name = "kofi_manager"
    description = "Creates and manages Ko-fi crowdfunding campaigns for monAI bootstrap funding."

    def __init__(self, config: Config, db: Database, llm: LLM,
                 bootstrap_wallet: BootstrapWallet | None = None):
        super().__init__(config, db, llm)
        self.bootstrap_wallet = bootstrap_wallet or BootstrapWallet(config, db)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(KOFI_SCHEMA)

    def plan(self) -> list[str]:
        """Plan Ko-fi campaign setup steps."""
        existing = self._get_active_campaign()
        if existing:
            return ["Sync donations from Ko-fi", "Update landing page with progress"]
        return [
            "Register Ko-fi account via browser automation",
            "Create campaign page with monAI story",
            "Configure payment methods and goal",
            "Register campaign in bootstrap system",
            "Generate and deploy landing page with Ko-fi link",
        ]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        """Execute Ko-fi campaign lifecycle."""
        existing = self._get_active_campaign()

        if existing:
            # Campaign exists — sync donations
            return self._sync_donations(existing)

        # No campaign — set up from scratch
        return self._setup_campaign()

    # ── Campaign Setup ───────────────────────────────────────────

    def _setup_campaign(self) -> dict[str, Any]:
        """Full autonomous Ko-fi campaign setup."""
        result: dict[str, Any] = {"steps": {}}

        # Step 1: Register Ko-fi account (browser automation)
        account = self._register_kofi_account()
        result["steps"]["register"] = account
        if account.get("status") == "error":
            return {**result, "status": "error", "error": account.get("error")}

        kofi_username = account.get("username", "monai")
        kofi_url = f"{KOFI_BASE_URL}/{kofi_username}"

        # Step 2: Create the campaign page
        page = self._create_campaign_page(kofi_username)
        result["steps"]["page"] = page

        # Step 3: Register in bootstrap system
        campaign_id = self.bootstrap_wallet.create_campaign(
            platform="kofi",
            title=CAMPAIGN_CONTENT["title"],
            description=CAMPAIGN_CONTENT["description"],
            goal_amount=CAMPAIGN_CONTENT["goal_amount"],
            campaign_url=kofi_url,
            platform_account_id=kofi_username,
        )
        result["campaign_id"] = campaign_id

        # Step 4: Track in kofi_campaigns table
        self.db.execute_insert(
            "INSERT INTO kofi_campaigns "
            "(campaign_id, kofi_page_url, kofi_username, kofi_email, status) "
            "VALUES (?, ?, ?, ?, 'live')",
            (campaign_id, kofi_url, kofi_username, account.get("email", "")),
        )

        result["status"] = "live"
        result["kofi_url"] = kofi_url
        result["campaign_id"] = campaign_id

        self.log_action("kofi_setup_complete", f"Campaign live at {kofi_url}")
        self.share_knowledge(
            "crowdfunding", "kofi_campaign",
            f"Ko-fi campaign live at {kofi_url} — goal €{CAMPAIGN_CONTENT['goal_amount']}",
            tags=["kofi", "crowdfunding", "live"],
        )

        return result

    def _register_kofi_account(self) -> dict[str, Any]:
        """Register a Ko-fi account using browser automation."""
        # Check if identity already has Ko-fi credentials
        existing = self.get_platform_credentials("kofi")
        if existing:
            return {
                "status": "already_registered",
                "username": existing.get("username", ""),
                "email": existing.get("email", ""),
            }

        # Use BaseAgent's ensure_platform_account to self-provision
        try:
            account_result = self.ensure_platform_account("kofi")
            if account_result.get("status") in ("ready", "created"):
                creds = self.get_platform_credentials("kofi")
                return {
                    "status": "registered",
                    "username": creds.get("username", "monai") if creds else "monai",
                    "email": creds.get("email", "") if creds else "",
                }
            return {"status": "error", "error": "Could not register on Ko-fi"}
        except Exception as e:
            logger.error(f"Ko-fi registration failed: {e}")
            return {"status": "error", "error": str(e)}

    def _create_campaign_page(self, username: str) -> dict[str, Any]:
        """Configure the Ko-fi page with campaign content.

        Uses browser automation to set the page title, description,
        and donation tiers.
        """
        try:
            # Use platform_action to configure the page
            result = self.platform_action("kofi", "configure_page", {
                "title": CAMPAIGN_CONTENT["title"],
                "description": CAMPAIGN_CONTENT["description"],
                "goal_amount": CAMPAIGN_CONTENT["goal_amount"],
                "tiers": CAMPAIGN_CONTENT["tiers"],
            })
            return {"status": "configured", "detail": result}
        except Exception as e:
            logger.warning(f"Ko-fi page configuration via API failed: {e}")
            # Fall back to browser automation
            try:
                self.browse_and_extract(
                    f"{KOFI_BASE_URL}/manage/page-settings",
                    f"Set page title to '{CAMPAIGN_CONTENT['title']}' and "
                    f"description to '{CAMPAIGN_CONTENT['description'][:200]}'"
                )
                return {"status": "configured_via_browser"}
            except Exception as e2:
                logger.error(f"Ko-fi page setup failed entirely: {e2}")
                return {"status": "error", "error": str(e2)}

    # ── Donation Sync ────────────────────────────────────────────

    def _sync_donations(self, campaign: dict[str, Any]) -> dict[str, Any]:
        """Sync recent donations from Ko-fi into the bootstrap system."""
        campaign_id = campaign["campaign_id"]
        kofi_username = campaign.get("kofi_username", "")

        try:
            # Fetch recent donations via Ko-fi page scraping
            donations_data = self.browse_and_extract(
                f"{KOFI_BASE_URL}/{kofi_username}",
                "Extract all recent supporter names and donation amounts from this page. "
                "Return as JSON: {\"donations\": [{\"name\": str, \"amount\": float}]}"
            )

            if not donations_data or not isinstance(donations_data, dict):
                return {"status": "sync_ok", "new_donations": 0}

            donations = donations_data.get("donations", [])
            new_count = 0

            for donation in donations:
                name = donation.get("name", "Anonymous")
                amount = donation.get("amount", 0)
                if amount <= 0:
                    continue

                # Check if already recorded (by name+amount combo)
                existing = self.db.execute(
                    "SELECT id FROM crowdfunding_contributions "
                    "WHERE campaign_id = ? AND backer_name = ? AND amount = ? "
                    "LIMIT 1",
                    (campaign_id, name, amount),
                )
                if existing:
                    continue

                self.bootstrap_wallet.record_contribution(
                    campaign_id=campaign_id,
                    amount=amount,
                    backer_name=name,
                )
                new_count += 1

            # Update sync timestamp
            self.db.execute(
                "UPDATE kofi_campaigns SET last_sync_at = CURRENT_TIMESTAMP, "
                "donations_synced = donations_synced + ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE campaign_id = ?",
                (new_count, campaign_id),
            )

            self.log_action("kofi_sync", f"Synced {new_count} new donations")
            return {"status": "sync_ok", "new_donations": new_count}

        except Exception as e:
            logger.error(f"Ko-fi donation sync failed: {e}")
            return {"status": "sync_error", "error": str(e)}

    # ── Helpers ──────────────────────────────────────────────────

    def _get_active_campaign(self) -> dict[str, Any] | None:
        """Get the active Ko-fi campaign, if any."""
        rows = self.db.execute(
            "SELECT * FROM kofi_campaigns WHERE status = 'live' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        return dict(rows[0]) if rows else None

    def get_campaign_status(self) -> dict[str, Any]:
        """Get full campaign status including funding progress."""
        campaign = self._get_active_campaign()
        if not campaign:
            return {"status": "no_campaign"}

        # Get funding progress from bootstrap
        bs_campaign = self.bootstrap_wallet.get_campaign(campaign["campaign_id"])
        if not bs_campaign:
            return {"status": "campaign_missing_from_bootstrap"}

        return {
            "status": campaign["status"],
            "kofi_url": campaign.get("kofi_page_url", ""),
            "raised": bs_campaign.get("raised_amount", 0),
            "goal": bs_campaign.get("goal_amount", 500),
            "backers": bs_campaign.get("backer_count", 0),
            "progress_pct": round(
                (bs_campaign.get("raised_amount", 0) / max(bs_campaign.get("goal_amount", 500), 1)) * 100, 1
            ),
            "last_sync": campaign.get("last_sync_at"),
            "donations_synced": campaign.get("donations_synced", 0),
        }
