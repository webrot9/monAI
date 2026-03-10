"""CAPTCHA Solver — integrates with solving services for fully autonomous browsing.

Supports:
- 2captcha (reCAPTCHA v2/v3, hCaptcha, Turnstile, image CAPTCHA)
- Anti-Captcha (same capabilities, different provider)
- Automatic provider failover

Flow:
1. Browser detects CAPTCHA → extracts sitekey + page URL
2. Solver submits task to solving service
3. Polls for result (human workers solve it)
4. Returns token → browser injects it into the page
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from monai.config import Config
from monai.db.database import Database
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)

CAPTCHA_SCHEMA = """
CREATE TABLE IF NOT EXISTS captcha_solves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,           -- twocaptcha, anticaptcha
    captcha_type TEXT NOT NULL,       -- recaptcha_v2, recaptcha_v3, hcaptcha, turnstile, image
    domain TEXT NOT NULL,
    success INTEGER NOT NULL,
    cost_usd REAL DEFAULT 0.0,
    solve_time_ms INTEGER,
    task_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class CaptchaSolver:
    """Solves CAPTCHAs via external services for fully autonomous operation."""

    # Average costs per CAPTCHA type (USD)
    COSTS = {
        "recaptcha_v2": 0.003,
        "recaptcha_v3": 0.004,
        "hcaptcha": 0.003,
        "turnstile": 0.003,
        "image": 0.001,
    }

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._anonymizer = get_anonymizer(config)
        self.__http = None

        with db.connect() as conn:
            conn.executescript(CAPTCHA_SCHEMA)

    @property
    def _http(self):
        if self.__http is None:
            self.__http = self._anonymizer.create_http_client(timeout=120)
        return self.__http

    def _get_api_key(self, provider: str) -> str:
        """Get API key for CAPTCHA provider from config."""
        captcha_cfg = self.config.captcha
        if provider == "twocaptcha" and captcha_cfg.twocaptcha_api_key:
            return captcha_cfg.twocaptcha_api_key
        if provider == "anticaptcha" and captcha_cfg.anticaptcha_api_key:
            return captcha_cfg.anticaptcha_api_key
        return captcha_cfg.api_key  # Fallback shared key

    @property
    def _provider(self) -> str:
        """Get configured provider (default: twocaptcha)."""
        return self.config.captcha.provider

    # ── Public API ──────────────────────────────────────────────

    async def solve(self, captcha_type: str, page_url: str,
                    sitekey: str = "", image_base64: str = "",
                    domain: str = "") -> dict[str, Any]:
        """Solve a CAPTCHA and return the token.

        Args:
            captcha_type: recaptcha_v2, recaptcha_v3, hcaptcha, turnstile, image
            page_url: URL of the page with the CAPTCHA
            sitekey: Site key (for reCAPTCHA/hCaptcha/Turnstile)
            image_base64: Base64 image data (for image CAPTCHAs)
            domain: Domain for logging

        Returns:
            Dict with 'token' (the solved token) or 'error'
        """
        provider = self._provider
        api_key = self._get_api_key(provider)
        if not api_key:
            return {
                "status": "error",
                "error": f"No API key for {provider}. "
                         f"Set captcha.{provider}_api_key in config.",
            }

        start = time.time()
        try:
            if provider == "twocaptcha":
                result = await self._solve_twocaptcha(
                    api_key, captcha_type, page_url, sitekey, image_base64)
            elif provider == "anticaptcha":
                result = await self._solve_anticaptcha(
                    api_key, captcha_type, page_url, sitekey, image_base64)
            else:
                return {"status": "error", "error": f"Unknown provider: {provider}"}
        except Exception as e:
            logger.error(f"CAPTCHA solve failed ({provider}): {e}")
            self._record(provider, captcha_type, domain or page_url, False, 0, 0)
            # Try failover
            fallback = "anticaptcha" if provider == "twocaptcha" else "twocaptcha"
            fallback_key = self._get_api_key(fallback)
            if fallback_key:
                logger.info(f"Failing over to {fallback}")
                try:
                    if fallback == "twocaptcha":
                        result = await self._solve_twocaptcha(
                            fallback_key, captcha_type, page_url, sitekey, image_base64)
                    else:
                        result = await self._solve_anticaptcha(
                            fallback_key, captcha_type, page_url, sitekey, image_base64)
                except Exception as e2:
                    return {"status": "error", "error": f"Both providers failed: {e}, {e2}"}
            else:
                return {"status": "error", "error": str(e)}

        solve_ms = int((time.time() - start) * 1000)
        cost = self.COSTS.get(captcha_type, 0.003)

        if result.get("status") == "solved":
            self._record(provider, captcha_type, domain or page_url,
                         True, cost, solve_ms, result.get("task_id"))
            return {
                "status": "solved",
                "token": result["token"],
                "cost_usd": cost,
                "solve_time_ms": solve_ms,
            }

        self._record(provider, captcha_type, domain or page_url, False, 0, solve_ms)
        return result

    async def solve_from_page(self, page: Any, page_url: str,
                              domain: str = "") -> dict[str, Any]:
        """Auto-detect CAPTCHA type from page and solve it.

        Args:
            page: Playwright page object
            page_url: Current URL
            domain: Domain for logging
        """
        detection = await self._detect_captcha_type(page)
        if not detection:
            return {"status": "error", "error": "No CAPTCHA detected on page"}

        captcha_type = detection["type"]
        sitekey = detection.get("sitekey", "")

        result = await self.solve(
            captcha_type=captcha_type,
            page_url=page_url,
            sitekey=sitekey,
            domain=domain,
        )

        if result.get("status") == "solved":
            # Inject the token into the page
            await self._inject_token(page, captcha_type, result["token"])

        return result

    # ── CAPTCHA Detection ───────────────────────────────────────

    async def _detect_captcha_type(self, page: Any) -> dict[str, Any] | None:
        """Detect CAPTCHA type and extract sitekey from page."""
        detections = await page.evaluate("""() => {
            // reCAPTCHA v2
            const recaptchaV2 = document.querySelector('.g-recaptcha, [data-sitekey]');
            if (recaptchaV2) {
                return {
                    type: 'recaptcha_v2',
                    sitekey: recaptchaV2.getAttribute('data-sitekey') || ''
                };
            }

            // reCAPTCHA v3 (script-based)
            const recaptchaScript = document.querySelector('script[src*="recaptcha"]');
            if (recaptchaScript) {
                const src = recaptchaScript.src;
                const match = src.match(/render=([^&]+)/);
                if (match && match[1] !== 'explicit') {
                    return { type: 'recaptcha_v3', sitekey: match[1] };
                }
            }

            // hCaptcha
            const hcaptcha = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
            if (hcaptcha) {
                return {
                    type: 'hcaptcha',
                    sitekey: hcaptcha.getAttribute('data-sitekey') ||
                             hcaptcha.getAttribute('data-hcaptcha-sitekey') || ''
                };
            }

            // Cloudflare Turnstile
            const turnstile = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
            if (turnstile) {
                return {
                    type: 'turnstile',
                    sitekey: turnstile.getAttribute('data-sitekey') || ''
                };
            }

            // Generic CAPTCHA image
            const captchaImg = document.querySelector(
                'img[src*="captcha"], img[alt*="captcha"], img[id*="captcha"]'
            );
            if (captchaImg) {
                return { type: 'image', sitekey: '' };
            }

            return null;
        }""")
        return detections

    async def _inject_token(self, page: Any, captcha_type: str, token: str):
        """Inject solved CAPTCHA token into the page."""
        if captcha_type in ("recaptcha_v2", "recaptcha_v3"):
            await page.evaluate(f"""(token) => {{
                document.getElementById('g-recaptcha-response').value = token;
                // Also set in hidden textareas
                document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {{
                    el.value = token;
                }});
                // Trigger callback if exists
                if (typeof ___grecaptcha_cfg !== 'undefined') {{
                    Object.keys(___grecaptcha_cfg.clients).forEach(key => {{
                        const client = ___grecaptcha_cfg.clients[key];
                        // Find callback in nested structure
                        const findCallback = (obj) => {{
                            for (const k in obj) {{
                                if (typeof obj[k] === 'function') return obj[k];
                                if (typeof obj[k] === 'object' && obj[k]) {{
                                    const found = findCallback(obj[k]);
                                    if (found) return found;
                                }}
                            }}
                        }};
                        const cb = findCallback(client);
                        if (cb) cb(token);
                    }});
                }}
            }}""", token)
        elif captcha_type == "hcaptcha":
            await page.evaluate(f"""(token) => {{
                // Set response in hidden inputs
                document.querySelectorAll('[name="h-captcha-response"]').forEach(el => {{
                    el.value = token;
                }});
                document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {{
                    el.value = token;
                }});
                // Trigger hcaptcha callback
                if (typeof hcaptcha !== 'undefined') {{
                    // hcaptcha stores callbacks internally
                    const iframe = document.querySelector('iframe[src*="hcaptcha"]');
                    if (iframe) {{
                        iframe.dispatchEvent(new Event('hcaptchaSuccess'));
                    }}
                }}
            }}""", token)
        elif captcha_type == "turnstile":
            await page.evaluate(f"""(token) => {{
                const input = document.querySelector('[name="cf-turnstile-response"]');
                if (input) input.value = token;
                // Trigger turnstile callback
                if (typeof turnstile !== 'undefined' && turnstile.getResponse) {{
                    // Cloudflare handles this differently
                    const widgets = document.querySelectorAll('.cf-turnstile');
                    widgets.forEach(w => {{
                        const cb = w.getAttribute('data-callback');
                        if (cb && typeof window[cb] === 'function') window[cb](token);
                    }});
                }}
            }}""", token)

    # ── 2captcha Integration ────────────────────────────────────

    async def _solve_twocaptcha(self, api_key: str, captcha_type: str,
                                page_url: str, sitekey: str,
                                image_base64: str) -> dict[str, Any]:
        """Solve via 2captcha API."""
        base = "https://2captcha.com"

        # Build request params
        params: dict[str, Any] = {"key": api_key, "json": 1}

        if captcha_type == "recaptcha_v2":
            params.update({"method": "userrecaptcha", "googlekey": sitekey,
                           "pageurl": page_url})
        elif captcha_type == "recaptcha_v3":
            params.update({"method": "userrecaptcha", "googlekey": sitekey,
                           "pageurl": page_url, "version": "v3",
                           "min_score": 0.5})
        elif captcha_type == "hcaptcha":
            params.update({"method": "hcaptcha", "sitekey": sitekey,
                           "pageurl": page_url})
        elif captcha_type == "turnstile":
            params.update({"method": "turnstile", "sitekey": sitekey,
                           "pageurl": page_url})
        elif captcha_type == "image":
            params.update({"method": "base64", "body": image_base64})
        else:
            return {"status": "error", "error": f"Unsupported type: {captcha_type}"}

        # Submit task
        resp = self._http.post(f"{base}/in.php", data=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != 1:
            return {"status": "error", "error": data.get("request", "submit failed")}

        task_id = data["request"]

        # Poll for result (max 180 seconds)
        for _ in range(36):
            await asyncio.sleep(5)
            resp = self._http.get(
                f"{base}/res.php",
                params={"key": api_key, "action": "get", "id": task_id, "json": 1},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == 1:
                return {"status": "solved", "token": data["request"],
                        "task_id": task_id}
            if data.get("request") != "CAPCHA_NOT_READY":
                return {"status": "error", "error": data.get("request", "unknown")}

        return {"status": "error", "error": "timeout"}

    # ── Anti-Captcha Integration ────────────────────────────────

    async def _solve_anticaptcha(self, api_key: str, captcha_type: str,
                                 page_url: str, sitekey: str,
                                 image_base64: str) -> dict[str, Any]:
        """Solve via Anti-Captcha API."""
        base = "https://api.anti-captcha.com"

        # Build task
        if captcha_type == "recaptcha_v2":
            task = {"type": "RecaptchaV2TaskProxyless",
                    "websiteURL": page_url, "websiteKey": sitekey}
        elif captcha_type == "recaptcha_v3":
            task = {"type": "RecaptchaV3TaskProxyless",
                    "websiteURL": page_url, "websiteKey": sitekey,
                    "minScore": 0.5}
        elif captcha_type == "hcaptcha":
            task = {"type": "HCaptchaTaskProxyless",
                    "websiteURL": page_url, "websiteKey": sitekey}
        elif captcha_type == "turnstile":
            task = {"type": "TurnstileTaskProxyless",
                    "websiteURL": page_url, "websiteKey": sitekey}
        elif captcha_type == "image":
            task = {"type": "ImageToTextTask", "body": image_base64}
        else:
            return {"status": "error", "error": f"Unsupported type: {captcha_type}"}

        # Create task
        resp = self._http.post(
            f"{base}/createTask",
            json={"clientKey": api_key, "task": task},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("errorId", 0) != 0:
            return {"status": "error",
                    "error": data.get("errorDescription", "create failed")}

        task_id = data["taskId"]

        # Poll for result (max 180 seconds)
        for _ in range(36):
            await asyncio.sleep(5)
            resp = self._http.post(
                f"{base}/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == "ready":
                solution = data.get("solution", {})
                token = (solution.get("gRecaptchaResponse")
                         or solution.get("token")
                         or solution.get("text", ""))
                return {"status": "solved", "token": token, "task_id": str(task_id)}

            if data.get("errorId", 0) != 0:
                return {"status": "error",
                        "error": data.get("errorDescription", "unknown")}

        return {"status": "error", "error": "timeout"}

    # ── Metrics ─────────────────────────────────────────────────

    def _record(self, provider: str, captcha_type: str, domain: str,
                success: bool, cost: float, solve_ms: int,
                task_id: str = ""):
        self.db.execute_insert(
            "INSERT INTO captcha_solves "
            "(provider, captcha_type, domain, success, cost_usd, solve_time_ms, task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (provider, captcha_type, domain, int(success), cost, solve_ms, task_id),
        )

    def get_stats(self) -> dict[str, Any]:
        """Get CAPTCHA solving statistics."""
        rows = self.db.execute(
            "SELECT captcha_type, COUNT(*) as total, "
            "SUM(success) as solved, SUM(cost_usd) as total_cost, "
            "AVG(solve_time_ms) as avg_time_ms "
            "FROM captcha_solves GROUP BY captcha_type"
        )
        return {r["captcha_type"]: dict(r) for r in rows}

    def get_total_cost(self) -> float:
        rows = self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM captcha_solves"
        )
        return rows[0]["total"]
