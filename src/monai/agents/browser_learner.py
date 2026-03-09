"""Browser Learner — adaptive browser automation that learns from failures.

Wraps the existing Browser class and adds:
- Action logging with outcomes
- Failure categorization (CAPTCHA, bot_detection, dom_change, timeout)
- Success rate tracking per site and action type
- Self-healing selectors (fallback strategies)
- Site playbooks (learned interaction patterns per domain)
- CAPTCHA solver integration
- Countermeasure generation
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any
from urllib.parse import urlparse

from monai.config import Config
from monai.db.database import Database
from monai.utils.browser import Browser
from monai.utils.llm import LLM

logger = logging.getLogger(__name__)

BROWSER_LEARNER_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    action_type TEXT NOT NULL,          -- navigate, click, type, fill_form, submit, screenshot
    selector TEXT,                       -- CSS selector used (if applicable)
    url TEXT,
    success INTEGER NOT NULL,           -- 1 = success, 0 = failure
    failure_type TEXT,                  -- captcha, bot_detection, dom_change, timeout, auth_required, unknown
    error_message TEXT,
    duration_ms INTEGER,
    countermeasure_used TEXT,           -- what strategy was used
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS site_playbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT UNIQUE NOT NULL,
    login_flow TEXT,                    -- JSON: steps to log in
    navigation_patterns TEXT,          -- JSON: common navigation patterns
    known_selectors TEXT,              -- JSON: reliable selectors for key elements
    anti_bot_measures TEXT,            -- JSON: what anti-bot tech they use
    success_rate REAL DEFAULT 0.0,
    total_attempts INTEGER DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class BrowserLearner:
    """Adaptive browser automation that learns from every interaction."""

    def __init__(self, config: Config, db: Database, llm: LLM,
                 headless: bool = True):
        self.config = config
        self.db = db
        self.llm = llm
        self.browser = Browser(config, headless=headless)
        self._captcha_solver = None  # Lazy-loaded

        with db.connect() as conn:
            conn.executescript(BROWSER_LEARNER_SCHEMA)

    async def start(self):
        await self.browser.start()

    async def stop(self):
        await self.browser.stop()

    # ── Smart Actions (with learning) ─────────────────────────────

    async def navigate(self, url: str, **kwargs: Any) -> dict[str, Any]:
        """Navigate to URL with failure detection and retry."""
        domain = urlparse(url).netloc
        start = time.time()

        try:
            # Add human-like delay
            await self._human_delay()

            content = await self.browser.navigate(url, **kwargs)
            page_info = await self.browser.get_page_info()
            duration = int((time.time() - start) * 1000)

            # Check for blocks
            failure_type = self._detect_failure(page_info)
            if failure_type:
                self._log_action(domain, "navigate", None, url, False,
                                 failure_type, duration=duration)
                # Try countermeasure
                result = await self._apply_countermeasure(failure_type, domain, url)
                return {"success": not failure_type, "failure": failure_type,
                        "countermeasure_result": result, "page_info": page_info}

            self._log_action(domain, "navigate", None, url, True, duration=duration)
            return {"success": True, "page_info": page_info}

        except Exception as e:
            duration = int((time.time() - start) * 1000)
            failure_type = self._classify_error(e)
            self._log_action(domain, "navigate", None, url, False,
                             failure_type, str(e), duration)
            return {"success": False, "failure": failure_type, "error": str(e)}

    async def smart_click(self, selector: str, domain: str = "",
                          fallback_text: str = "") -> dict[str, Any]:
        """Click with self-healing selectors."""
        start = time.time()

        try:
            await self._human_delay(short=True)
            await self.browser.click(selector)
            duration = int((time.time() - start) * 1000)
            self._log_action(domain, "click", selector, None, True, duration=duration)
            return {"success": True, "selector_used": selector}

        except Exception as e:
            # Primary selector failed — try fallbacks
            if fallback_text:
                fallback_selectors = self._generate_fallback_selectors(fallback_text)
                for fallback in fallback_selectors:
                    try:
                        await self.browser.click(fallback)
                        duration = int((time.time() - start) * 1000)
                        self._log_action(domain, "click", fallback, None, True,
                                         countermeasure="fallback_selector", duration=duration)
                        # Learn the working selector
                        self._update_playbook_selector(domain, selector, fallback)
                        return {"success": True, "selector_used": fallback,
                                "original_failed": selector}
                    except Exception:
                        continue

            duration = int((time.time() - start) * 1000)
            self._log_action(domain, "click", selector, None, False,
                             "dom_change", str(e), duration)
            return {"success": False, "error": str(e), "selector": selector}

    async def smart_type(self, selector: str, text: str,
                         domain: str = "", human_like: bool = True) -> dict[str, Any]:
        """Type with human-like keystroke timing."""
        start = time.time()

        try:
            if human_like:
                page = await self.browser._get_page()
                await page.click(selector)
                for char in text:
                    await page.keyboard.type(char, delay=random.randint(30, 150))
                    if random.random() < 0.05:  # 5% chance of brief pause
                        await asyncio.sleep(random.uniform(0.2, 0.8))
            else:
                await self.browser.type_text(selector, text)

            duration = int((time.time() - start) * 1000)
            self._log_action(domain, "type", selector, None, True, duration=duration)
            return {"success": True}

        except Exception as e:
            duration = int((time.time() - start) * 1000)
            self._log_action(domain, "type", selector, None, False,
                             "dom_change", str(e), duration)
            return {"success": False, "error": str(e)}

    async def smart_fill_form(self, fields: dict[str, str],
                              domain: str = "") -> dict[str, Any]:
        """Fill a form with human-like behavior."""
        results = {}
        for selector, value in fields.items():
            await self._human_delay(short=True)
            result = await self.smart_type(selector, value, domain, human_like=True)
            results[selector] = result

        return {"success": all(r.get("success") for r in results.values()),
                "fields": results}

    # ── Failure Detection ─────────────────────────────────────────

    def _detect_failure(self, page_info: dict[str, Any]) -> str | None:
        """Analyze page content to detect blocks/CAPTCHAs/bot detection."""
        text = (page_info.get("text", "") or "").lower()
        title = (page_info.get("title", "") or "").lower()

        # CAPTCHA detection
        captcha_signals = [
            "captcha", "recaptcha", "hcaptcha", "verify you're human",
            "prove you're not a robot", "challenge", "turnstile",
        ]
        if any(s in text or s in title for s in captcha_signals):
            return "captcha"

        # Bot detection
        bot_signals = [
            "blocked", "access denied", "forbidden", "rate limit",
            "suspicious activity", "automated", "bot detected",
            "cloudflare", "please wait", "checking your browser",
        ]
        if any(s in text or s in title for s in bot_signals):
            return "bot_detection"

        # Auth required
        auth_signals = ["sign in", "log in", "login required", "unauthorized"]
        if any(s in title for s in auth_signals):
            return "auth_required"

        return None

    def _classify_error(self, error: Exception) -> str:
        """Classify an exception into a failure type."""
        msg = str(error).lower()
        if "timeout" in msg:
            return "timeout"
        if "selector" in msg or "not found" in msg:
            return "dom_change"
        if "net::" in msg or "connection" in msg:
            return "network"
        return "unknown"

    # ── Countermeasures ───────────────────────────────────────────

    async def _apply_countermeasure(self, failure_type: str,
                                    domain: str, url: str) -> dict[str, Any]:
        """Apply a countermeasure based on the failure type."""
        if failure_type == "captcha":
            return await self._handle_captcha(domain)
        elif failure_type == "bot_detection":
            return await self._handle_bot_detection(domain, url)
        elif failure_type == "timeout":
            return await self._handle_timeout(url)
        elif failure_type == "auth_required":
            return {"action": "needs_login", "domain": domain}
        return {"action": "none"}

    async def _handle_captcha(self, domain: str) -> dict[str, Any]:
        """Handle CAPTCHA challenges."""
        # Log for now — CAPTCHA solving service integration point
        logger.warning(f"CAPTCHA detected on {domain}")
        return {
            "action": "captcha_detected",
            "domain": domain,
            "note": "CAPTCHA solving service needed (2captcha/anti-captcha)",
        }

    async def _handle_bot_detection(self, domain: str,
                                    url: str) -> dict[str, Any]:
        """Handle bot detection by rotating fingerprint and retrying."""
        logger.warning(f"Bot detection on {domain}, rotating fingerprint")

        # Stop and restart browser with new fingerprint
        await self.browser.stop()
        await asyncio.sleep(random.uniform(2, 5))
        await self.browser.start()

        # Retry with new fingerprint
        try:
            await self.browser.navigate(url)
            page_info = await self.browser.get_page_info()
            if not self._detect_failure(page_info):
                self._log_action(domain, "navigate", None, url, True,
                                 countermeasure="fingerprint_rotation")
                return {"action": "fingerprint_rotated", "success": True}
        except Exception:
            pass

        return {"action": "fingerprint_rotated", "success": False}

    async def _handle_timeout(self, url: str) -> dict[str, Any]:
        """Handle timeout by retrying with longer wait."""
        try:
            await self.browser.navigate(url, wait_for="networkidle")
            return {"action": "retry_with_longer_wait", "success": True}
        except Exception:
            return {"action": "retry_with_longer_wait", "success": False}

    # ── Self-Healing Selectors ────────────────────────────────────

    def _generate_fallback_selectors(self, text: str) -> list[str]:
        """Generate fallback selectors based on text content."""
        safe_text = text.replace("'", "\\'")
        return [
            f"text={text}",
            f"button:has-text('{safe_text}')",
            f"a:has-text('{safe_text}')",
            f"[aria-label='{safe_text}']",
            f"[title='{safe_text}']",
            f"[value='{safe_text}']",
            f"//*[contains(text(), '{safe_text}')]",
        ]

    def _update_playbook_selector(self, domain: str,
                                  old_selector: str, new_selector: str):
        """Update the playbook when we find a better selector."""
        if not domain:
            return
        rows = self.db.execute(
            "SELECT known_selectors FROM site_playbooks WHERE domain = ?",
            (domain,),
        )
        if rows:
            selectors = json.loads(rows[0]["known_selectors"] or "{}")
            selectors[old_selector] = new_selector
            self.db.execute(
                "UPDATE site_playbooks SET known_selectors = ?, last_updated = CURRENT_TIMESTAMP "
                "WHERE domain = ?",
                (json.dumps(selectors), domain),
            )
        else:
            self.db.execute_insert(
                "INSERT INTO site_playbooks (domain, known_selectors) VALUES (?, ?)",
                (domain, json.dumps({old_selector: new_selector})),
            )

    # ── Human-Like Behavior ───────────────────────────────────────

    async def _human_delay(self, short: bool = False):
        """Add human-like random delays between actions."""
        if short:
            await asyncio.sleep(random.uniform(0.3, 1.2))
        else:
            await asyncio.sleep(random.uniform(0.8, 3.0))

    # ── Logging & Metrics ─────────────────────────────────────────

    def _log_action(self, domain: str, action_type: str,
                    selector: str | None, url: str | None,
                    success: bool, failure_type: str | None = None,
                    error_message: str | None = None,
                    duration: int | None = None,
                    countermeasure: str | None = None):
        """Log a browser action with its outcome."""
        self.db.execute_insert(
            "INSERT INTO browser_actions (domain, action_type, selector, url, "
            "success, failure_type, error_message, duration_ms, countermeasure_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (domain, action_type, selector, url, int(success),
             failure_type, error_message, duration, countermeasure),
        )

    def get_success_rate(self, domain: str = "") -> dict[str, Any]:
        """Get success rates, optionally filtered by domain."""
        if domain:
            rows = self.db.execute(
                "SELECT action_type, "
                "SUM(success) as successes, COUNT(*) as total, "
                "ROUND(CAST(SUM(success) AS REAL) / COUNT(*) * 100, 1) as rate "
                "FROM browser_actions WHERE domain = ? GROUP BY action_type",
                (domain,),
            )
        else:
            rows = self.db.execute(
                "SELECT action_type, "
                "SUM(success) as successes, COUNT(*) as total, "
                "ROUND(CAST(SUM(success) AS REAL) / COUNT(*) * 100, 1) as rate "
                "FROM browser_actions GROUP BY action_type"
            )
        return {r["action_type"]: dict(r) for r in rows}

    def get_failure_breakdown(self, domain: str = "") -> dict[str, int]:
        """Get failure types breakdown."""
        if domain:
            rows = self.db.execute(
                "SELECT failure_type, COUNT(*) as count FROM browser_actions "
                "WHERE success = 0 AND domain = ? GROUP BY failure_type",
                (domain,),
            )
        else:
            rows = self.db.execute(
                "SELECT failure_type, COUNT(*) as count FROM browser_actions "
                "WHERE success = 0 GROUP BY failure_type"
            )
        return {r["failure_type"]: r["count"] for r in rows}

    def get_playbook(self, domain: str) -> dict[str, Any] | None:
        """Get the learned playbook for a domain."""
        rows = self.db.execute(
            "SELECT * FROM site_playbooks WHERE domain = ?", (domain,)
        )
        return dict(rows[0]) if rows else None
