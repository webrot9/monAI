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
    order_id TEXT,                      -- provider order/transaction ID for polling
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
        self.__http = None  # Lazy — only created when needed

        with db.connect() as conn:
            conn.executescript(PHONE_SCHEMA)

    @property
    def _http(self):
        """Lazy http client — avoids import errors when socksio isn't installed."""
        if self.__http is None:
            self.__http = self._anonymizer.create_http_client(timeout=30)
        return self.__http

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

        api_key = self._get_api_key(provider)
        if not api_key:
            return {
                "status": "error",
                "reason": f"No API key configured for {provider}. "
                          f"Set sms.{provider}_api_key in config or store via identity agent.",
            }

        try:
            if provider == "smspool":
                result = self._acquire_smspool(api_key, platform, country)
            elif provider == "textverified":
                result = self._acquire_textverified(api_key, platform, country)
            else:
                return {"status": "error", "reason": f"No acquisition logic for: {provider}"}
        except Exception as e:
            logger.error(f"SMS acquisition failed ({provider}): {e}")
            self.log_action("acquire_number_failed", str(e))
            return {"status": "error", "reason": str(e)}

        if result.get("status") != "acquired":
            return result

        # Record in DB
        phone_id = self.db.execute_insert(
            "INSERT INTO virtual_phones "
            "(provider, phone_number, country, status, used_for_platform, "
            "used_by_agent, order_id, cost, expires_at) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (provider, result["phone_number"], country, platform,
             requesting_agent, result.get("order_id", ""),
             result.get("cost", 0), result.get("expires_at")),
        )

        self.record_expense(
            result.get("cost", 0), "phone_verification",
            f"Virtual number for {platform} via {provider}",
        )
        self.log_action(
            "acquire_number",
            f"Got {result['phone_number']} from {provider} for {platform}",
        )

        return {
            "status": "acquired",
            "phone_number": result["phone_number"],
            "provider": provider,
            "phone_id": phone_id,
            "order_id": result.get("order_id"),
            "cost": result.get("cost", 0),
        }

    def _get_api_key(self, provider: str) -> str | None:
        """Retrieve API key for the SMS provider from identity store."""
        # Check identity store (encrypted credentials)
        rows = self.db.execute(
            "SELECT credentials FROM identities "
            "WHERE platform = ? AND type = 'api_key' AND status = 'active' LIMIT 1",
            (f"sms_{provider}",),
        )
        if rows and rows[0]["credentials"]:
            from monai.utils.crypto import decrypt_value
            try:
                import json as _json
                decrypted = decrypt_value(rows[0]["credentials"])
                data = _json.loads(decrypted)
                return data.get("key", "")
            except Exception:
                # Try as plaintext fallback
                try:
                    data = _json.loads(rows[0]["credentials"])
                    return data.get("key", "")
                except Exception:
                    return None
        return None

    # ── SMSPool Integration ──────────────────────────────────────

    def _acquire_smspool(self, api_key: str, platform: str,
                         country: str) -> dict[str, Any]:
        """Acquire a number via SMSPool API (https://api.smspool.net)."""
        base = SMS_PROVIDERS["smspool"]["base_url"]

        # Step 1: Purchase SMS
        resp = self._http.post(
            f"{base}/purchase/sms",
            data={
                "key": api_key,
                "country": self._smspool_country_id(country),
                "service": self._smspool_service_id(platform),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("success") == 0:
            return {"status": "error", "reason": data.get("message", "SMSPool error")}

        return {
            "status": "acquired",
            "phone_number": data.get("phonenumber", data.get("number", "")),
            "order_id": str(data.get("order_id", "")),
            "cost": float(data.get("cost", data.get("price", 0))),
            "expires_at": None,  # SMSPool orders auto-expire after ~15 min
        }

    def _smspool_country_id(self, country: str) -> str:
        """Map ISO country code to SMSPool country ID."""
        mapping = {"US": "1", "UK": "10", "CA": "36", "DE": "43", "FR": "16"}
        return mapping.get(country.upper(), "1")

    def _smspool_service_id(self, platform: str) -> str:
        """Map platform name to SMSPool service ID."""
        mapping = {
            "twitter": "1", "google": "9", "facebook": "3",
            "instagram": "6", "whatsapp": "15", "telegram": "14",
            "linkedin": "50", "fiverr": "283", "upwork": "619",
            "gumroad": "0", "stripe": "0",  # "0" = any service
        }
        return mapping.get(platform.lower(), "0")

    # ── TextVerified Integration ─────────────────────────────────

    def _acquire_textverified(self, api_key: str, platform: str,
                              country: str) -> dict[str, Any]:
        """Acquire a number via TextVerified API."""
        base = SMS_PROVIDERS["textverified"]["base_url"]

        # TextVerified uses bearer auth + service-based ordering
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # Create verification
        resp = self._http.post(
            f"{base}/Verifications",
            headers=headers,
            json={
                "id": self._textverified_service_name(platform),
                "capability": "sms",
                "method": "Standard",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if "number" not in data:
            return {"status": "error", "reason": data.get("message", "TextVerified error")}

        return {
            "status": "acquired",
            "phone_number": data["number"],
            "order_id": str(data.get("id", "")),
            "cost": float(data.get("cost", 1.0)),
            "expires_at": data.get("expires_at"),
        }

    def _textverified_service_name(self, platform: str) -> str:
        """Map platform name to TextVerified service name."""
        mapping = {
            "twitter": "Twitter", "google": "Google", "facebook": "Facebook",
            "instagram": "Instagram", "whatsapp": "WhatsApp", "telegram": "Telegram",
            "linkedin": "LinkedIn", "fiverr": "Fiverr", "upwork": "Upwork",
        }
        return mapping.get(platform.lower(), platform.title())

    # ── Verification Code Polling ────────────────────────────────

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

        # Poll the provider for new SMS
        code = self._poll_provider(phone)
        if code:
            self.db.execute(
                "UPDATE virtual_phones SET verification_code = ?, status = 'used' "
                "WHERE id = ?",
                (code, phone_id),
            )
            return {
                "status": "received",
                "code": code,
                "phone_number": phone["phone_number"],
            }

        return {
            "status": "waiting",
            "phone_number": phone["phone_number"],
        }

    def wait_for_code(self, phone_id: int, timeout: int = 120,
                      poll_interval: int = 5) -> dict[str, Any]:
        """Block until a verification code is received or timeout."""
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            result = self.check_verification(phone_id)
            if result["status"] == "received":
                return result
            if result["status"] == "not_found":
                return result
            _time.sleep(poll_interval)
        return {"status": "timeout", "phone_id": phone_id}

    def _poll_provider(self, phone: dict) -> str | None:
        """Poll the SMS provider API for verification code."""
        provider = phone.get("provider", "")
        api_key = self._get_api_key(provider)
        if not api_key:
            return None

        try:
            if provider == "smspool":
                return self._poll_smspool(api_key, phone)
            elif provider == "textverified":
                return self._poll_textverified(api_key, phone)
        except Exception as e:
            logger.warning(f"SMS poll failed ({provider}): {e}")
        return None

    def _poll_smspool(self, api_key: str, phone: dict) -> str | None:
        """Check SMSPool for received SMS."""
        base = SMS_PROVIDERS["smspool"]["base_url"]
        order_id = phone.get("order_id", "")
        if not order_id:
            return None

        resp = self._http.post(
            f"{base}/sms/check",
            data={"key": api_key, "orderid": order_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("success") == 1 and data.get("sms"):
            return self._extract_code(data["sms"])
        return None

    def _poll_textverified(self, api_key: str, phone: dict) -> str | None:
        """Check TextVerified for received SMS."""
        base = SMS_PROVIDERS["textverified"]["base_url"]
        order_id = phone.get("order_id", "")
        if not order_id:
            return None

        resp = self._http.get(
            f"{base}/Verifications/{order_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        sms_text = data.get("sms") or data.get("code") or ""
        if sms_text:
            return self._extract_code(sms_text)
        return None

    def _extract_code(self, sms_text: str) -> str:
        """Extract verification code from SMS text."""
        import re
        # Match 4-8 digit codes
        match = re.search(r'\b(\d{4,8})\b', sms_text)
        return match.group(1) if match else sms_text.strip()

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
