"""Identity manager — monAI's self-managed identity system.

Manages the agent's name, emails, accounts, API keys, credentials,
and any digital identity it creates for itself across platforms.
"""

from __future__ import annotations

import json
import logging
import secrets
import string
from datetime import datetime
from pathlib import Path
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.utils.crypto import decrypt_value, encrypt_value
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

# Extend DB schema for identity management
IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,           -- email, platform_account, domain, api_key, payment
    platform TEXT NOT NULL,       -- gmail, upwork, fiverr, namecheap, stripe, etc.
    identifier TEXT NOT NULL,     -- email address, username, domain name, key name
    credentials TEXT,             -- encrypted/stored credentials (JSON)
    status TEXT DEFAULT 'active', -- active, suspended, pending_verification
    metadata TEXT,                -- additional JSON metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,  -- api_key, service, tool, subscription
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    config TEXT,                  -- JSON config
    cost_monthly REAL DEFAULT 0.0,
    status TEXT DEFAULT 'active',
    acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class IdentityManager:
    """Manages all of monAI's digital identities and resources."""

    def __init__(self, config: Config, db: Database, llm: LLM):
        self.config = config
        self.db = db
        self.llm = llm
        self._validator = None  # Lazy-loaded
        self._init_schema()
        self._ensure_base_identity()

    @property
    def validator(self):
        """Lazy-load NameValidator to avoid circular imports."""
        if self._validator is None:
            from monai.agents.name_validator import NameValidator
            self._validator = NameValidator(self.config, self.db, self.llm)
        return self._validator

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(IDENTITY_SCHEMA)

    def _ensure_base_identity(self):
        """Ensure the agent has a base identity configured."""
        existing = self.db.execute(
            "SELECT * FROM identities WHERE type = 'agent_identity' LIMIT 1"
        )
        if not existing:
            # Agent creates its own identity
            identity = self._generate_identity()
            self.db.execute_insert(
                "INSERT INTO identities (type, platform, identifier, metadata) "
                "VALUES ('agent_identity', 'self', ?, ?)",
                (identity["name"], json.dumps(identity)),
            )
            logger.info(f"Created base identity: {identity['name']}")

    def _generate_identity(self, platform: str = "",
                           validate: bool = True) -> dict[str, Any]:
        """Generate a professional business identity and validate it.

        Uses generate → validate → retry loop. If a name is taken,
        the LLM gets feedback about WHY it failed and generates a
        different name. Each platform gets a UNIQUE identity to
        prevent cross-platform correlation.

        Args:
            platform: Target platform (empty for base identity).
            validate: Whether to run real-world validation checks.
                      Set to False only in tests or when offline.
        """
        platform_hint = ""
        if platform:
            platform_hint = (
                f" This identity is specifically for the '{platform}' platform. "
                "It must be COMPLETELY DIFFERENT from identities used on other "
                "platforms — different company name, different username, different "
                "description. No overlap."
            )

        if validate:
            try:
                # Use the validator's generate-and-validate loop
                platforms_to_check = [platform] if platform else []
                identity, validation = self.validator.generate_and_validate(
                    platforms=platforms_to_check or None,
                    context=platform_hint,
                )

                if not validation.overall_viable:
                    logger.warning(
                        f"Identity '{identity.get('name')}' has blockers: "
                        f"{validation.blockers}. Using anyway (best attempt)."
                    )
                else:
                    logger.info(
                        f"Identity '{identity.get('name')}' validated successfully"
                    )

                return identity
            except Exception as e:
                logger.warning(
                    f"Validation failed ({e}), falling back to unvalidated generation"
                )

        # Fallback: generate without validation
        result = self.llm.quick_json(
            "Generate a professional business identity for a digital services company. "
            "The name must be unique and creative — NOT generic like 'Digital Solutions'. "
            "Return JSON: {\"name\": str (company name, professional sounding), "
            "\"tagline\": str, \"description\": str (what the company does), "
            "\"preferred_username\": str (lowercase, no spaces, unique random suffix), "
            "\"business_type\": str}" + platform_hint
        )
        return result

    def get_identity(self) -> dict[str, Any]:
        """Get the agent's current base identity."""
        rows = self.db.execute(
            "SELECT * FROM identities WHERE type = 'agent_identity' LIMIT 1"
        )
        if rows:
            meta = json.loads(rows[0]["metadata"] or "{}")
            return {**dict(rows[0]), **meta}
        return {}

    def _encrypt_credentials(self, credentials: dict | None) -> str | None:
        """Encrypt credentials before storing in DB."""
        if not credentials:
            return None
        return encrypt_value(json.dumps(credentials))

    def _decrypt_credentials(self, encrypted: str | None) -> dict | None:
        """Decrypt credentials read from DB."""
        if not encrypted:
            return None
        try:
            plaintext = decrypt_value(encrypted)
            return json.loads(plaintext)
        except Exception:
            # Fallback for legacy plaintext entries
            try:
                return json.loads(encrypted)
            except Exception:
                logger.warning("Failed to decrypt credentials")
                return None

    def store_account(self, platform: str, identifier: str,
                      credentials: dict | None = None, metadata: dict | None = None) -> int:
        """Store a new platform account with encrypted credentials.

        Each platform account gets a unique identity to prevent
        cross-platform correlation.
        """
        # Generate a platform-specific identity if metadata doesn't already include one
        if metadata is None:
            metadata = {}
        if "platform_identity" not in metadata:
            try:
                platform_identity = self._generate_identity(platform=platform)
                metadata["platform_identity"] = platform_identity
            except Exception as e:
                logger.warning(f"Could not generate platform identity for {platform}: {e}")

        return self.db.execute_insert(
            "INSERT INTO identities (type, platform, identifier, credentials, metadata) "
            "VALUES ('platform_account', ?, ?, ?, ?)",
            (platform, identifier,
             self._encrypt_credentials(credentials),
             json.dumps(metadata)),
        )

    def get_account(self, platform: str) -> dict[str, Any] | None:
        """Get stored account for a platform (decrypts credentials)."""
        rows = self.db.execute(
            "SELECT * FROM identities WHERE platform = ? AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            (platform,),
        )
        if rows:
            row = dict(rows[0])
            if row.get("credentials"):
                row["credentials"] = self._decrypt_credentials(row["credentials"])
            if row.get("metadata"):
                try:
                    row["metadata"] = json.loads(row["metadata"])
                except (json.JSONDecodeError, TypeError):
                    row["metadata"] = {}
            return row
        return None

    def has_account(self, platform: str) -> bool:
        rows = self.db.execute(
            "SELECT COUNT(*) as count FROM identities WHERE platform = ? AND status = 'active'",
            (platform,),
        )
        return rows[0]["count"] > 0

    def store_api_key(self, provider: str, key_name: str, key_value: str,
                      cost_monthly: float = 0.0) -> int:
        """Store an acquired API key (encrypted)."""
        self.db.execute_insert(
            "INSERT INTO identities (type, platform, identifier, credentials) "
            "VALUES ('api_key', ?, ?, ?)",
            (provider, key_name, self._encrypt_credentials({"key": key_value})),
        )
        return self.db.execute_insert(
            "INSERT INTO agent_resources (resource_type, name, provider, cost_monthly) "
            "VALUES ('api_key', ?, ?, ?)",
            (key_name, provider, cost_monthly),
        )

    def get_api_key(self, provider: str) -> str | None:
        rows = self.db.execute(
            "SELECT credentials FROM identities WHERE type = 'api_key' "
            "AND platform = ? AND status = 'active' LIMIT 1",
            (provider,),
        )
        if rows and rows[0]["credentials"]:
            creds = self._decrypt_credentials(rows[0]["credentials"])
            return creds.get("key") if creds else None
        return None

    def store_domain(self, domain: str, registrar: str, metadata: dict | None = None) -> int:
        return self.db.execute_insert(
            "INSERT INTO identities (type, platform, identifier, metadata) "
            "VALUES ('domain', ?, ?, ?)",
            (registrar, domain, json.dumps(metadata) if metadata else None),
        )

    def get_all_accounts(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM identities WHERE status = 'active' ORDER BY platform"
        )
        return [dict(r) for r in rows]

    def get_monthly_resource_costs(self) -> float:
        rows = self.db.execute(
            "SELECT COALESCE(SUM(cost_monthly), 0) as total FROM agent_resources WHERE status = 'active'"
        )
        return rows[0]["total"]

    def mark_suspended(self, platform: str, reason: str = "") -> None:
        """Mark a stored account/asset as suspended (dead, expired, revoked)."""
        self.db.execute(
            "UPDATE identities SET status = 'suspended', "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE platform = ? AND status = 'active'",
            (platform,),
        )
        logger.warning(f"Marked '{platform}' as suspended: {reason}")

    def verify_stored_assets(self, email_verifier) -> dict[str, Any]:
        """Verify that stored assets actually exist and work.

        Checks emails (Mailslurp inboxes) and API keys.  Marks dead
        assets as 'suspended' so the system stops using them.

        Returns a summary of what was verified and what was killed.
        """
        results: dict[str, Any] = {"verified": [], "suspended": [], "errors": []}

        # 1. Verify email accounts (Mailslurp inboxes)
        email_rows = self.db.execute(
            "SELECT id, platform, identifier, credentials, metadata "
            "FROM identities WHERE type = 'platform_account' "
            "AND platform = 'email' AND status = 'active'"
        )
        # Also check the primary email
        primary_rows = self.db.execute(
            "SELECT id, platform, identifier, credentials, metadata "
            "FROM identities WHERE type = 'agent_identity' "
            "AND status = 'active' AND platform = 'email'"
        )
        # And explicit email type
        email_type_rows = self.db.execute(
            "SELECT id, platform, identifier, credentials, metadata "
            "FROM identities WHERE platform = 'email' AND status = 'active'"
        )

        all_email_rows = {r["id"]: r for r in [*email_rows, *primary_rows, *email_type_rows]}
        for row in all_email_rows.values():
            row = dict(row)
            try:
                meta = json.loads(row.get("metadata") or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            inbox_id = meta.get("inbox_id")
            creds = self._decrypt_credentials(row.get("credentials"))
            if not inbox_id and creds:
                inbox_id = creds.get("inbox_id")
            identifier = row.get("identifier", "?")

            if inbox_id and email_verifier:
                # Verify the inbox still exists
                if email_verifier.verify_mailslurp_inbox(inbox_id):
                    results["verified"].append(f"email:{identifier}")
                    logger.info(f"Email verified: {identifier}")
                else:
                    # Dead inbox — mark suspended
                    self.db.execute(
                        "UPDATE identities SET status = 'suspended', "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (row["id"],),
                    )
                    results["suspended"].append(f"email:{identifier}")
                    logger.warning(f"Email DEAD, suspended: {identifier}")
            else:
                # No inbox_id — can't verify, leave as-is but warn
                results["errors"].append(
                    f"email:{identifier} — no inbox_id, can't verify")

        # 2. Verify Mailslurp API key itself
        if email_verifier:
            ms_key = email_verifier._mailslurp_key() if hasattr(
                email_verifier, '_mailslurp_key') else ""
            if ms_key:
                if email_verifier.verify_mailslurp_key(ms_key):
                    results["verified"].append("mailslurp_api_key")
                else:
                    results["suspended"].append("mailslurp_api_key")
                    logger.warning("Mailslurp API key is INVALID")

        summary = (
            f"Asset verification: {len(results['verified'])} OK, "
            f"{len(results['suspended'])} suspended, "
            f"{len(results['errors'])} unverifiable"
        )
        logger.info(summary)
        return results

    def generate_password(self, length: int = 20) -> str:
        chars = string.ascii_letters + string.digits + "!@#$%&*"
        return "".join(secrets.choice(chars) for _ in range(length))

    def generate_email_alias(self, base_domain: str = "") -> str:
        """Generate a unique email alias for platform registrations.

        Uses fully random usernames to prevent cross-platform correlation.
        """
        # Random username — no connection to base identity
        prefix = secrets.token_hex(3)  # 6 random hex chars
        word = secrets.choice(["dev", "team", "ops", "lab", "hub", "net", "pro", "app"])
        username = f"{word}{prefix}"
        if base_domain:
            return f"{username}@{base_domain}"
        return username
