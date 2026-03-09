"""Phone Provisioner — acquires virtual phone numbers for platform signups.

Integrates with SMS API services to:
- Procure virtual numbers for platform verifications
- Receive SMS verification codes
- Route codes to requesting agents
- Manage number lifecycle (acquire → use → release)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from monai.agents.base import BaseAgent
from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)

PHONE_SCHEMA = """
CREATE TABLE IF NOT EXISTS virtual_phones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,             -- textverified, smspool, twilio, etc.
    phone_number TEXT NOT NULL,
    country TEXT DEFAULT 'US',
    status TEXT NOT NULL DEFAULT 'active',  -- active, used, expired, released
    used_for_platform TEXT,             -- which platform signup used this number
    used_by_agent TEXT,                 -- which agent requested it
    verification_code TEXT,             -- received code (if any)
    cost REAL DEFAULT 0.0,
    acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    released_at TIMESTAMP
);
"""

# Known SMS verification services with API access
SMS_PROVIDERS = {
    "smspool": {
        "base_url": "https://api.smspool.net",
        "endpoints": {
            "get_number": "/purchase/sms",
            "check_sms": "/sms/check",
            "get_balance": "/request/balance",
        },
        "avg_cost_usd": 0.50,
    },
    "textverified": {
        "base_url": "https://www.textverified.com/api",
        "endpoints": {
            "get_number": "/Verifications",
            "check_status": "/Verifications/{id}",
        },
        "avg_cost_usd": 1.00,
    },
}


class PhoneProvisioner(BaseAgent):
    name = "phone_provisioner"
    description = (
        "Acquires virtual phone numbers for platform signups. "
        "Integrates with SMS verification services to receive codes "
        "and route them to requesting agents."
    )

    def __init__(self, config: Config, db: Database, llm: LLM):
        super().__init__(config, db, llm)
        self._anonymizer = get_anonymizer(config)
        self._http = self._anonymizer.create_http_client(timeout=30)

        with db.connect() as conn:
            conn.executescript(PHONE_SCHEMA)

    def plan(self) -> list[str]:
        return ["check_inventory", "fulfill_requests"]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        inventory = self.get_inventory()
        return {"active_numbers": inventory.get("active", 0)}

    def get_number(self, platform: str, requesting_agent: str,
                   country: str = "US", provider: str = "smspool") -> dict[str, Any]:
        """Acquire a virtual phone number for a specific platform signup.

        Args:
            platform: Which platform needs the number (e.g., "upwork", "fiverr")
            requesting_agent: Which agent is requesting
            country: Country code for the number
            provider: SMS service provider to use

        Returns:
            Dict with phone number info or error
        """
        self.log_action("get_number", f"Requested by {requesting_agent} for {platform}")

        # Check if we already have an unused number
        existing = self.db.execute(
            "SELECT * FROM virtual_phones WHERE status = 'active' "
            "AND used_for_platform IS NULL LIMIT 1"
        )
        if existing:
            number = dict(existing[0])
            self.db.execute(
                "UPDATE virtual_phones SET used_for_platform = ?, used_by_agent = ? "
                "WHERE id = ?",
                (platform, requesting_agent, number["id"]),
            )
            return {
                "status": "reused",
                "phone_number": number["phone_number"],
                "provider": number["provider"],
                "phone_id": number["id"],
            }

        # Need to acquire a new number
        return self._acquire_number(provider, platform, requesting_agent, country)

    def _acquire_number(self, provider: str, platform: str,
                        requesting_agent: str, country: str) -> dict[str, Any]:
        """Acquire a new number from an SMS service provider."""
        provider_info = SMS_PROVIDERS.get(provider)
        if not provider_info:
            return {"status": "error", "reason": f"Unknown provider: {provider}"}

        # Record the intent — actual API call would go here
        # For now, log the attempt so the system knows what's needed
        self.log_action(
            "acquire_number",
            f"Provider: {provider}, Platform: {platform}, Country: {country}",
            "API integration needed",
        )

        # Record expense estimate
        estimated_cost = provider_info["avg_cost_usd"]
        self.record_expense(
            estimated_cost, "phone_verification",
            f"Virtual number for {platform} via {provider}",
        )

        return {
            "status": "pending_api_integration",
            "provider": provider,
            "platform": platform,
            "estimated_cost_usd": estimated_cost,
            "note": (
                f"SMS provider {provider} API integration needed. "
                f"Endpoint: {provider_info['base_url']}"
            ),
        }

    def check_verification(self, phone_id: int) -> dict[str, Any]:
        """Check if a verification code has been received."""
        rows = self.db.execute(
            "SELECT * FROM virtual_phones WHERE id = ?", (phone_id,)
        )
        if not rows:
            return {"status": "not_found"}

        phone = dict(rows[0])
        if phone.get("verification_code"):
            return {
                "status": "received",
                "code": phone["verification_code"],
                "phone_number": phone["phone_number"],
            }

        return {
            "status": "waiting",
            "phone_number": phone["phone_number"],
        }

    def release_number(self, phone_id: int):
        """Release a number that's no longer needed."""
        self.db.execute(
            "UPDATE virtual_phones SET status = 'released', released_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (phone_id,),
        )
        self.log_action("release_number", f"Released phone id={phone_id}")

    def get_inventory(self) -> dict[str, int]:
        """Get current phone number inventory by status."""
        rows = self.db.execute(
            "SELECT status, COUNT(*) as count FROM virtual_phones GROUP BY status"
        )
        return {r["status"]: r["count"] for r in rows}

    def get_costs(self) -> dict[str, Any]:
        """Get total costs for phone provisioning."""
        rows = self.db.execute(
            "SELECT SUM(cost) as total_cost, COUNT(*) as total_numbers "
            "FROM virtual_phones WHERE cost > 0"
        )
        if rows:
            return dict(rows[0])
        return {"total_cost": 0.0, "total_numbers": 0}
