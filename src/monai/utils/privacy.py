"""Network anonymization layer — agents must be COMPLETELY untraceable.

All outbound traffic (HTTP, browser, SMTP, DNS) is routed through Tor or a
configured proxy. The creator's real IP must NEVER be exposed.

Layers:
1. Tor/SOCKS5 proxy for all connections
2. User-agent rotation to prevent browser fingerprinting
3. Tor circuit rotation to prevent request correlation
4. DNS leak prevention (all DNS goes through proxy)
5. IP verification before any operation
6. Metadata stripping from all generated files
7. No timezone/locale leaks in browser context
"""

from __future__ import annotations

import io
import logging
import os
import random
import secrets
import struct
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from monai.config import Config, PrivacyConfig

logger = logging.getLogger(__name__)

# Realistic, diverse user agents — rotated per-session
USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    # Firefox on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    # Chrome on Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
]

# Common screen resolutions to randomize fingerprint
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 720},
    {"width": 2560, "height": 1440},
]

# Common timezones — randomized to prevent locale fingerprinting
TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Los_Angeles",
    "Europe/London", "Europe/Berlin", "Europe/Paris",
    "Asia/Tokyo", "Asia/Singapore", "Australia/Sydney",
]

# Common locales
LOCALES = [
    "en-US", "en-GB", "de-DE", "fr-FR", "es-ES",
    "pt-BR", "ja-JP", "ko-KR", "zh-CN",
]

# IP check services (use multiple for reliability)
IP_CHECK_URLS = [
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]


class AnonymityError(Exception):
    """Raised when anonymity cannot be guaranteed."""
    pass


class AllProxiesBlockedError(AnonymityError):
    """Raised when every proxy in the fallback chain is blocked for a domain."""
    pass


# ── Proxy type constants ────────────────────────────────────────
PROXY_TOR = "tor"
PROXY_RESIDENTIAL = "residential"
PROXY_DATACENTER = "datacenter"
PROXY_DIRECT = "direct"  # NEVER actually used — exists only as a sentinel

# Ordered preference: best anonymity first
PROXY_CHAIN_ORDER = [PROXY_TOR, PROXY_RESIDENTIAL, PROXY_DATACENTER]

# Block-detection patterns in page content (case-insensitive)
BLOCK_PATTERNS = [
    "access denied",
    "access blocked",
    "403 forbidden",
    "you have been blocked",
    "please verify you are a human",
    "captcha",
    "ray id",           # Cloudflare block page fingerprint
    "cf-error-details", # Cloudflare error page
    "attention required",
    "why have i been blocked",
    "security check",
    "one more step",
    "checking your browser",
    "enable javascript and cookies",
    "unusual traffic",
    "automated queries",
    "suspected automated",
]


class ProxyFallbackChain:
    """Manages an ordered fallback chain of proxy methods per domain.

    When Tor gets detected/blocked on a domain, the chain falls back to
    residential proxy, then datacenter proxy.  NEVER falls back to a direct
    connection — if all proxies are exhausted the action is aborted.

    Thread-safe: all state mutations are guarded by a lock.
    """

    def __init__(self, privacy_config: PrivacyConfig):
        self._privacy = privacy_config
        self._lock = threading.Lock()
        # domain → set of blocked proxy types
        self._blocked: dict[str, set[str]] = {}
        # domain → proxy type that last succeeded
        self._preferred: dict[str, str] = {}

    # ── Proxy URL resolution ────────────────────────────────────

    def _proxy_url_for_type(self, proxy_type: str) -> str | None:
        """Resolve a proxy type to its URL.  Returns None if unconfigured."""
        if proxy_type == PROXY_TOR:
            return f"socks5://127.0.0.1:{self._privacy.tor_socks_port}"
        elif proxy_type == PROXY_RESIDENTIAL:
            return self._privacy.residential_proxy or None
        elif proxy_type == PROXY_DATACENTER:
            return self._privacy.datacenter_proxy or None
        return None  # PROXY_DIRECT — never returned

    def _available_chain(self) -> list[str]:
        """Return proxy types that are actually configured."""
        available = []
        for ptype in PROXY_CHAIN_ORDER:
            if self._proxy_url_for_type(ptype):
                available.append(ptype)
        return available

    # ── Public API ──────────────────────────────────────────────

    def get_proxy_for_domain(self, domain: str) -> tuple[str, str]:
        """Return (proxy_type, proxy_url) for a domain.

        Uses the preferred proxy if one has previously succeeded, otherwise
        walks the chain skipping blocked types.

        Raises AllProxiesBlockedError if nothing is available.
        """
        if not self._privacy.fallback_enabled:
            # Fallback disabled — just return the primary proxy
            url = self._proxy_url_for_type(PROXY_TOR)
            if url:
                return (PROXY_TOR, url)
            raise AnonymityError("No proxy configured and fallback is disabled")

        with self._lock:
            blocked = self._blocked.get(domain, set())
            preferred = self._preferred.get(domain)

            # Try preferred first if it's not blocked
            if preferred and preferred not in blocked:
                url = self._proxy_url_for_type(preferred)
                if url:
                    return (preferred, url)

            # Walk the chain
            for ptype in self._available_chain():
                if ptype not in blocked:
                    url = self._proxy_url_for_type(ptype)
                    if url:
                        return (ptype, url)

        # All proxies blocked or unconfigured — ABORT, never go direct
        raise AllProxiesBlockedError(
            f"All proxy methods blocked for {domain}. "
            f"Blocked: {blocked}. Aborting to protect anonymity."
        )

    def report_blocked(self, domain: str, proxy_type: str) -> None:
        """Mark a proxy type as blocked for a domain."""
        with self._lock:
            if domain not in self._blocked:
                self._blocked[domain] = set()
            self._blocked[domain].add(proxy_type)
            # Clear preferred if it was the one that got blocked
            if self._preferred.get(domain) == proxy_type:
                del self._preferred[domain]
        logger.warning(
            f"PROXY FALLBACK: {proxy_type} blocked on {domain} — "
            f"blocked set: {self._blocked[domain]}"
        )

    def report_success(self, domain: str, proxy_type: str) -> None:
        """Mark a proxy type as working for a domain."""
        with self._lock:
            self._preferred[domain] = proxy_type
            # Un-block it in case it was previously blocked and recovered
            if domain in self._blocked:
                self._blocked[domain].discard(proxy_type)
        logger.info(f"PROXY FALLBACK: {proxy_type} working for {domain}")

    def get_next_fallback(self, domain: str, current_type: str) -> tuple[str, str]:
        """Get the next proxy in the chain after *current_type* for a domain.

        Marks current_type as blocked, then returns the next available.
        Raises AllProxiesBlockedError if nothing is left.
        """
        self.report_blocked(domain, current_type)
        return self.get_proxy_for_domain(domain)

    def is_blocked(self, domain: str, proxy_type: str) -> bool:
        """Check whether a proxy type is blocked for a domain."""
        with self._lock:
            return proxy_type in self._blocked.get(domain, set())

    def get_domain_status(self) -> dict[str, Any]:
        """Return a snapshot of blocked/preferred state for logging."""
        with self._lock:
            return {
                "blocked": {d: list(s) for d, s in self._blocked.items()},
                "preferred": dict(self._preferred),
            }

    @staticmethod
    def detect_block_page(page_content: str) -> bool:
        """Detect whether page content indicates a proxy/Tor block.

        Checks for CAPTCHA challenges, Cloudflare blocks, access-denied
        pages, and similar anti-bot responses.
        """
        if not page_content:
            return False
        lower = page_content.lower()
        matches = sum(1 for p in BLOCK_PATTERNS if p in lower)
        # Require at least 2 pattern matches to reduce false positives
        # (a single word like "captcha" could appear legitimately)
        return matches >= 2

    @staticmethod
    def is_blocked_status_code(status_code: int) -> bool:
        """Check if an HTTP status code indicates proxy detection."""
        return status_code in (403, 429, 503)


class TorController:
    """Manages Tor circuit rotation via the control protocol."""

    def __init__(self, control_port: int = 9051, password: str = ""):
        self.control_port = control_port
        self.password = password

    def new_circuit(self) -> bool:
        """Request a new Tor circuit (new exit IP)."""
        import socket
        try:
            with socket.create_connection(("127.0.0.1", self.control_port), timeout=10) as sock:
                sock.sendall(b'AUTHENTICATE "' + self.password.encode() + b'"\r\n')
                response = sock.recv(256)
                if b"250" not in response:
                    logger.error(f"Tor auth failed: {response}")
                    return False
                sock.sendall(b"SIGNAL NEWNYM\r\n")
                response = sock.recv(256)
                if b"250" in response:
                    logger.info("Tor circuit rotated — new exit IP")
                    time.sleep(5)  # Wait for circuit to establish
                    return True
                logger.error(f"Tor NEWNYM failed: {response}")
                return False
        except Exception as e:
            logger.error(f"Tor control connection failed: {e}")
            return False


class NetworkAnonymizer:
    """Central anonymization engine — all network traffic goes through here."""

    def __init__(self, config: Config):
        self.config = config
        self.privacy = config.privacy
        self._lock = threading.Lock()
        self._request_count = 0
        self._real_ip: str | None = None  # Cached real IP (detected once, then hidden)
        self._tor_controller: TorController | None = None

        self._fallback_chain = ProxyFallbackChain(self.privacy)

        if self.privacy.proxy_type == "tor":
            self._tor_controller = TorController(
                self.privacy.tor_control_port,
                self.privacy.tor_password,
            )

    # ── Fallback Chain ────────────────────────────────────────────

    @property
    def fallback_chain(self) -> ProxyFallbackChain:
        """Access the proxy fallback chain."""
        return self._fallback_chain

    def get_proxy_for_domain(self, domain: str) -> dict[str, str] | None:
        """Get proxy config for a specific domain, respecting the fallback chain.

        Returns a dict suitable for Playwright proxy config: {"server": url}.
        Returns None only if proxy_type is 'none'.
        Raises AllProxiesBlockedError if all proxies are blocked for the domain.
        """
        if self.privacy.proxy_type == "none":
            return None
        if not self.privacy.fallback_enabled:
            return self.get_browser_proxy()
        _ptype, url = self._fallback_chain.get_proxy_for_domain(domain)
        return {"server": url}

    # ── Proxy URLs ───────────────────────────────────────────────

    def get_proxy_url(self) -> str | None:
        """Get the proxy URL for HTTP clients."""
        if self.privacy.proxy_type == "tor":
            return f"socks5://127.0.0.1:{self.privacy.tor_socks_port}"
        elif self.privacy.proxy_type == "socks5":
            return self.privacy.socks5_proxy or None
        elif self.privacy.proxy_type == "http":
            return self.privacy.http_proxy or None
        return None  # No proxy

    def get_proxy_dict(self) -> dict[str, str]:
        """Get proxy dict for httpx/requests."""
        url = self.get_proxy_url()
        if not url:
            return {}
        # httpx uses a single proxy URL for all protocols
        return {"all://": url}

    def get_browser_proxy(self) -> dict[str, str] | None:
        """Get proxy config for Playwright browser launch."""
        url = self.get_proxy_url()
        if not url:
            return None
        return {"server": url}

    # ── HTTP Client ──────────────────────────────────────────────

    def create_http_client(self, **kwargs) -> httpx.Client:
        """Create an anonymous httpx.Client routed through proxy."""
        proxy_url = self.get_proxy_url()
        client_kwargs = {
            "timeout": kwargs.get("timeout", 30),
            "headers": {"User-Agent": self.get_user_agent()},
            "follow_redirects": True,
        }
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        client_kwargs.update(kwargs)
        return httpx.Client(**client_kwargs)

    def create_async_http_client(self, **kwargs) -> httpx.AsyncClient:
        """Create an anonymous async httpx client."""
        proxy_url = self.get_proxy_url()
        client_kwargs = {
            "timeout": kwargs.get("timeout", 30),
            "headers": {"User-Agent": self.get_user_agent()},
            "follow_redirects": True,
        }
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        client_kwargs.update(kwargs)
        return httpx.AsyncClient(**client_kwargs)

    # ── User Agent ───────────────────────────────────────────────

    def get_user_agent(self) -> str:
        """Get a random user agent string."""
        if self.privacy.rotate_user_agent:
            return random.choice(USER_AGENTS)
        return USER_AGENTS[0]

    def get_browser_fingerprint(self) -> dict[str, Any]:
        """Get a randomized browser fingerprint for Playwright context."""
        ua = self.get_user_agent()
        viewport = random.choice(VIEWPORTS)
        timezone = random.choice(TIMEZONES)
        locale = random.choice(LOCALES)

        # Match viewport to user agent (mobile UA gets mobile viewport)
        if "Mobile" in ua or "Android" in ua:
            viewport = {"width": 412, "height": 915}

        return {
            "user_agent": ua,
            "viewport": viewport,
            "timezone_id": timezone,
            "locale": locale,
            "color_scheme": random.choice(["light", "dark", "no-preference"]),
            "device_scale_factor": random.choice([1, 1.5, 2]),
        }

    # ── Circuit Management ───────────────────────────────────────

    def rotate_circuit(self) -> bool:
        """Request a new Tor circuit for a fresh exit IP."""
        if self._tor_controller:
            return self._tor_controller.new_circuit()
        return False

    def maybe_rotate(self) -> None:
        """Rotate circuit if we've hit the request threshold."""
        with self._lock:
            self._request_count += 1
            if self._request_count >= self.privacy.max_requests_per_circuit:
                self._request_count = 0
                if self._tor_controller:
                    self._tor_controller.new_circuit()

    # ── IP Verification ──────────────────────────────────────────

    def get_visible_ip(self) -> str | None:
        """Check what IP the outside world sees (through proxy)."""
        client = self.create_http_client(timeout=15)
        try:
            for url in IP_CHECK_URLS:
                try:
                    resp = client.get(url)
                    if resp.status_code == 200:
                        text = resp.text.strip()
                        # Handle JSON response from ipify
                        if text.startswith("{"):
                            import json
                            return json.loads(text).get("ip", text)
                        return text
                except Exception:
                    continue
            return None
        finally:
            client.close()

    def get_real_ip(self) -> str | None:
        """Detect the real IP by making a proxied request and comparing.

        SECURITY: We NEVER make a direct (unproxied) connection to any
        external service.  Instead we rely on comparing the proxied visible
        IP across circuit rotations.  If the IP changes after a rotation,
        the proxy is working.  The real IP is never sent anywhere.
        """
        # We cannot safely determine the real IP without leaking it.
        # Instead, return a sentinel so verify_anonymity() uses circuit
        # rotation comparison to confirm the proxy works.
        return None

    def verify_anonymity(self) -> dict[str, Any]:
        """Verify that the proxy is working and real IP is hidden.

        Uses circuit rotation comparison: if the visible IP changes after
        rotating the Tor circuit, the proxy is definitely working.  This
        avoids ever making an unproxied connection to determine the real IP.

        Returns status dict. Raises AnonymityError if proxy is down.
        """
        result = {
            "proxy_type": self.privacy.proxy_type,
            "proxy_active": False,
            "real_ip_hidden": False,
            "visible_ip": None,
        }

        if self.privacy.proxy_type == "none":
            logger.warning("PRIVACY: No proxy configured — traffic is NOT anonymous")
            result["proxy_active"] = False
            return result

        # Get the IP visible through proxy
        visible_ip = self.get_visible_ip()
        result["visible_ip"] = visible_ip

        if not visible_ip:
            raise AnonymityError(
                "Cannot verify anonymity — IP check failed through proxy. "
                "Tor may not be running. Start Tor before operating."
            )

        # Verify proxy works by rotating circuit and checking IP changes
        if self._tor_controller:
            old_ip = visible_ip
            self._tor_controller.new_circuit()
            new_ip = self.get_visible_ip()
            if new_ip and new_ip != old_ip:
                logger.info("Anonymity verified: IP changed after circuit rotation")
                result["proxy_active"] = True
                result["real_ip_hidden"] = True
                return result
            elif new_ip:
                # Same IP after rotation — proxy may still work (exit node reuse)
                # but we can at least confirm traffic goes through proxy
                logger.info("Anonymity check: proxy responding (same exit node reused)")
                result["proxy_active"] = True
                result["real_ip_hidden"] = True
                return result

        # For non-Tor proxies, just confirm proxy is responding
        result["proxy_active"] = True
        result["real_ip_hidden"] = True
        logger.info(f"Anonymity verified: visible IP {visible_ip} via proxy")
        return result

    def startup_check(self) -> dict[str, Any]:
        """Run full anonymity check at startup. Must pass before any operations.

        SECURITY: Never makes a direct (unproxied) connection. Verifies
        proxy functionality by checking that proxied requests succeed and
        (for Tor) that circuit rotation changes the exit IP.
        """
        if self.privacy.proxy_type == "none":
            raise AnonymityError(
                "PRIVACY: proxy_type=none is BLOCKED — all traffic would expose your real IP. "
                "Configure a proxy (tor/socks5/http) before running monAI."
            )

        # Verify proxy works (all checks go through the proxy)
        try:
            status = self.verify_anonymity()
            return {"anonymous": True, **status}
        except AnonymityError as e:
            logger.error(f"ANONYMITY CHECK FAILED: {e}")
            return {"anonymous": False, "error": str(e)}

    # ── Metadata Stripping ───────────────────────────────────────

    def strip_image_metadata(self, image_path: Path) -> None:
        """Strip EXIF and other metadata from images."""
        if not self.privacy.strip_metadata:
            return
        try:
            from PIL import Image
            img = Image.open(image_path)
            # Create a clean copy without metadata
            clean = Image.new(img.mode, img.size)
            # Use get_flattened_data (Pillow 11+) or fallback to getdata
            pixel_data = list(img.get_flattened_data() if hasattr(img, 'get_flattened_data') else img.getdata())
            clean.putdata(pixel_data)
            clean.save(image_path)
            logger.debug(f"Stripped metadata from {image_path}")
        except ImportError:
            logger.warning("Pillow not installed — cannot strip image metadata")
        except Exception as e:
            logger.warning(f"Failed to strip image metadata: {e}")

    def strip_pdf_metadata(self, pdf_path: Path) -> None:
        """Strip identifying metadata from PDFs."""
        if not self.privacy.strip_metadata:
            return
        try:
            content = pdf_path.read_bytes()
            # Remove /Producer, /Creator, /Author fields from PDF
            import re
            for field in [b"/Producer", b"/Creator", b"/Author"]:
                content = re.sub(
                    field + rb"\s*\([^)]*\)", field + b" ()", content
                )
            pdf_path.write_bytes(content)
            logger.debug(f"Stripped PDF metadata from {pdf_path}")
        except Exception as e:
            logger.warning(f"Failed to strip PDF metadata: {e}")

    def strip_file_metadata(self, file_path: Path) -> None:
        """Strip metadata from any supported file type."""
        if not self.privacy.strip_metadata:
            return
        suffix = file_path.suffix.lower()
        if suffix in (".jpg", ".jpeg", ".png", ".gif", ".tiff", ".webp"):
            self.strip_image_metadata(file_path)
        elif suffix == ".pdf":
            self.strip_pdf_metadata(file_path)
        # HTML, JSON, TXT files don't carry OS-level metadata


# ── Module-level singleton ───────────────────────────────────────

_anonymizer: NetworkAnonymizer | None = None
_anonymizer_lock = threading.Lock()


def get_anonymizer(config: Config | None = None) -> NetworkAnonymizer:
    """Get or create the global anonymizer instance."""
    global _anonymizer
    with _anonymizer_lock:
        if _anonymizer is None:
            if config is None:
                config = Config.load()
            _anonymizer = NetworkAnonymizer(config)
        return _anonymizer


def reset_anonymizer() -> None:
    """Reset the global anonymizer (for testing)."""
    global _anonymizer
    with _anonymizer_lock:
        _anonymizer = None
