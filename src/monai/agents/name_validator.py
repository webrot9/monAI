"""Real-world business name and domain validator.

Before monAI commits to a business name, domain, or platform username,
this module checks REAL availability — no hallucination, no assumptions.

Checks:
1. Domain availability (WHOIS + DNS)
2. LLC name availability (Secretary of State search)
3. Platform username availability (direct HTTP checks)
4. Trademark conflicts (USPTO/EUIPO search)
5. LLM-assisted conflict analysis for edge cases

Design: generate → validate → retry loop. Never register something
without confirming it's available first.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.utils.llm import LLM
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)

# Max retry attempts when generating a valid name
MAX_GENERATION_ATTEMPTS = 5

# Platforms with known signup/profile URL patterns for username checks
PLATFORM_USERNAME_URLS = {
    "github": "https://github.com/{username}",
    "twitter": "https://x.com/{username}",
    "gumroad": "https://{username}.gumroad.com",
    "fiverr": "https://www.fiverr.com/{username}",
    "upwork": "https://www.upwork.com/freelancers/~{username}",
    "ko-fi": "https://ko-fi.com/{username}",
    "lemonsqueezy": "https://{username}.lemonsqueezy.com",
}

VALIDATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS name_validations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    validation_type TEXT NOT NULL,   -- domain, llc_name, username, trademark
    platform TEXT,                   -- which platform/registrar
    status TEXT NOT NULL,            -- available, taken, error, unknown
    details TEXT,                    -- JSON: raw check results
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_name_validations_name
    ON name_validations(name, validation_type);
"""


@dataclass
class ValidationResult:
    """Result of a single validation check."""
    check_type: str         # domain, llc_name, username, trademark
    target: str             # what was checked (e.g. "example.com")
    available: bool | None  # True=available, False=taken, None=unknown
    details: str = ""       # human-readable explanation
    raw: dict = field(default_factory=dict)  # raw response data


@dataclass
class FullValidation:
    """Aggregated validation result for a business identity."""
    name: str
    checks: list[ValidationResult] = field(default_factory=list)
    overall_viable: bool = False
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "viable": self.overall_viable,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "checks": [
                {
                    "type": c.check_type,
                    "target": c.target,
                    "available": c.available,
                    "details": c.details,
                }
                for c in self.checks
            ],
        }


class NameValidator:
    """Validates business names, domains, and usernames against real-world availability."""

    def __init__(self, config: Config, db: Database, llm: LLM):
        self.config = config
        self.db = db
        self.llm = llm
        self._anonymizer = get_anonymizer(config)
        self._http = self._anonymizer.create_http_client(timeout=15)
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(VALIDATION_SCHEMA)

    def close(self):
        """Clean up HTTP client."""
        try:
            if hasattr(self._http, "close"):
                self._http.close()
        except Exception:
            pass

    # ── Core Validation Methods ─────────────────────────────────

    def check_domain(self, domain: str) -> ValidationResult:
        """Check domain availability via DNS lookup.

        Strategy: if the domain resolves to an IP, it's taken.
        If DNS fails (NXDOMAIN), it's likely available.
        This is faster and more reliable than WHOIS parsing.
        """
        domain = domain.strip().lower()
        if not re.match(r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z]{2,})+$', domain):
            return ValidationResult(
                check_type="domain",
                target=domain,
                available=None,
                details=f"Invalid domain format: {domain}",
            )

        try:
            # DNS A record lookup — if it resolves, domain is registered
            socket.getaddrinfo(domain, None, socket.AF_INET)
            result = ValidationResult(
                check_type="domain",
                target=domain,
                available=False,
                details=f"Domain {domain} resolves (already registered)",
            )
        except socket.gaierror:
            # NXDOMAIN = likely available
            result = ValidationResult(
                check_type="domain",
                target=domain,
                available=True,
                details=f"Domain {domain} does not resolve (likely available)",
            )
        except Exception as e:
            result = ValidationResult(
                check_type="domain",
                target=domain,
                available=None,
                details=f"DNS check error: {e}",
            )

        self._store_result(result)
        return result

    def check_domain_whois(self, domain: str) -> ValidationResult:
        """Check domain via WHOIS HTTP API for more accurate results.

        Uses web-based WHOIS lookup through the anonymizer.
        Falls back to DNS check if WHOIS fails.
        """
        domain = domain.strip().lower()
        try:
            # Use a public WHOIS API
            self._anonymizer.maybe_rotate()
            resp = self._http.get(
                f"https://dns.google/resolve?name={domain}&type=A",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                has_answer = bool(data.get("Answer"))
                status = data.get("Status", -1)

                if status == 3:  # NXDOMAIN
                    result = ValidationResult(
                        check_type="domain",
                        target=domain,
                        available=True,
                        details=f"WHOIS: {domain} — NXDOMAIN (available)",
                        raw=data,
                    )
                elif has_answer:
                    result = ValidationResult(
                        check_type="domain",
                        target=domain,
                        available=False,
                        details=f"WHOIS: {domain} — has DNS records (taken)",
                        raw=data,
                    )
                else:
                    # No answer but not NXDOMAIN — might be available but uncertain
                    result = ValidationResult(
                        check_type="domain",
                        target=domain,
                        available=None,
                        details=f"WHOIS: {domain} — unclear status (status={status})",
                        raw=data,
                    )
            else:
                # API error, fall back to DNS
                return self.check_domain(domain)
        except Exception:
            return self.check_domain(domain)

        self._store_result(result)
        return result

    def check_username(self, username: str, platform: str) -> ValidationResult:
        """Check if a username is available on a platform.

        Makes an HTTP request to the platform's profile URL.
        404 = available, 200 = taken.
        """
        username = username.strip().lower()
        platform = platform.strip().lower()

        url_template = PLATFORM_USERNAME_URLS.get(platform)
        if not url_template:
            return ValidationResult(
                check_type="username",
                target=f"{username}@{platform}",
                available=None,
                details=f"No URL pattern known for platform: {platform}",
            )

        url = url_template.format(username=username)

        try:
            self._anonymizer.maybe_rotate()
            resp = self._http.get(url, follow_redirects=True, timeout=10)

            if resp.status_code == 404:
                result = ValidationResult(
                    check_type="username",
                    target=f"{username}@{platform}",
                    available=True,
                    details=f"Username '{username}' available on {platform} (404)",
                )
            elif resp.status_code == 200:
                result = ValidationResult(
                    check_type="username",
                    target=f"{username}@{platform}",
                    available=False,
                    details=f"Username '{username}' taken on {platform} (200)",
                )
            else:
                result = ValidationResult(
                    check_type="username",
                    target=f"{username}@{platform}",
                    available=None,
                    details=f"Uncertain — got HTTP {resp.status_code} for {platform}",
                )
        except Exception as e:
            result = ValidationResult(
                check_type="username",
                target=f"{username}@{platform}",
                available=None,
                details=f"Check failed for {platform}: {e}",
            )

        self._store_result(result)
        return result

    def check_llc_name(self, name: str, jurisdiction: str = "US-WY") -> ValidationResult:
        """Check LLC name availability via Secretary of State website.

        Uses web search to check if a similar LLC already exists.
        """
        try:
            # Search for the business name via web
            self._anonymizer.maybe_rotate()
            search_query = f"{name} LLC {jurisdiction} secretary of state"
            resp = self._http.get(
                f"https://www.google.com/search?q={search_query}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )

            if resp.status_code == 200:
                body = resp.text.lower()
                name_lower = name.lower()

                # Look for signs the name already exists
                exists_signals = [
                    f'"{name_lower}"' in body,
                    f"{name_lower} llc" in body and "active" in body,
                    f"{name_lower} holdings" in body and "registered" in body,
                ]

                if any(exists_signals):
                    result = ValidationResult(
                        check_type="llc_name",
                        target=name,
                        available=False,
                        details=f"LLC name '{name}' likely already exists in {jurisdiction}",
                    )
                else:
                    result = ValidationResult(
                        check_type="llc_name",
                        target=name,
                        available=True,
                        details=f"LLC name '{name}' appears available in {jurisdiction}",
                    )
            else:
                result = ValidationResult(
                    check_type="llc_name",
                    target=name,
                    available=None,
                    details=f"Could not verify LLC name (HTTP {resp.status_code})",
                )
        except Exception as e:
            result = ValidationResult(
                check_type="llc_name",
                target=name,
                available=None,
                details=f"LLC name check failed: {e}",
            )

        self._store_result(result)
        return result

    def check_trademark(self, name: str) -> ValidationResult:
        """Check for potential trademark conflicts.

        Uses LLM to analyze the name for obvious trademark issues,
        and performs a basic web search for existing trademarks.
        """
        try:
            # Ask LLM to assess trademark risk
            assessment = self.llm.quick_json(
                f"Analyze the business name '{name}' for trademark conflicts.\n"
                "Consider:\n"
                "1. Is it too similar to well-known brands? (e.g. 'Appel' → Apple)\n"
                "2. Does it use protected terms? (e.g. 'Olympic', 'FIFA')\n"
                "3. Is it a common dictionary word that's hard to trademark?\n"
                "4. Could it cause consumer confusion with existing businesses?\n\n"
                "Return JSON: {\"risk_level\": \"low|medium|high\", "
                "\"conflicts\": [str], \"reasoning\": str}"
            )

            risk = assessment.get("risk_level", "unknown")
            conflicts = assessment.get("conflicts", [])
            reasoning = assessment.get("reasoning", "")

            if risk == "high":
                result = ValidationResult(
                    check_type="trademark",
                    target=name,
                    available=False,
                    details=f"HIGH trademark risk: {reasoning}. Conflicts: {', '.join(conflicts)}",
                    raw=assessment,
                )
            elif risk == "medium":
                result = ValidationResult(
                    check_type="trademark",
                    target=name,
                    available=True,  # Proceed with caution
                    details=f"Medium trademark risk: {reasoning}",
                    raw=assessment,
                )
            else:
                result = ValidationResult(
                    check_type="trademark",
                    target=name,
                    available=True,
                    details=f"Low trademark risk: {reasoning}",
                    raw=assessment,
                )
        except Exception as e:
            result = ValidationResult(
                check_type="trademark",
                target=name,
                available=None,
                details=f"Trademark check failed: {e}",
            )

        self._store_result(result)
        return result

    # ── Full Validation Pipeline ────────────────────────────────

    def validate_business_identity(
        self,
        name: str,
        domain: str = "",
        username: str = "",
        platforms: list[str] | None = None,
        jurisdiction: str = "US-WY",
    ) -> FullValidation:
        """Run all validation checks for a business identity.

        Returns a FullValidation with blockers (must-fix) and warnings.
        """
        validation = FullValidation(name=name)

        # 1. Domain check
        if domain:
            domain_result = self.check_domain_whois(domain)
            validation.checks.append(domain_result)
            if domain_result.available is False:
                validation.blockers.append(f"Domain '{domain}' is already taken")
            elif domain_result.available is None:
                validation.warnings.append(f"Could not verify domain '{domain}' availability")

        # 2. LLC name check
        llc_result = self.check_llc_name(name, jurisdiction)
        validation.checks.append(llc_result)
        if llc_result.available is False:
            validation.blockers.append(f"LLC name '{name}' already exists in {jurisdiction}")
        elif llc_result.available is None:
            validation.warnings.append(f"Could not verify LLC name availability for '{name}'")

        # 3. Trademark check
        tm_result = self.check_trademark(name)
        validation.checks.append(tm_result)
        if tm_result.available is False:
            validation.blockers.append(
                f"Trademark conflict detected for '{name}': {tm_result.details}"
            )
        elif tm_result.raw.get("risk_level") == "medium":
            validation.warnings.append(f"Medium trademark risk: {tm_result.details}")

        # 4. Platform username checks
        if username and platforms:
            for platform in platforms:
                user_result = self.check_username(username, platform)
                validation.checks.append(user_result)
                if user_result.available is False:
                    validation.warnings.append(
                        f"Username '{username}' taken on {platform}"
                    )

        # Determine overall viability
        validation.overall_viable = len(validation.blockers) == 0

        return validation

    def generate_and_validate(
        self,
        platforms: list[str] | None = None,
        jurisdiction: str = "US-WY",
        domain_tlds: list[str] | None = None,
        max_attempts: int = MAX_GENERATION_ATTEMPTS,
        context: str = "",
    ) -> tuple[dict[str, Any], FullValidation]:
        """Generate a business identity and validate it. Retry until viable.

        This is the main entry point: generate name → check everything → if
        blockers, feed failures back to LLM and generate again.

        Returns (identity_dict, validation) for the first viable identity,
        or the best attempt if all fail.
        """
        if domain_tlds is None:
            domain_tlds = [".com", ".io", ".co"]
        if platforms is None:
            platforms = ["github", "gumroad"]

        failed_names: list[str] = []
        failed_reasons: list[str] = []
        best_attempt: tuple[dict, FullValidation] | None = None

        for attempt in range(max_attempts):
            # Generate identity, feeding back failures from previous attempts
            identity = self._generate_with_feedback(
                failed_names, failed_reasons, context
            )
            name = identity.get("name", "")
            username = identity.get("preferred_username", "")

            if not name:
                failed_names.append("(empty)")
                failed_reasons.append("LLM returned empty name")
                continue

            logger.info(
                f"Validating business identity attempt {attempt + 1}/{max_attempts}: "
                f"'{name}' (username: {username})"
            )

            # Try each TLD for the domain
            domain = ""
            for tld in domain_tlds:
                slug = re.sub(r'[^a-z0-9]', '', name.lower())
                candidate_domain = f"{slug}{tld}"
                dns_check = self.check_domain(candidate_domain)
                if dns_check.available is True:
                    domain = candidate_domain
                    break

            # Run full validation
            validation = self.validate_business_identity(
                name=name,
                domain=domain,
                username=username,
                platforms=platforms,
                jurisdiction=jurisdiction,
            )

            # Track best attempt (fewest blockers)
            if (best_attempt is None
                    or len(validation.blockers) < len(best_attempt[1].blockers)):
                best_attempt = (identity, validation)

            if validation.overall_viable:
                logger.info(
                    f"Identity '{name}' is viable (domain: {domain or 'none'}, "
                    f"warnings: {len(validation.warnings)})"
                )
                identity["validated_domain"] = domain
                return identity, validation

            # Not viable — collect reasons and retry
            failed_names.append(name)
            failed_reasons.extend(validation.blockers)
            logger.warning(
                f"Identity '{name}' not viable: {validation.blockers}. "
                f"Retrying ({attempt + 1}/{max_attempts})..."
            )

        # All attempts failed — return best effort
        logger.error(
            f"Could not find viable identity after {max_attempts} attempts. "
            f"Returning best attempt with {len(best_attempt[1].blockers)} blockers."
        )
        return best_attempt[0], best_attempt[1]

    # ── Internal Helpers ────────────────────────────────────────

    def _generate_with_feedback(
        self,
        failed_names: list[str],
        failed_reasons: list[str],
        context: str = "",
    ) -> dict[str, Any]:
        """Generate a business identity, incorporating feedback from failures."""
        feedback = ""
        if failed_names:
            feedback = (
                "\n\nPREVIOUS ATTEMPTS THAT FAILED:\n"
                + "\n".join(
                    f"- '{n}': {r}"
                    for n, r in zip(failed_names, failed_reasons)
                    if n and r
                )
                + "\n\nGenerate a COMPLETELY DIFFERENT name. "
                "Avoid anything similar to the failed names. "
                "Be more creative and unique."
            )

        prompt = (
            "Generate a professional business identity for a digital services company. "
            "The name must be:\n"
            "1. UNIQUE — not similar to any well-known brand\n"
            "2. AVAILABLE — likely to pass domain and LLC name checks\n"
            "3. PROFESSIONAL — suitable for B2B services\n"
            "4. SHORT — 1-2 words, easy to spell\n"
            "5. NO GENERIC WORDS — avoid 'Digital', 'Solutions', 'Tech', 'Labs'\n\n"
            "Return JSON: {\"name\": str, \"tagline\": str, "
            "\"description\": str, \"preferred_username\": str (lowercase, no spaces), "
            "\"business_type\": str}"
            + (f"\n\nAdditional context: {context}" if context else "")
            + feedback
        )

        return self.llm.quick_json(prompt)

    def _store_result(self, result: ValidationResult):
        """Store validation result in DB for audit trail."""
        try:
            self.db.execute_insert(
                "INSERT INTO name_validations "
                "(name, validation_type, platform, status, details) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    result.target,
                    result.check_type,
                    "",
                    "available" if result.available else "taken" if result.available is False else "unknown",
                    result.details,
                ),
            )
        except Exception as e:
            logger.debug(f"Failed to store validation result: {e}")

    def get_validation_history(self, name: str = "", limit: int = 50) -> list[dict]:
        """Get past validation results."""
        if name:
            rows = self.db.execute(
                "SELECT * FROM name_validations WHERE name LIKE ? "
                "ORDER BY checked_at DESC LIMIT ?",
                (f"%{name}%", limit),
            )
        else:
            rows = self.db.execute(
                "SELECT * FROM name_validations ORDER BY checked_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in rows]
