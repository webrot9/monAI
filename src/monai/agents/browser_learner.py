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
import hashlib
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

CREATE TABLE IF NOT EXISTS form_scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    url_pattern TEXT NOT NULL,          -- URL path pattern (e.g. '/signup', '/register')
    form_signature TEXT NOT NULL,       -- hash of form field names for matching
    script TEXT NOT NULL,               -- the generated Playwright JS code
    field_mapping TEXT,                 -- JSON: which fields the script fills
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(domain, form_signature)
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
        self._captcha_solver = None  # Lazy-loaded on first CAPTCHA encounter
        self.task_context: str = ""  # Set by executor — used for ethics review

        with db.connect() as conn:
            conn.executescript(BROWSER_LEARNER_SCHEMA)

        self._seed_platform_playbooks()

    # ── Pre-seeded playbooks for common platforms ──────────────────

    # Maps domain → {original_selector: actual_selector}.
    # These are derived from real page inspections and prevent
    # costly LLM calls + 30s timeouts on first-visit registration.
    _KNOWN_PLATFORM_SELECTORS: dict[str, dict[str, str]] = {
        "app.gumroad.com": {
            # Gumroad signup is React — fields use dynamic :r0: ids
            # Only email + password on initial signup, NO name field
            "input[name='email']": 'input[type="email"]',
            "input[name='password']": 'input[type="password"]',
            "input[name='name']": "__MISSING__",
        },
        "gumroad.com": {
            "input[name='email']": 'input[type="email"]',
            "input[name='password']": 'input[type="password"]',
            "input[name='name']": "__MISSING__",
        },
        "auth.lemonsqueezy.com": {
            # LemonSqueezy uses #name, #email, #password
            # store_name does NOT exist on the registration page
            "input[name='name']": "#name",
            "input[name='email']": "#email",
            "input[name='password']": "#password",
            "input[name='store_name']": "__MISSING__",
        },
        "app.lemonsqueezy.com": {
            "input[name='name']": "#name",
            "input[name='email']": "#email",
            "input[name='password']": "#password",
            "input[name='store_name']": "__MISSING__",
        },
        "dashboard.stripe.com": {
            "input[name='email']": "#email",
            "input[name='password']": "#password",
            # Country dropdown is a custom SearchableSelect React component.
            # Route it to the custom dropdown handler, not standard fill.
            ".SearchableSelect-element[aria-label='Select country']": "__CUSTOM_DROPDOWN__",
        },
        "www.linkedin.com": {
            # LinkedIn signup — multi-step form
            "first-name": 'input[name="first-name"]',
            "last-name": 'input[name="last-name"]',
            "email-address": "#email-address",
            "password": "#password",
        },
    }

    def _seed_platform_playbooks(self) -> None:
        """Pre-seed known platform selectors into the playbook database.

        Only inserts if no playbook exists yet for that domain,
        preserving any learned selectors from previous sessions.
        """
        for domain, selectors in self._KNOWN_PLATFORM_SELECTORS.items():
            existing = self.db.execute(
                "SELECT known_selectors FROM site_playbooks WHERE domain = ?",
                (domain,),
            )
            if not existing:
                self.db.execute_insert(
                    "INSERT INTO site_playbooks (domain, known_selectors) "
                    "VALUES (?, ?)",
                    (domain, json.dumps(selectors)),
                )
                logger.debug(f"Pre-seeded playbook for {domain}")

    async def start(self):
        await self.browser.start()

    async def stop(self):
        await self.browser.stop()

    # ── Smart Actions (with learning) ─────────────────────────────

    async def navigate(self, url: str, **kwargs: Any) -> dict[str, Any]:
        """Navigate to URL with failure detection, redirect detection, and retry."""
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

            # Detect unexpected redirects (e.g. /register → /dashboard)
            redirect_warning = self._detect_redirect_mismatch(url, page_info)
            if redirect_warning:
                page_info["redirect_warning"] = redirect_warning
                logger.warning(
                    "Navigation redirect detected: requested %s but "
                    "landed on %s — %s",
                    url, page_info.get("url", "unknown"), redirect_warning,
                )

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
        """Click with self-healing selectors.

        Healing chain: known playbook → original → text fallbacks → LLM discovery.
        """
        start = time.time()

        # 1. Check if we already learned a better selector
        effective = self._get_known_selector(domain, selector) or selector

        try:
            await self._human_delay(short=True)
            await self.browser.click(effective)
            duration = int((time.time() - start) * 1000)
            self._log_action(domain, "click", effective, None, True, duration=duration)
            return {"success": True, "selector_used": effective}

        except Exception as e:
            # 2. Try text-based fallbacks
            if fallback_text:
                fallback_selectors = self._generate_fallback_selectors(fallback_text)
                for fallback in fallback_selectors:
                    try:
                        await self.browser.click(fallback)
                        duration = int((time.time() - start) * 1000)
                        self._log_action(domain, "click", fallback, None, True,
                                         countermeasure="fallback_selector", duration=duration)
                        self._update_playbook_selector(domain, selector, fallback)
                        return {"success": True, "selector_used": fallback,
                                "original_failed": selector}
                    except Exception:
                        continue

            # 3. LLM-based self-healing: discover page elements, ask LLM
            logger.info(f"Click selector '{effective}' failed on {domain}, "
                        f"attempting LLM self-healing")
            try:
                elements = await self._discover_form_elements(domain)
                description = fallback_text or selector
                healed = self._llm_match_selector(description, elements)
                if healed:
                    try:
                        await self.browser.click(healed)
                        duration = int((time.time() - start) * 1000)
                        self._log_action(domain, "click", healed, None, True,
                                         countermeasure="llm_healed", duration=duration)
                        self._update_playbook_selector(domain, selector, healed)
                        logger.info(f"LLM healed click: '{selector}' → '{healed}'")
                        return {"success": True, "selector_used": healed,
                                "original_failed": selector, "healed": True}
                    except Exception:
                        pass
            except Exception as heal_err:
                logger.debug(f"LLM healing failed for click: {heal_err}")

            duration = int((time.time() - start) * 1000)
            self._log_action(domain, "click", selector, None, False,
                             "dom_change", str(e), duration)
            return {"success": False, "error": str(e), "selector": selector}

    async def smart_type(self, selector: str, text: str,
                         domain: str = "", human_like: bool = True) -> dict[str, Any]:
        """Type with human-like keystroke timing and self-healing selectors."""
        start = time.time()

        # Check if we already learned a better selector
        effective = self._get_known_selector(domain, selector) or selector

        try:
            if human_like:
                page = await self.browser._get_page()
                await page.click(effective)
                for char in text:
                    await page.keyboard.type(char, delay=random.randint(30, 150))
                    if random.random() < 0.05:  # 5% chance of brief pause
                        await asyncio.sleep(random.uniform(0.2, 0.8))
            else:
                await self.browser.type_text(effective, text)

            duration = int((time.time() - start) * 1000)
            self._log_action(domain, "type", effective, None, True, duration=duration)
            return {"success": True}

        except Exception as e:
            # Self-healing: discover page elements, ask LLM for correct selector
            logger.info(f"Type selector '{effective}' failed on {domain}, "
                        f"attempting LLM self-healing")
            try:
                elements = await self._discover_form_elements(domain)
                healed = self._llm_match_selector(selector, elements)
                if healed:
                    try:
                        page = await self.browser._get_page()
                        await page.click(healed)
                        if human_like:
                            for char in text:
                                await page.keyboard.type(char, delay=random.randint(30, 150))
                        else:
                            await self.browser.type_text(healed, text)

                        duration = int((time.time() - start) * 1000)
                        self._log_action(domain, "type", healed, None, True,
                                         countermeasure="llm_healed", duration=duration)
                        self._update_playbook_selector(domain, selector, healed)
                        logger.info(f"LLM healed type: '{selector}' → '{healed}'")
                        return {"success": True, "healed_selector": healed,
                                "original_selector": selector}
                    except Exception:
                        pass
            except Exception as heal_err:
                logger.debug(f"LLM healing failed for type: {heal_err}")

            duration = int((time.time() - start) * 1000)
            self._log_action(domain, "type", selector, None, False,
                             "dom_change", str(e), duration)
            return {"success": False, "error": str(e)}

    async def smart_fill_form(self, fields: dict[str, str],
                              domain: str = "") -> dict[str, Any]:
        """Fill a form with human-like behavior and self-healing selectors.

        Uses a pre-healing strategy: discovers page elements upfront and
        resolves all selectors BEFORE attempting to type, avoiding costly
        30s timeouts per field when selectors don't match.
        """
        results = {}

        # --- Pre-healing: resolve all selectors upfront ---
        resolved_fields = self._pre_resolve_selectors(fields, domain)
        unresolved = [s for s, r in resolved_fields.items() if r == s
                      and not self._get_known_selector(domain, s)]

        if unresolved:
            # Discover real page elements and batch-match all unresolved fields
            discovered = await self._discover_form_elements(domain)
            if discovered:
                batch_map = self._llm_batch_match_selectors(
                    unresolved, discovered)
                for orig, healed in batch_map.items():
                    if healed:
                        resolved_fields[orig] = healed
                        logger.info(
                            f"Pre-healed selector: '{orig}' → '{healed}'")
                    elif healed is None:
                        # LLM explicitly said no element matches this field.
                        # Mark it so we skip filling instead of timing out.
                        resolved_fields[orig] = None
                        # Cache the "missing" result so future fill_form
                        # calls skip this field instantly without LLM calls
                        self._update_playbook_selector(
                            domain, orig, "__MISSING__")
                        logger.warning(
                            f"Field '{orig}' has NO matching element on "
                            f"{domain} — will skip filling")

        # --- Fill each field with the resolved selector ---
        for selector, value in fields.items():
            effective = resolved_fields.get(selector, selector)

            # Skip fields that don't exist on this page (LLM returned null)
            if effective is None:
                results[selector] = {
                    "success": False,
                    "skipped": True,
                    "reason": f"No matching element found on {domain}",
                }
                logger.info(
                    f"Skipping field '{selector}' — not present on {domain}")
                continue

            # Custom dropdown components (SearchableSelect, React-Select, etc.)
            # These need click → type → select-option, not standard text input
            if effective == "__CUSTOM_DROPDOWN__":
                result = await self._fill_custom_dropdown(
                    selector, value, domain)
                results[selector] = result
                continue

            await self._human_delay(short=True)

            # Handle multi-step forms: if the target element is hidden,
            # try to reveal it (scroll into view, click next/continue)
            result = await self._reveal_if_hidden(effective, domain)
            if result.get("revealed"):
                logger.info(f"Revealed hidden field '{effective}' on {domain}")

            result = await self.smart_type(effective, value, domain,
                                           human_like=True)

            if result.get("success") and effective != selector:
                # Cache the successful healing for future use
                self._update_playbook_selector(domain, selector, effective)
                result["healed_selector"] = effective
                result["original_selector"] = selector

            results[selector] = result

        # Fields that were skipped (not present on page) don't count as failures
        filled_results = [r for r in results.values() if not r.get("skipped")]
        all_ok = bool(filled_results) and all(
            r.get("success") for r in filled_results
        )

        # --- Code-gen fallback: when standard fill fails, write & execute a script ---
        # Include BOTH failed fields AND skipped fields (complex UI components
        # like SearchableSelect that weren't matched to standard form elements
        # but likely exist as custom React/Vue components on the page).
        # Exclude fields already handled by _fill_custom_dropdown (they already
        # went through their own codegen path).
        custom_dropdown_fields = {
            sel for sel, eff in resolved_fields.items()
            if eff == "__CUSTOM_DROPDOWN__"
        }
        unfilled_fields = {
            sel: val for sel, val in fields.items()
            if not results.get(sel, {}).get("success")
            and sel not in custom_dropdown_fields
        }
        if unfilled_fields:
            logger.info(
                f"Standard fill missed {len(unfilled_fields)} fields on "
                f"{domain}, attempting code-gen fallback"
            )
            codegen_result = await self._codegen_fill_form(
                unfilled_fields, domain)
            if codegen_result.get("success"):
                # Update results for the fields that codegen handled
                for sel in unfilled_fields:
                    results[sel] = {
                        "success": True,
                        "codegen": True,
                        "script_used": True,
                    }
                    # Clear __MISSING__ cache — codegen proved it exists
                    if self._get_known_selector(domain, sel) == "__MISSING__":
                        self._update_playbook_selector(
                            domain, sel, "__CODEGEN__")
                # Recompute success
                filled_results = [
                    r for r in results.values() if not r.get("skipped")]
                all_ok = bool(filled_results) and all(
                    r.get("success") for r in filled_results)
                logger.info(
                    f"Code-gen fallback succeeded for {len(unfilled_fields)} "
                    f"fields on {domain}")
            else:
                logger.warning(
                    f"Code-gen fallback also failed on {domain}: "
                    f"{codegen_result.get('error', 'unknown')}")

        # Self-healing: check for CAPTCHA after filling form
        if all_ok:
            try:
                page_info = await self.browser.get_page_info()
                failure = self._detect_failure(page_info)
                if failure == "captcha":
                    logger.info(f"CAPTCHA detected after form fill on {domain}")
                    captcha_result = await self._handle_captcha(domain)
                    if captcha_result.get("success"):
                        return {"success": True, "fields": results,
                                "captcha_solved": True}
                    return {"success": False, "fields": results,
                            "captcha_failed": True}
            except Exception as e:
                logger.debug(f"Post-fill CAPTCHA check error: {e}")

        skipped_fields = [k for k, v in results.items() if v.get("skipped")]
        result = {"success": all_ok, "fields": results}
        if skipped_fields:
            result["skipped_fields"] = skipped_fields
            result["note"] = (
                f"Fields {skipped_fields} were skipped because they don't "
                f"exist on this page. The signup form may not require them, "
                f"or they may appear on a later step."
            )
        return result

    # ── Failure Detection ─────────────────────────────────────────

    # URL path keywords that signal a registration/signup page
    _REGISTRATION_PATH_KEYWORDS = {"register", "signup", "sign-up", "join", "create-account"}
    # URL path keywords that signal the user is already logged in (dashboard)
    _DASHBOARD_PATH_KEYWORDS = {"dashboard", "home", "overview", "account", "settings", "app"}

    def _detect_redirect_mismatch(self, requested_url: str,
                                  page_info: dict[str, Any]) -> str | None:
        """Detect when navigation landed on a different page than expected.

        Common case: requesting /register but landing on /dashboard because
        the browser has stale session cookies from a previous account.
        Returns a human-readable warning string, or None if no mismatch.
        """
        actual_url = page_info.get("url", "")
        if not actual_url:
            return None

        requested_path = urlparse(requested_url).path.lower().rstrip("/")
        actual_path = urlparse(actual_url).path.lower().rstrip("/")

        # No mismatch if paths are the same
        if requested_path == actual_path:
            return None

        # Detect: requested registration page, but landed on dashboard/login
        requested_is_registration = any(
            kw in requested_path for kw in self._REGISTRATION_PATH_KEYWORDS
        )
        actual_is_dashboard = any(
            kw in actual_path for kw in self._DASHBOARD_PATH_KEYWORDS
        )
        actual_is_login = "login" in actual_path or "signin" in actual_path

        if requested_is_registration and actual_is_dashboard:
            return (
                f"REDIRECT: Requested {requested_path} but landed on "
                f"{actual_path}. This looks like a dashboard — you may "
                f"already be logged into an existing account. The "
                f"registration form will NOT be present. Either clear "
                f"cookies/use incognito, or call fail() explaining "
                f"that an existing session was detected."
            )

        if requested_is_registration and actual_is_login:
            return (
                f"REDIRECT: Requested {requested_path} but landed on "
                f"{actual_path}. The site redirected to a login page. "
                f"Look for a 'Create account' or 'Sign up' link on "
                f"this page, or the registration form may be embedded."
            )

        # Generic path mismatch — just warn
        if requested_path and actual_path and requested_path != actual_path:
            return (
                f"URL changed: requested {requested_path}, landed on "
                f"{actual_path}. The page content may differ from what "
                f"you expected."
            )

        return None

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

    def _get_captcha_solver(self):
        """Lazy-load CAPTCHA solver."""
        if self._captcha_solver is None:
            from monai.agents.captcha_solver import CaptchaSolver
            self._captcha_solver = CaptchaSolver(self.config, self.db)
        return self._captcha_solver

    async def _handle_captcha(self, domain: str) -> dict[str, Any]:
        """Handle CAPTCHA challenges via external solving service."""
        logger.warning(f"CAPTCHA detected on {domain}, attempting solve")
        solver = self._get_captcha_solver()

        try:
            page = await self.browser._get_page()
            page_url = page.url
            result = await solver.solve_from_page(page, page_url, domain)

            if result.get("status") == "solved":
                logger.info(f"CAPTCHA solved on {domain} in {result.get('solve_time_ms')}ms")
                self._log_action(domain, "captcha_solve", None, page_url, True,
                                 countermeasure="captcha_service")
                # Wait for page to process the token
                await asyncio.sleep(2)
                # Check if CAPTCHA is gone
                page_info = await self.browser.get_page_info()
                if not self._detect_failure(page_info):
                    return {"action": "captcha_solved", "success": True,
                            "cost_usd": result.get("cost_usd", 0)}

                # Sometimes need to click submit after injection
                try:
                    submit_selectors = [
                        "button[type='submit']", "input[type='submit']",
                        "#captcha-submit", ".submit-button",
                    ]
                    for sel in submit_selectors:
                        try:
                            await page.click(sel, timeout=2000)
                            break
                        except Exception:
                            continue
                    await asyncio.sleep(2)
                except Exception:
                    pass

                return {"action": "captcha_solved", "success": True,
                        "cost_usd": result.get("cost_usd", 0)}

            logger.warning(f"CAPTCHA solve failed on {domain}: {result.get('error')}")
            self._log_action(domain, "captcha_solve", None, None, False,
                             failure_type="captcha",
                             error_message=result.get("error", "unknown"))
            return {"action": "captcha_failed", "success": False,
                    "error": result.get("error")}

        except Exception as e:
            logger.error(f"CAPTCHA handling error on {domain}: {e}")
            return {"action": "captcha_error", "success": False, "error": str(e)}

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

    async def _reveal_if_hidden(self, selector: str, domain: str) -> dict[str, Any]:
        """Try to reveal a hidden form element (multi-step signup forms).

        Many signup pages (LinkedIn, Gumroad, LemonSqueezy) have fields that
        exist in the DOM but are hidden until you scroll down, click "Join",
        or progress through a wizard step. This method detects hidden elements
        and tries common patterns to reveal them.
        """
        try:
            page = await self.browser._get_page()
            # Check if the element exists but is hidden
            is_hidden = await page.evaluate(f"""(sel) => {{
                const el = document.querySelector(sel);
                if (!el) return null;  // Element doesn't exist
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return {{
                    exists: true,
                    visible: rect.width > 0 && rect.height > 0,
                    display: style.display,
                    visibility: style.visibility
                }};
            }}""", selector)

            if not is_hidden or is_hidden.get("visible"):
                return {"revealed": False, "reason": "already_visible_or_missing"}

            # Element exists but is hidden — try to reveal it
            logger.info(f"Element '{selector}' is hidden on {domain}, attempting reveal")

            # Strategy 1: Scroll element into view
            try:
                await page.evaluate(f"""(sel) => {{
                    const el = document.querySelector(sel);
                    if (el) el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                }}""", selector)
                await asyncio.sleep(0.5)

                # Check if now visible
                vis_check = await page.evaluate(f"""(sel) => {{
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }}""", selector)
                if vis_check:
                    return {"revealed": True, "strategy": "scroll_into_view"}
            except Exception:
                pass

            # Strategy 2: Click common "next step" buttons
            next_buttons = [
                'button[type="submit"]',
                'button:has-text("Join")',
                'button:has-text("Continue")',
                'button:has-text("Next")',
                'button:has-text("Sign up")',
                'button:has-text("Get started")',
                'a:has-text("Join now")',
            ]
            for btn_selector in next_buttons:
                try:
                    btn = page.locator(btn_selector).first
                    if await btn.is_visible():
                        await btn.click()
                        await asyncio.sleep(1.0)

                        # Check if target is now visible
                        vis_check = await page.evaluate(f"""(sel) => {{
                            const el = document.querySelector(sel);
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                        }}""", selector)
                        if vis_check:
                            return {"revealed": True, "strategy": f"clicked:{btn_selector}"}
                except Exception:
                    continue

            return {"revealed": False, "reason": "could_not_reveal"}
        except Exception as e:
            logger.debug(f"Reveal check failed for '{selector}': {e}")
            return {"revealed": False, "error": str(e)}

    # ── Self-Healing Selectors ────────────────────────────────────

    async def _discover_form_elements(self, domain: str) -> list[dict]:
        """Extract all interactive form elements from the current page.

        Includes BOTH visible AND hidden elements (for multi-step forms where
        fields like input#first-name exist in DOM but are hidden until a step
        transition). The `isVisible` flag lets callers distinguish them.
        """
        page = await self.browser._get_page()
        elements = await page.evaluate("""() => {
            // Standard form elements + custom interactive components
            // (React/Vue SearchableSelect, combobox, listbox, etc.)
            const standardEls = document.querySelectorAll(
                'input, textarea, select, button'
            );
            const customEls = document.querySelectorAll(
                '[role="combobox"], [role="listbox"], [role="searchbox"], '
                + '[role="spinbutton"], [contenteditable="true"], '
                + '[data-testid], [aria-haspopup="listbox"], '
                + '[class*="Select"], [class*="select"], '
                + '[class*="Dropdown"], [class*="dropdown"], '
                + '[class*="Combobox"], [class*="combobox"], '
                + '[class*="Autocomplete"], [class*="autocomplete"]'
            );
            // Merge and deduplicate
            const seen = new Set();
            const allEls = [];
            for (const el of [...standardEls, ...customEls]) {
                if (!seen.has(el)) {
                    seen.add(el);
                    allEls.push(el);
                }
            }
            return allEls.map(el => {
                const rect = el.getBoundingClientRect();
                const isVis = rect.width > 0 && rect.height > 0;
                const role = el.getAttribute('role') || '';
                return {
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type') || '',
                    name: el.getAttribute('name') || '',
                    id: el.getAttribute('id') || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    className: el.className || '',
                    role: role,
                    visibleText: el.innerText || el.value || '',
                    boundingBox: {
                        x: rect.x, y: rect.y,
                        width: rect.width, height: rect.height
                    },
                    isVisible: isVis,
                    isCustom: role === 'combobox' || role === 'listbox'
                        || role === 'searchbox' || el.hasAttribute('aria-haspopup')
                        || (el.className && /Select|Dropdown|Combobox|Autocomplete/i.test(el.className))
                };
            }).filter(e => {
                // Include visible elements AND hidden elements that have a
                // name or id (likely real form fields in a multi-step flow)
                return e.isVisible || e.name || e.id;
            });
        }""")
        logger.debug(f"Discovered {len(elements)} form elements on {domain}")
        return elements

    def _llm_match_selector(self, field_description: str,
                            elements: list[dict]) -> str | None:
        """Use the LLM to find the best CSS selector for a field."""
        elements_summary = json.dumps(elements, indent=2)
        prompt = (
            f"Given these interactive form elements on a web page:\n"
            f"{elements_summary}\n\n"
            f"Which element best matches the field '{field_description}'?\n"
            f"Return ONLY a single CSS selector string that uniquely identifies "
            f"the matching element (e.g. '#email', 'input[name=\"user\"]'). "
            f"If no element matches, return the word NONE."
        )
        response = self.llm.quick(prompt)
        selector = response.strip().strip("`").strip('"').strip("'")
        if not selector or selector.upper() == "NONE":
            return None
        return selector

    def _get_known_selector(self, domain: str,
                            original_selector: str) -> str | None:
        """Check if we already learned a replacement selector for this domain."""
        if not domain:
            return None
        rows = self.db.execute(
            "SELECT known_selectors FROM site_playbooks WHERE domain = ?",
            (domain,),
        )
        if rows:
            selectors = json.loads(rows[0]["known_selectors"] or "{}")
            return selectors.get(original_selector)
        return None

    # ------------------------------------------------------------------
    #  Pre-healing helpers — resolve selectors before attempting to type
    # ------------------------------------------------------------------

    def _pre_resolve_selectors(
        self, fields: dict[str, str], domain: str
    ) -> dict[str, str | None]:
        """Fast, local resolution of selectors using playbook + heuristics.

        Returns a dict mapping original selector → best-guess selector.
        If no better option is found, the original selector is returned as-is.
        A value of None means the field was explicitly determined to not exist.
        """
        resolved: dict[str, str | None] = {}
        for selector in fields:
            # 1. Playbook (previously learned for this domain)
            known = self._get_known_selector(domain, selector)
            if known == "__MISSING__":
                # Previously confirmed this field doesn't exist as a
                # standard element — but codegen might still handle it
                # (complex UI components like SearchableSelect).
                # Mark as None so standard fill skips it, but codegen
                # fallback will still attempt it.
                resolved[selector] = None
                logger.info(
                    f"Field '{selector}' known missing on {domain} "
                    f"(cached) — standard fill will skip, codegen may try")
            elif known == "__CODEGEN__":
                # Previously filled via codegen — skip standard fill,
                # let codegen handle it again (it caches its scripts)
                resolved[selector] = None
                logger.info(
                    f"Field '{selector}' handled by codegen on {domain} "
                    f"(cached) — routing to codegen fallback")
            elif known == "__CUSTOM_DROPDOWN__":
                # Known custom dropdown component (SearchableSelect, etc.)
                # Skip standard fill — handled by _fill_custom_dropdown
                resolved[selector] = "__CUSTOM_DROPDOWN__"
                logger.info(
                    f"Field '{selector}' is a custom dropdown on {domain} "
                    f"— routing to dropdown handler")
            elif known:
                resolved[selector] = known
            else:
                resolved[selector] = selector
        return resolved

    def _llm_batch_match_selectors(
        self, field_selectors: list[str], elements: list[dict]
    ) -> dict[str, str | None]:
        """Single LLM call to match ALL unresolved fields to page elements."""
        elements_summary = json.dumps(elements, indent=2)
        fields_list = "\n".join(f"- {s}" for s in field_selectors)
        prompt = (
            f"Given these interactive form elements on a web page:\n"
            f"{elements_summary}\n\n"
            f"Match each of these form field selectors to the best element:\n"
            f"{fields_list}\n\n"
            f"Return a JSON object mapping each selector to its best CSS "
            f"selector match. Use precise selectors (prefer id-based, then "
            f"name-based, then type+placeholder). "
            f"If no match exists, map to null.\n"
            f"Return ONLY the raw JSON, no markdown fences."
        )
        try:
            response = self.llm.quick(prompt)
            # Strip any markdown fencing
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            mapping = json.loads(text)
            return {k: v for k, v in mapping.items()
                    if isinstance(v, str) or v is None}
        except Exception as e:
            logger.warning(f"LLM batch selector match failed: {e}")
            return {}

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

    # ── Custom Dropdown Handler ──────────────────────────────────
    #
    # Handles custom React/Vue dropdown components (SearchableSelect,
    # react-select, combobox, etc.) that can't be filled with standard
    # text input.  Uses a reliable click → type → select pattern.

    async def _fill_custom_dropdown(
        self, selector: str, value: str, domain: str
    ) -> dict[str, Any]:
        """Fill a custom dropdown component (SearchableSelect, react-select, etc.).

        Strategy:
        1. Discover the actual dropdown elements on the page
        2. Generate a targeted script that clicks the trigger, types the
           value to filter options, then clicks the matching option
        3. Cache the script for future use on this domain
        """
        logger.info(
            f"Filling custom dropdown '{selector}' = '{value}' on {domain}")
        try:
            page = await self.browser._get_page()

            # Extract the dropdown's DOM structure for targeted script gen
            dropdown_info = await page.evaluate("""(sel) => {
                // Find the dropdown container by the original selector or
                // common patterns (aria-label, class, role)
                const ariaMatch = sel.match(/aria-label=['\"]([^'\"]+)['\"]/);
                const label = ariaMatch ? ariaMatch[1] : '';

                // Strategy: find all potential dropdown triggers
                const candidates = [
                    ...document.querySelectorAll('[role="combobox"]'),
                    ...document.querySelectorAll('[aria-haspopup="listbox"]'),
                    ...document.querySelectorAll('[class*="SearchableSelect"]'),
                    ...document.querySelectorAll('[class*="select"]'),
                    ...document.querySelectorAll('[class*="Select"]'),
                ];
                // Also try the original selector directly
                const direct = document.querySelector(sel);
                if (direct && !candidates.includes(direct)) {
                    candidates.unshift(direct);
                }

                return candidates.map(el => ({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    className: (typeof el.className === 'string' ? el.className : '') || '',
                    role: el.getAttribute('role') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    ariaExpanded: el.getAttribute('aria-expanded'),
                    ariaHaspopup: el.getAttribute('aria-haspopup') || '',
                    text: (el.textContent || '').substring(0, 100).trim(),
                    rect: (() => {
                        const r = el.getBoundingClientRect();
                        return {x: r.x, y: r.y, w: r.width, h: r.height};
                    })(),
                    // Check for nested input (searchable dropdowns)
                    hasInput: !!el.querySelector('input'),
                    inputSelector: el.querySelector('input')
                        ? (el.querySelector('input').id
                            ? '#' + el.querySelector('input').id
                            : 'input')
                        : null,
                })).filter(e => e.rect.w > 0 && e.rect.h > 0);
            }""", selector)

            if not dropdown_info:
                logger.warning(
                    f"No custom dropdown elements found for '{selector}' "
                    f"on {domain}")
                return {"success": False, "error": "No dropdown elements found"}

            # Generate a targeted dropdown-fill script
            script = self._generate_dropdown_script(
                selector, value, dropdown_info, domain)
            if not script:
                return {
                    "success": False,
                    "error": "Failed to generate dropdown script",
                }

            # Execute it
            await self._human_delay(short=True)
            result = await self._execute_form_script(
                page, script, {selector: value})

            if result.get("success"):
                # Cache as __CODEGEN__ so future calls use codegen path
                self._update_playbook_selector(
                    domain, selector, "__CODEGEN__")
                # Also cache the script itself
                form_sig = self._form_signature({selector: value})
                url_path = urlparse(page.url).path
                self._cache_form_script(
                    domain, url_path, form_sig, script, {selector: value})
                logger.info(
                    f"Custom dropdown filled and cached for {domain}")
            else:
                logger.warning(
                    f"Custom dropdown fill failed on {domain}: "
                    f"{result.get('error')}")

            return result

        except Exception as e:
            logger.error(f"Custom dropdown handler failed on {domain}: {e}")
            return {"success": False, "error": str(e)}

    def _generate_dropdown_script(
        self,
        selector: str,
        value: str,
        dropdown_info: list[dict],
        domain: str,
    ) -> str | None:
        """Generate JS to interact with a custom dropdown component.

        Uses a more specific prompt than generic codegen, with explicit
        patterns for SearchableSelect, react-select, etc.
        """
        info_json = json.dumps(dropdown_info[:10], indent=2)

        prompt = (
            f"Write JavaScript to select '{value}' in a custom dropdown "
            f"component on {domain}.\n\n"
            f"## Dropdown Elements Found\n"
            f"```json\n{info_json}\n```\n\n"
            f"## Original Selector\n`{selector}`\n\n"
            f"## Required Steps\n"
            f"1. Click the dropdown trigger/container to open it\n"
            f"2. Wait 300ms for the options list to render\n"
            f"3. If the dropdown has a search input, type the value to filter\n"
            f"4. Wait 300ms for filtering\n"
            f"5. Find and click the matching option by text content\n"
            f"6. Return {{filled: ['{selector}'], failed: []}}\n\n"
            f"## Common Patterns\n"
            f"- SearchableSelect: click container → type in nested input → "
            f"  click [role='option'] or [data-value='XX']\n"
            f"- React-Select: click .Select__control → type in "
            f"  .Select__input input → click .Select__option\n"
            f"- Combobox: click [role='combobox'] → type → click "
            f"  [role='option']\n\n"
            f"## Template\n"
            f"Code runs as: `async (FIELD_VALUES) => {{ <YOUR CODE> }}`\n"
            f"Use `sleep(ms)` for delays. Use document.querySelector.\n"
            f"Try MULTIPLE selector strategies if the first fails.\n"
            f"Return ONLY raw JavaScript, no markdown fences."
        )

        try:
            response = self.llm.quick(prompt)
            script = response.strip()
            if script.startswith("```"):
                script = script.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            # Ethics review
            from monai.agents.ethics import is_script_ethical
            is_ethical, reason = is_script_ethical(
                script,
                context=f"Custom dropdown fill on {domain}: {selector}",
                task_context=self.task_context,
                script_type="browser_js",
                llm=self.llm,
            )
            if not is_ethical:
                logger.warning(
                    f"Dropdown script BLOCKED by ethics: {reason}")
                return None
            return script
        except Exception as e:
            logger.error(f"Dropdown script generation failed: {e}")
            return None

    # ── Code-Generation Fallback ─────────────────────────────────
    #
    # When standard selector-based form filling fails, the agent writes
    # a Playwright script tailored to the exact page DOM, executes it,
    # and caches it for reuse.  This is the "write code to solve it"
    # capability that turns the agent from a dumb tool-caller into an
    # adaptive coder.

    async def _codegen_fill_form(
        self, fields: dict[str, str], domain: str
    ) -> dict[str, Any]:
        """Generate and execute a Playwright script to fill form fields.

        This is the nuclear option: when pre-healing + LLM selector matching
        both fail, we ask the LLM to write actual Playwright code that
        interacts with the page directly.  The script runs in the current
        page context via page.evaluate() for JS or via Playwright API calls.

        The generated script is cached in form_scripts for reuse on the
        same form in future sessions.
        """
        try:
            # Pre-check: detect dropdown-like selectors and route them
            # through the specialized handler for better reliability.
            dropdown_patterns = (
                "Select", "select", "Dropdown", "dropdown",
                "Combobox", "combobox", "aria-haspopup",
            )
            dropdown_fields = {}
            regular_fields = {}
            for sel, val in fields.items():
                if any(p in sel for p in dropdown_patterns):
                    dropdown_fields[sel] = val
                else:
                    regular_fields[sel] = val

            # Handle detected dropdowns via specialized handler
            dropdown_results = {}
            if dropdown_fields:
                for sel, val in dropdown_fields.items():
                    result = await self._fill_custom_dropdown(
                        sel, val, domain)
                    dropdown_results[sel] = result

                # If all fields were dropdowns and all succeeded, done
                if not regular_fields and all(
                    r.get("success") for r in dropdown_results.values()
                ):
                    return {"success": True, "codegen": True,
                            "dropdown_handled": True}

                # Continue with regular fields only
                fields = regular_fields
                if not fields:
                    # Only dropdown fields, some failed
                    any_ok = any(
                        r.get("success") for r in dropdown_results.values())
                    return {"success": any_ok, "codegen": True,
                            "dropdown_results": dropdown_results}

            page = await self.browser._get_page()
            url = page.url
            url_path = urlparse(url).path

            # 1. Check cache first — maybe we already have a working script
            form_sig = self._form_signature(fields)
            cached = self._get_cached_script(domain, form_sig)
            if cached:
                logger.info(
                    f"Found cached form script for {domain}{url_path}")
                result = await self._execute_form_script(
                    page, cached, fields)
                if result.get("success"):
                    self._update_script_stats(domain, form_sig, success=True)
                    return result
                else:
                    # Cached script failed — it might be stale
                    self._update_script_stats(domain, form_sig, success=False)
                    logger.info(
                        f"Cached script failed for {domain}, regenerating")

            # 2. Extract full page context for the LLM
            page_html = await page.evaluate("""() => {
                // Get a minimal but informative DOM snapshot
                const forms = document.querySelectorAll('form');
                if (forms.length > 0) {
                    return Array.from(forms).map(f => f.outerHTML).join('\\n');
                }
                // No form tags — get the main content area
                const main = document.querySelector('main, [role="main"], .main, #app, #root, body');
                return main ? main.innerHTML.substring(0, 15000) : document.body.innerHTML.substring(0, 15000);
            }""")

            elements = await self._discover_form_elements(domain)

            # 3. Ask LLM to write a Playwright script
            script = self._generate_form_script(
                fields, elements, page_html, url, domain)
            if not script:
                return {"success": False, "error": "LLM failed to generate script"}

            # 4. Execute the script
            result = await self._execute_form_script(page, script, fields)

            # 5. Cache if successful
            if result.get("success"):
                self._cache_form_script(
                    domain, url_path, form_sig, script, fields)
                logger.info(
                    f"Generated and cached form script for {domain}{url_path}")

            return result

        except Exception as e:
            logger.error(f"Code-gen form fill failed on {domain}: {e}")
            return {"success": False, "error": str(e)}

    def _generate_form_script(
        self,
        fields: dict[str, str],
        elements: list[dict],
        page_html: str,
        url: str,
        domain: str,
    ) -> str | None:
        """Ask LLM to write Playwright JS code to fill the form.

        Returns executable JavaScript code or None on failure.
        """
        fields_desc = json.dumps(
            {k: f"<value:{k}>" for k in fields}, indent=2)
        elements_summary = json.dumps(elements[:30], indent=2)
        # Truncate HTML to keep prompt reasonable
        html_snippet = page_html[:8000]

        prompt = (
            f"You are writing Playwright JavaScript code to fill a form on {url}.\n\n"
            f"## Form Fields to Fill\n"
            f"```json\n{fields_desc}\n```\n"
            f"The actual values will be injected at runtime via the `FIELD_VALUES` object.\n\n"
            f"## Interactive Elements on Page\n"
            f"```json\n{elements_summary}\n```\n\n"
            f"## Page HTML (truncated)\n"
            f"```html\n{html_snippet}\n```\n\n"
            f"## Requirements\n"
            f"Write a JavaScript async function body that:\n"
            f"1. Finds and fills each form field using the ACTUAL selectors from the DOM\n"
            f"2. Handles React/Vue/Angular apps (use input events, not just .value=)\n"
            f"3. Dispatches proper events (input, change, blur) so frameworks detect the change\n"
            f"4. Handles multi-step forms (click Next/Continue if needed before filling later fields)\n"
            f"5. Uses human-like delays between fields (50-200ms)\n"
            f"6. Returns a JSON object: {{filled: ['field1', 'field2'], failed: ['field3']}}\n\n"
            f"## Template\n"
            f"The code will be wrapped in: `async (FIELD_VALUES) => {{ <YOUR CODE> }}`\n"
            f"FIELD_VALUES is an object mapping original field keys to their values.\n\n"
            f"## CRITICAL\n"
            f"- Return ONLY the function body (no wrapping function declaration)\n"
            f"- Use document.querySelector, NOT Playwright selectors\n"
            f"- To trigger React state updates, use: \n"
            f"  `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, val);\n"
            f"   el.dispatchEvent(new Event('input', {{bubbles: true}}));\n"
            f"   el.dispatchEvent(new Event('change', {{bubbles: true}}));`\n"
            f"- For CUSTOM COMPONENTS (SearchableSelect, React-Select, Combobox, etc.):\n"
            f"  1. Click the container/trigger element to open the dropdown\n"
            f"  2. Wait 200ms for options to render\n"
            f"  3. Find the input inside (if searchable) and type the value\n"
            f"  4. Wait 200ms then click the matching option from the dropdown\n"
            f"  5. Look for [role='combobox'], [role='listbox'], [role='option'],\n"
            f"     [class*='Select'], [aria-haspopup='listbox'] patterns in the DOM\n"
            f"- Do NOT use alert(), confirm(), or prompt()\n"
            f"- No network requests\n"
            f"- Return ONLY raw JavaScript, no markdown fences"
        )

        try:
            response = self.llm.quick(prompt)
            script = response.strip()
            # Strip markdown fencing if present
            if script.startswith("```"):
                script = script.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            # Ethics review — every generated script must pass
            from monai.agents.ethics import is_script_ethical
            is_ethical, reason = is_script_ethical(
                script,
                context=f"Form fill on {domain}: {list(fields.keys())}",
                task_context=self.task_context,
                script_type="browser_js",
                llm=self.llm,
            )
            if not is_ethical:
                logger.warning(
                    f"Generated form script BLOCKED by ethics review: {reason}")
                return None
            return script
        except Exception as e:
            logger.error(f"LLM script generation failed: {e}")
            return None

    async def _execute_form_script(
        self, page, script: str, fields: dict[str, str]
    ) -> dict[str, Any]:
        """Execute a generated form-fill script on the page.

        The script runs inside page.evaluate() with field values injected.
        Human-like delays are handled inside the script itself.
        """
        # Wrap the script body in an async IIFE with field values
        wrapped = f"""
        async (fieldValues) => {{
            const FIELD_VALUES = fieldValues;
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            try {{
                {script}
            }} catch (err) {{
                return {{filled: [], failed: Object.keys(fieldValues), error: err.message}};
            }}
        }}
        """
        try:
            result = await page.evaluate(wrapped, fields)
            if not isinstance(result, dict):
                result = {"filled": list(fields.keys()), "failed": []}

            filled = result.get("filled", [])
            failed = result.get("failed", [])
            error = result.get("error")

            if error:
                logger.warning(f"Form script execution error: {error}")

            success = len(filled) > 0 and len(failed) == 0
            return {
                "success": success,
                "filled": filled,
                "failed": failed,
                "error": error,
                "codegen": True,
            }
        except Exception as e:
            logger.error(f"Form script execution failed: {e}")
            return {"success": False, "error": str(e), "codegen": True}

    async def run_page_script(self, script: str, args: dict | None = None) -> dict[str, Any]:
        """Execute Playwright JS on the current page.

        This is the public API that the executor's `run_page_script` tool
        calls.  It gives the agent the ability to write code and run it
        against any page, not just forms.

        Every script goes through full ethics review before execution.
        """
        page = await self.browser._get_page()
        domain = urlparse(page.url).netloc

        # Full ethics review — not just pattern matching
        from monai.agents.ethics import is_script_ethical
        is_ethical, reason = is_script_ethical(
            script,
            context=f"run_page_script on {domain} ({page.url})",
            task_context=self.task_context,
            script_type="browser_js",
            llm=self.llm,
        )
        if not is_ethical:
            logger.warning(
                f"run_page_script BLOCKED by ethics review on {domain}: {reason}")
            return {
                "success": False,
                "error": f"Script blocked by ethics review: {reason}",
            }

        start = time.time()
        try:
            if args:
                wrapped = f"async (args) => {{ {script} }}"
                result = await page.evaluate(wrapped, args)
            else:
                wrapped = f"async () => {{ {script} }}"
                result = await page.evaluate(wrapped)

            duration = int((time.time() - start) * 1000)
            self._log_action(domain, "run_script", None, page.url, True,
                             duration=duration, countermeasure="codegen")
            return {"success": True, "result": result, "duration_ms": duration}

        except Exception as e:
            duration = int((time.time() - start) * 1000)
            self._log_action(domain, "run_script", None, page.url, False,
                             "script_error", str(e), duration)
            return {"success": False, "error": str(e), "duration_ms": duration}

    # ── Form Script Cache ─────────────────────────────────────────

    @staticmethod
    def _form_signature(fields: dict[str, str]) -> str:
        """Create a stable hash of field names for cache matching."""
        keys = sorted(fields.keys())
        return hashlib.sha256("|".join(keys).encode()).hexdigest()[:16]

    def _get_cached_script(self, domain: str, form_sig: str) -> str | None:
        """Retrieve a cached form script if it exists and has good success rate."""
        rows = self.db.execute(
            "SELECT script, success_count, fail_count FROM form_scripts "
            "WHERE domain = ? AND form_signature = ?",
            (domain, form_sig),
        )
        if not rows:
            return None
        row = rows[0]
        # Don't use scripts that fail more than they succeed (after 2+ uses)
        total = row["success_count"] + row["fail_count"]
        if total >= 2 and row["fail_count"] > row["success_count"]:
            logger.info(
                f"Cached script for {domain} has poor success rate "
                f"({row['success_count']}/{total}), skipping")
            return None
        return row["script"]

    def _cache_form_script(
        self, domain: str, url_pattern: str, form_sig: str,
        script: str, fields: dict[str, str]
    ) -> None:
        """Cache a successful form script for reuse."""
        field_mapping = json.dumps(list(fields.keys()))
        self.db.execute(
            "INSERT OR REPLACE INTO form_scripts "
            "(domain, url_pattern, form_signature, script, field_mapping, "
            "success_count, last_used) "
            "VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)",
            (domain, url_pattern, form_sig, script, field_mapping),
        )

    def _update_script_stats(
        self, domain: str, form_sig: str, success: bool
    ) -> None:
        """Update success/fail counts for a cached script."""
        col = "success_count" if success else "fail_count"
        self.db.execute(
            f"UPDATE form_scripts SET {col} = {col} + 1, "
            "last_used = CURRENT_TIMESTAMP "
            "WHERE domain = ? AND form_signature = ?",
            (domain, form_sig),
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
