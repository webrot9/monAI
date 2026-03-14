"""Email Verifier — retrieves verification codes from email accounts.

Completes the autonomous account creation pipeline:
1. Provisioner creates account with email
2. Platform sends verification email
3. EmailVerifier polls IMAP inbox for the code
4. Code returned to provisioner to complete signup

Supports:
- IMAP polling with configurable intervals
- Regex-based code extraction (4-8 digit, UUID links)
- Verification link extraction (click-to-verify)
- Multi-account inbox monitoring
- Temp email services as fallback (mail.tm, guerrillamail)
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import time
from email.header import decode_header
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)

EMAIL_VERIFIER_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_address TEXT NOT NULL,
    platform TEXT NOT NULL,
    verification_type TEXT,       -- code, link, button
    verification_value TEXT,      -- the code or URL extracted
    status TEXT DEFAULT 'pending', -- pending, found, used, expired
    imap_host TEXT,
    attempts INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    found_at TIMESTAMP
);
"""

# Patterns for extracting verification codes from emails
# Ordered from most specific to least specific — first match wins
CODE_PATTERNS = [
    r'(?:verification|confirm|verify|code|pin|otp)[\s:]*(\d{4,8})',
    r'(?:your code is|enter code|use code)[\s:]*(\d{4,8})',
    r'(?:^|[\s>:])(\d{6})(?:[\s<.]|$)',  # Standalone 6-digit code (not embedded in longer number)
]

# Patterns for extracting verification links
LINK_PATTERNS = [
    r'(https?://[^\s<>"]+(?:verify|confirm|activate|validate)[^\s<>"]*)',
    r'(https?://[^\s<>"]+(?:token|code|key)=[^\s<>"]+)',
]


class EmailVerifier:
    """Polls email inboxes for verification codes and links."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._anonymizer = get_anonymizer(config)
        self.__http = None

        with db.connect() as conn:
            conn.executescript(EMAIL_VERIFIER_SCHEMA)

    @property
    def _http(self):
        if self.__http is None:
            self.__http = self._anonymizer.create_http_client(timeout=30)
        return self.__http

    # ── Public API ──────────────────────────────────────────────

    def wait_for_verification(self, email_address: str, platform: str,
                              imap_host: str = "", imap_port: int = 993,
                              imap_user: str = "", imap_password: str = "",
                              timeout: int = 180,
                              poll_interval: int = 10) -> dict[str, Any]:
        """Wait for a verification email and extract the code/link.

        Args:
            email_address: The email to check
            platform: Which platform sent the verification
            imap_host: IMAP server (auto-detected if empty)
            imap_port: IMAP port (default 993 for SSL)
            imap_user: IMAP username (defaults to email_address)
            imap_password: IMAP password
            timeout: Max seconds to wait
            poll_interval: Seconds between inbox checks

        Returns:
            Dict with verification_type (code/link) and verification_value
        """
        # Record the request
        req_id = self.db.execute_insert(
            "INSERT INTO email_verifications "
            "(email_address, platform, imap_host) VALUES (?, ?, ?)",
            (email_address, platform, imap_host),
        )

        # Auto-detect IMAP settings if not provided
        if not imap_host:
            imap_host, imap_port = self._detect_imap(email_address)

        if not imap_user:
            imap_user = email_address

        # If no IMAP credentials, try temp email API
        if not imap_password:
            return self._try_temp_email(email_address, platform, req_id, timeout)

        # Poll IMAP
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            result = self._check_imap(
                imap_host, imap_port, imap_user, imap_password,
                platform, email_address,
            )
            if result:
                self.db.execute(
                    "UPDATE email_verifications SET status = 'found', "
                    "verification_type = ?, verification_value = ?, "
                    "attempts = ?, found_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (result["type"], result["value"], attempt, req_id),
                )
                return {
                    "status": "found",
                    "verification_type": result["type"],
                    "verification_value": result["value"],
                    "attempts": attempt,
                }
            time.sleep(poll_interval)

        self.db.execute(
            "UPDATE email_verifications SET status = 'expired', attempts = ? WHERE id = ?",
            (attempt, req_id),
        )
        return {"status": "timeout", "attempts": attempt}

    # ── IMAP Integration ────────────────────────────────────────

    def _check_imap(self, host: str, port: int, user: str, password: str,
                    platform: str, target_email: str) -> dict[str, str] | None:
        """Connect to IMAP and search for verification emails.

        Routes connection through SOCKS5 proxy to prevent real IP leaks.
        """
        conn = None
        try:
            # Route IMAP through SOCKS5 proxy to prevent IP leak
            proxy_url = self._anonymizer.get_proxy_url()
            if proxy_url:
                import socks
                import socket
                from urllib.parse import urlparse as _urlparse
                parsed = _urlparse(proxy_url)
                proxy_type = socks.SOCKS5 if "socks5" in parsed.scheme else socks.SOCKS4
                socks.set_default_proxy(
                    proxy_type, parsed.hostname, parsed.port or 1080,
                )
                orig_socket = socket.socket
                socket.socket = socks.socksocket
                try:
                    conn = imaplib.IMAP4_SSL(host, port)
                finally:
                    socket.socket = orig_socket
            else:
                logger.warning(
                    "PRIVACY: No proxy available for IMAP — skipping direct connection "
                    "to prevent IP leak"
                )
                return None
            conn.login(user, password)
            conn.select("INBOX")

            # Search for recent emails from the platform
            platform_domains = self._get_platform_domains(platform)
            for domain in platform_domains:
                _, msg_ids = conn.search(None, f'(FROM "{domain}" UNSEEN)')
                if not msg_ids[0]:
                    continue

                # Check most recent first
                for msg_id in reversed(msg_ids[0].split()):
                    _, msg_data = conn.fetch(msg_id, "(RFC822)")
                    if not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    if isinstance(raw, bytes):
                        msg = email.message_from_bytes(raw)
                    else:
                        msg = email.message_from_string(raw)

                    body = self._get_email_body(msg)
                    subject = self._decode_subject(msg.get("Subject", ""))
                    full_text = f"{subject}\n{body}"

                    result = self._extract_verification(full_text)
                    if result:
                        # Mark as read
                        conn.store(msg_id, "+FLAGS", "\\Seen")
                        return result

            return None
        except Exception as e:
            logger.warning(f"IMAP check failed ({host}): {e}")
            return None
        finally:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass

    def _get_email_body(self, msg: email.message.Message) -> str:
        """Extract text body from email message, stripping HTML tags."""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode("utf-8", errors="ignore")
                elif content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = payload.decode("utf-8", errors="ignore")
                        return re.sub(r'<[^>]+>', ' ', html)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode("utf-8", errors="ignore")
                # Strip HTML tags if content is HTML
                if msg.get_content_type() == "text/html":
                    return re.sub(r'<[^>]+>', ' ', text)
                return text
        return ""

    def _decode_subject(self, raw_subject: str) -> str:
        """Decode email subject header."""
        decoded_parts = decode_header(raw_subject)
        parts = []
        for data, charset in decoded_parts:
            if isinstance(data, bytes):
                parts.append(data.decode(charset or "utf-8", errors="ignore"))
            else:
                parts.append(data)
        return " ".join(parts)

    def _extract_verification(self, text: str) -> dict[str, str] | None:
        """Extract verification code or link from email text."""
        text_lower = text.lower()

        # Skip if not a verification email
        verify_signals = [
            "verify", "confirm", "activate", "code", "otp",
            "validation", "registration", "welcome",
        ]
        if not any(s in text_lower for s in verify_signals):
            return None

        # Try code extraction first
        for pattern in CODE_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return {"type": "code", "value": match.group(1)}

        # Try link extraction
        for pattern in LINK_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return {"type": "link", "value": match.group(1)}

        return None

    def _detect_imap(self, email_address: str) -> tuple[str, int]:
        """Auto-detect IMAP settings from email domain."""
        domain = email_address.split("@")[-1].lower()
        imap_map = {
            "gmail.com": ("imap.gmail.com", 993),
            "outlook.com": ("outlook.office365.com", 993),
            "hotmail.com": ("outlook.office365.com", 993),
            "yahoo.com": ("imap.mail.yahoo.com", 993),
            "protonmail.com": ("127.0.0.1", 1143),  # ProtonMail Bridge
            "proton.me": ("127.0.0.1", 1143),
            "icloud.com": ("imap.mail.me.com", 993),
        }
        return imap_map.get(domain, (f"imap.{domain}", 993))

    def _get_platform_domains(self, platform: str) -> list[str]:
        """Get email domains associated with a platform."""
        domain_map = {
            "upwork": ["upwork.com"],
            "fiverr": ["fiverr.com"],
            "twitter": ["twitter.com", "x.com"],
            "google": ["google.com", "accounts.google.com"],
            "facebook": ["facebook.com", "facebookmail.com"],
            "instagram": ["instagram.com", "mail.instagram.com"],
            "linkedin": ["linkedin.com"],
            "github": ["github.com"],
            "stripe": ["stripe.com"],
            "gumroad": ["gumroad.com"],
            "substack": ["substack.com"],
        }
        domains = domain_map.get(platform.lower(), [f"{platform.lower()}.com"])
        # Also search broadly by platform name
        domains.append(platform.lower())
        return domains

    # ── Temp Email Fallback ─────────────────────────────────────

    def _try_temp_email(self, email_address: str, platform: str,
                        req_id: int, timeout: int) -> dict[str, Any]:
        """Use mail.tm API as temp email provider if no IMAP creds."""
        # Check if this is a mail.tm address
        domain = email_address.split("@")[-1].lower()

        try:
            # Try mail.tm API — get messages for the address
            # First authenticate
            auth_resp = self._http.post(
                "https://api.mail.tm/token",
                json={"address": email_address, "password": email_address},
                timeout=15,
            )
            if auth_resp.status_code != 200:
                return {"status": "error",
                        "error": "No IMAP credentials and temp email auth failed"}

            token = auth_resp.json().get("token", "")
            headers = {"Authorization": f"Bearer {token}"}

            deadline = time.time() + timeout
            attempt = 0
            while time.time() < deadline:
                attempt += 1
                msgs_resp = self._http.get(
                    "https://api.mail.tm/messages",
                    headers=headers,
                    timeout=15,
                )
                if msgs_resp.status_code == 200:
                    messages = msgs_resp.json().get("hydra:member", [])
                    for msg in messages:
                        # Get full message
                        msg_resp = self._http.get(
                            f"https://api.mail.tm/messages/{msg['id']}",
                            headers=headers,
                            timeout=15,
                        )
                        if msg_resp.status_code == 200:
                            msg_data = msg_resp.json()
                            full_text = (
                                f"{msg_data.get('subject', '')}\n"
                                f"{msg_data.get('text', '')}\n"
                                f"{msg_data.get('html', [''])[0] if msg_data.get('html') else ''}"
                            )
                            result = self._extract_verification(full_text)
                            if result:
                                self.db.execute(
                                    "UPDATE email_verifications SET status = 'found', "
                                    "verification_type = ?, verification_value = ?, "
                                    "attempts = ?, found_at = CURRENT_TIMESTAMP "
                                    "WHERE id = ?",
                                    (result["type"], result["value"], attempt, req_id),
                                )
                                return {
                                    "status": "found",
                                    "verification_type": result["type"],
                                    "verification_value": result["value"],
                                    "attempts": attempt,
                                }
                time.sleep(10)

            return {"status": "timeout", "attempts": attempt}

        except Exception as e:
            logger.warning(f"Temp email check failed: {e}")
            return {"status": "error", "error": str(e)}

    def create_temp_email(self) -> dict[str, Any]:
        """Create a temporary email address via mail.tm for signups."""
        try:
            # Get available domains
            domains_resp = self._http.get(
                "https://api.mail.tm/domains", timeout=15)
            domains_resp.raise_for_status()
            domains = domains_resp.json().get("hydra:member", [])
            if not domains:
                return {"status": "error", "error": "No temp email domains available"}

            domain = domains[0]["domain"]
            import secrets
            username = f"monai.{secrets.token_hex(6)}"
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(16)

            # Register
            reg_resp = self._http.post(
                "https://api.mail.tm/accounts",
                json={"address": address, "password": password},
                timeout=15,
            )
            reg_resp.raise_for_status()

            return {
                "status": "created",
                "address": address,
                "password": password,
                "domain": domain,
            }

        except Exception as e:
            logger.error(f"Temp email creation failed: {e}")
            return {"status": "error", "error": str(e)}

    # ── Mailslurp API ─────────────────────────────────────────────

    _MAILSLURP_BASE = "https://api.mailslurp.com"

    def _mailslurp_key(self) -> str:
        return self.config.comms.mailslurp_api_key

    def _mailslurp_headers(self) -> dict[str, str]:
        return {"x-api-key": self._mailslurp_key()}

    def create_mailslurp_inbox(self, name: str = "") -> dict[str, Any]:
        """Create a persistent email inbox via Mailslurp API.

        Returns:
            {"status": "created", "inbox_id": str, "address": str} or error.
        """
        key = self._mailslurp_key()
        if not key:
            return {"status": "error",
                    "error": "MAILSLURP_API_KEY not configured"}
        try:
            params: dict[str, Any] = {"inboxType": "HTTP_INBOX"}
            if name:
                params["name"] = name
            resp = self._http.post(
                f"{self._MAILSLURP_BASE}/inboxes",
                headers=self._mailslurp_headers(),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            inbox_id = data["id"]
            address = data["emailAddress"]
            logger.info(f"Mailslurp inbox created: {address} ({inbox_id})")
            return {
                "status": "created",
                "inbox_id": inbox_id,
                "address": address,
            }
        except Exception as e:
            logger.error(f"Mailslurp inbox creation failed: {e}")
            return {"status": "error", "error": str(e)}

    def mailslurp_wait_for_email(self, inbox_id: str, timeout_ms: int = 60_000,
                                  subject_filter: str = "") -> dict[str, Any]:
        """Wait for an email to arrive in a Mailslurp inbox.

        Returns:
            {"status": "received", "subject": str, "body": str, "from": str,
             "verification_code": str | None, "verification_link": str | None}
        """
        key = self._mailslurp_key()
        if not key:
            return {"status": "error",
                    "error": "MAILSLURP_API_KEY not configured"}
        try:
            params: dict[str, Any] = {
                "inboxId": inbox_id,
                "timeout": timeout_ms,
                "unreadOnly": True,
            }
            resp = self._http.get(
                f"{self._MAILSLURP_BASE}/waitForLatestEmail",
                headers=self._mailslurp_headers(),
                params=params,
                timeout=max(timeout_ms // 1000 + 10, 30),
            )
            resp.raise_for_status()
            data = resp.json()

            subject = data.get("subject", "")
            body = data.get("body", "")
            sender = (data.get("from") or "")

            # Extract verification code / link
            verification = self._extract_verification(
                subject + "\n" + body)

            return {
                "status": "received",
                "subject": subject,
                "body": body[:2000],
                "from": sender,
                "verification_code": (
                    verification["value"]
                    if verification and verification["type"] == "code"
                    else None
                ),
                "verification_link": (
                    verification["value"]
                    if verification and verification["type"] == "link"
                    else None
                ),
            }
        except Exception as e:
            logger.error(f"Mailslurp wait_for_email failed: {e}")
            return {"status": "error", "error": str(e)}
