"""Asset awareness — agents know exactly what they have and what they don't.

Before ANY action that requires a resource (email, account, domain, payment method),
the agent checks its asset inventory. If the resource doesn't exist, it either:
1. Creates it first (dependency resolution)
2. Fails clearly ("I don't have X, cannot do Y")
3. NEVER uses fake/placeholder values (example.com, fake@email, etc.)

This is the bridge between "what do I want to do" and "what can I actually do."
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)


@dataclass
class Asset:
    """A real resource the agent actually owns."""
    type: str           # email, platform_account, domain, api_key, payment_method, llc
    platform: str       # gmail, upwork, stripe, etc.
    identifier: str     # the actual value (email address, username, domain name)
    status: str         # active, pending, suspended
    metadata: dict = field(default_factory=dict)


@dataclass
class AssetInventory:
    """Complete inventory of what the agent actually has."""
    assets: list[Asset] = field(default_factory=list)

    @property
    def emails(self) -> list[Asset]:
        return [a for a in self.assets if a.type == "email"]

    @property
    def has_email(self) -> bool:
        return any(a.type == "email" and a.status == "active" for a in self.assets)

    @property
    def email_address(self) -> str | None:
        """Get the actual email address, or None if we don't have one."""
        for a in self.assets:
            if a.type == "email" and a.status == "active":
                return a.identifier
        return None

    @property
    def platform_accounts(self) -> list[Asset]:
        return [a for a in self.assets if a.type == "platform_account"]

    @property
    def domains(self) -> list[Asset]:
        return [a for a in self.assets if a.type == "domain"]

    @property
    def api_keys(self) -> list[Asset]:
        return [a for a in self.assets if a.type == "api_key"]

    @property
    def payment_methods(self) -> list[Asset]:
        return [a for a in self.assets if a.type == "payment"]

    def has_account(self, platform: str) -> bool:
        return any(
            a.type == "platform_account" and a.platform == platform and a.status == "active"
            for a in self.assets
        )

    def has_api_key(self, provider: str) -> bool:
        return any(
            a.type == "api_key" and a.platform == provider and a.status == "active"
            for a in self.assets
        )

    def has_domain(self) -> bool:
        return any(a.type == "domain" and a.status == "active" for a in self.assets)

    def has_payment_method(self) -> bool:
        return any(a.type == "payment" and a.status == "active" for a in self.assets)

    def summary(self) -> str:
        """Human-readable summary for LLM context."""
        parts = []
        parts.append(f"Email: {self.email_address or 'NONE — not yet created'}")

        accts = self.platform_accounts
        if accts:
            parts.append(f"Platform accounts: {', '.join(a.platform for a in accts)}")
        else:
            parts.append("Platform accounts: NONE")

        doms = self.domains
        if doms:
            parts.append(f"Domains: {', '.join(a.identifier for a in doms)}")
        else:
            parts.append("Domains: NONE")

        keys = self.api_keys
        if keys:
            parts.append(f"API keys: {', '.join(a.platform for a in keys)}")
        else:
            parts.append("API keys: NONE")

        pay = self.payment_methods
        if pay:
            parts.append(f"Payment methods: {', '.join(a.platform for a in pay)}")
        else:
            parts.append("Payment methods: NONE")

        return "\n".join(parts)

    def to_context(self) -> str:
        """Format for injection into LLM prompts.

        This is the KEY function — it tells the LLM exactly what's real
        and what's not, so it NEVER hallucinates resources.
        """
        lines = [
            "=== MY ACTUAL ASSETS (use ONLY these, never invent fake ones) ===",
            self.summary(),
            "=== END ASSETS ===",
            "",
            "CRITICAL: If you need a resource listed as NONE above, you CANNOT use it.",
            "Do NOT invent fake emails, fake accounts, or placeholder values.",
            "If you need something you don't have, call fail() explaining what's missing.",
        ]
        return "\n".join(lines)


class AssetManager:
    """Queries the database to build an accurate asset inventory."""

    def __init__(self, db: Database):
        self.db = db

    def get_inventory(self) -> AssetInventory:
        """Build complete asset inventory from the database."""
        assets: list[Asset] = []

        # 1. Identities (email, platform accounts, domains, API keys)
        try:
            rows = self.db.execute(
                "SELECT * FROM identities WHERE status = 'active' ORDER BY platform"
            )
            for row in rows:
                r = dict(row)
                meta = {}
                if r.get("metadata"):
                    try:
                        meta = json.loads(r["metadata"])
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Map identity types to asset types
                id_type = r.get("type", "")
                if id_type == "agent_identity":
                    continue  # Internal, not an external asset

                asset_type = id_type
                if id_type == "platform_account":
                    # Check if this is actually an email account
                    platform = r.get("platform", "").lower()
                    if platform in ("email", "gmail", "outlook", "protonmail",
                                     "proton", "hotmail", "yahoo"):
                        asset_type = "email"

                assets.append(Asset(
                    type=asset_type,
                    platform=r.get("platform", "unknown"),
                    identifier=r.get("identifier", ""),
                    status=r.get("status", "active"),
                    metadata=meta,
                ))
        except Exception as e:
            logger.debug(f"Could not query identities: {e}")

        # 2. Brand API keys (from api_provisioner)
        try:
            rows = self.db.execute(
                "SELECT * FROM brand_api_keys WHERE status = 'active'"
            )
            for row in rows:
                r = dict(row)
                assets.append(Asset(
                    type="api_key",
                    platform=r.get("provider", "unknown"),
                    identifier=f"{r.get('brand', '')}:{r.get('provider', '')}",
                    status="active",
                    metadata={"brand": r.get("brand", "")},
                ))
        except Exception:
            pass  # Table might not exist

        # 3. Corporate entities (LLCs)
        try:
            rows = self.db.execute(
                "SELECT * FROM corporate_entities WHERE status = 'active'"
            )
            for row in rows:
                r = dict(row)
                assets.append(Asset(
                    type="llc",
                    platform=r.get("jurisdiction", ""),
                    identifier=r.get("name", ""),
                    status=r.get("status", "active"),
                    metadata={"entity_id": r.get("id")},
                ))
        except Exception:
            pass

        return AssetInventory(assets=assets)

    def get_missing_prerequisites(self, action: str) -> list[str]:
        """Check what's missing for a given action.

        Returns a list of missing prerequisites, or empty list if all good.
        """
        inventory = self.get_inventory()
        missing = []

        action_lower = action.lower()

        # Platform registration requires email
        registration_keywords = ["register", "signup", "sign up", "create account"]
        if any(kw in action_lower for kw in registration_keywords):
            if not inventory.has_email:
                missing.append("email (required for platform registration)")

        # Domain registration requires payment method
        if "domain" in action_lower and ("register" in action_lower or "buy" in action_lower):
            if not inventory.has_payment_method():
                missing.append("payment method (required for domain purchase)")

        # API key acquisition often requires account on that platform
        if "api" in action_lower and "key" in action_lower:
            # Try to extract platform name
            for platform in ["stripe", "gumroad", "lemonsqueezy"]:
                if platform in action_lower and not inventory.has_account(platform):
                    missing.append(f"{platform} account (required before API key)")

        return missing
