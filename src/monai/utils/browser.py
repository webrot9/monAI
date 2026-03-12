"""Browser automation layer — gives monAI eyes and hands on the web.

Uses Playwright for full browser control: navigate, click, type, screenshot,
extract content. This is how monAI registers on platforms, manages accounts,
and interacts with any website autonomously.

ALL traffic routed through proxy (Tor/SOCKS5) for complete anonymity.
Browser fingerprint randomized per session to prevent tracking.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from monai.config import Config
from monai.utils.privacy import (
    AllProxiesBlockedError,
    ProxyFallbackChain,
    get_anonymizer,
)

logger = logging.getLogger(__name__)


class Browser:
    """Autonomous browser — all traffic proxied, fingerprint randomized."""

    def __init__(self, config: Config, headless: bool = True):
        self.config = config
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._anonymizer = get_anonymizer(config)
        self.screenshots_dir = config.data_dir / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    async def start(self):
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()

        # Launch with proxy — all traffic routed through Tor/SOCKS5
        launch_args = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=WebGLDraftExtensions",
                "--disable-webgl",
                "--disable-webgl2",
            ],
        }
        proxy_config = self._anonymizer.get_browser_proxy()
        if proxy_config:
            launch_args["proxy"] = proxy_config
            logger.info(f"Browser proxy: {proxy_config['server']}")

        self._browser = await self._playwright.chromium.launch(**launch_args)

        # Randomized fingerprint — prevents cross-session tracking
        fp = self._anonymizer.get_browser_fingerprint()
        self._context = await self._browser.new_context(
            viewport=fp["viewport"],
            user_agent=fp["user_agent"],
            timezone_id=fp["timezone_id"],
            locale=fp["locale"],
            color_scheme=fp["color_scheme"],
            device_scale_factor=fp["device_scale_factor"],
            permissions=[],  # Block all permissions (geolocation, notifications, etc.)
        )

        # Inject anti-fingerprinting and anti-headless-detection scripts
        await self._context.add_init_script("""
            // Disable WebRTC to prevent real IP leak via STUN/TURN
            Object.defineProperty(navigator, 'mediaDevices', { get: () => undefined });
            window.RTCPeerConnection = undefined;
            window.RTCSessionDescription = undefined;
            window.RTCIceCandidate = undefined;
            window.webkitRTCPeerConnection = undefined;
            // Normalize hardware fingerprint
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 4 });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            // Anti-headless detection: spoof chrome.runtime
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
            // Spoof webdriver flag
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            // Spoof plugins (headless has 0 plugins)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            // Spoof languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            // Disable WebGL fingerprinting
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(param) {
                if (param === 37445) return 'Intel Inc.';
                if (param === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.call(this, param);
            };
            // Prevent canvas fingerprinting
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {
                const ctx = this.getContext('2d');
                if (ctx) {
                    const imgData = ctx.getImageData(0, 0, this.width, this.height);
                    for (let i = 0; i < imgData.data.length; i += 4) {
                        imgData.data[i] ^= 1;  // Tiny noise, invisible but breaks fingerprint
                    }
                    ctx.putImageData(imgData, 0, 0);
                }
                return origToDataURL.call(this, type);
            };
        """)

        self._active_proxy_url = proxy_config["server"] if proxy_config else None

        logger.info(
            f"Browser started (proxy={'yes' if proxy_config else 'no'}, "
            f"tz={fp['timezone_id']}, locale={fp['locale']})"
        )

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    async def navigate(self, url: str, wait_for: str = "domcontentloaded") -> str:
        """Navigate to a URL with automatic proxy fallback on block detection.

        If the target domain blocks the current proxy (Tor exit node detected,
        CAPTCHA challenge, 403, etc.), the browser retries with the next proxy
        in the fallback chain.  Maximum 3 retry attempts.

        Raises AllProxiesBlockedError if every proxy is exhausted — the action
        is aborted rather than revealing the real IP.
        """
        max_retries = 3
        domain = urlparse(url).hostname or ""
        fallback = self._anonymizer.fallback_chain

        for attempt in range(max_retries + 1):
            # Determine which proxy to use for this domain
            try:
                proxy_type, proxy_url = fallback.get_proxy_for_domain(domain)
            except AllProxiesBlockedError:
                logger.error(
                    f"NAVIGATE ABORTED: all proxies blocked for {domain} — "
                    f"refusing to connect without proxy"
                )
                raise

            # If the proxy changed from what the browser context was launched
            # with, we need to recreate the context with the new proxy.
            current_proxy = self._current_proxy_url()
            if proxy_url != current_proxy:
                logger.info(
                    f"PROXY FALLBACK: switching from {current_proxy} to "
                    f"{proxy_url} for {domain} (attempt {attempt + 1})"
                )
                await self._recreate_context_with_proxy(proxy_url)

            page = await self._get_page()
            self._anonymizer.maybe_rotate()

            try:
                response = await page.goto(url, wait_until=wait_for)
            except Exception as e:
                logger.warning(
                    f"Navigation error on {domain} via {proxy_type}: {e}"
                )
                if attempt < max_retries:
                    try:
                        fallback.report_blocked(domain, proxy_type)
                    except AllProxiesBlockedError:
                        raise
                    continue
                raise

            content = await page.content()
            status = response.status if response else 0

            # Check for block signals: HTTP status or page content
            blocked = False
            if ProxyFallbackChain.is_blocked_status_code(status):
                logger.warning(
                    f"Blocked status {status} on {domain} via {proxy_type}"
                )
                blocked = True
            elif ProxyFallbackChain.detect_block_page(content):
                logger.warning(
                    f"Block page detected on {domain} via {proxy_type}"
                )
                blocked = True

            if blocked and attempt < max_retries:
                try:
                    fallback.report_blocked(domain, proxy_type)
                except AllProxiesBlockedError:
                    raise
                continue

            if blocked:
                # Final attempt still blocked — abort
                fallback.report_blocked(domain, proxy_type)
                raise AllProxiesBlockedError(
                    f"All retry attempts exhausted for {domain}. "
                    f"Last proxy ({proxy_type}) was also blocked."
                )

            # Success
            fallback.report_success(domain, proxy_type)
            logger.info(f"Navigated to {url} via {proxy_type}")
            return content

        # Should be unreachable, but guard anyway
        raise AllProxiesBlockedError(f"Navigation to {domain} failed after all retries")

    async def screenshot(self, name: str = "page") -> Path:
        page = await self._get_page()
        path = self.screenshots_dir / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        # Strip metadata from screenshot
        self._anonymizer.strip_file_metadata(path)
        logger.info(f"Screenshot saved: {path}")
        return path

    async def get_text(self) -> str:
        page = await self._get_page()
        return await page.inner_text("body")

    async def click(self, selector: str):
        page = await self._get_page()
        await page.click(selector)
        logger.info(f"Clicked: {selector}")

    async def type_text(self, selector: str, text: str):
        page = await self._get_page()
        await page.fill(selector, text)
        logger.info(f"Typed into: {selector}")

    async def select_option(self, selector: str, value: str):
        page = await self._get_page()
        await page.select_option(selector, value)

    async def wait_for(self, selector: str, timeout: int = 10000):
        page = await self._get_page()
        await page.wait_for_selector(selector, timeout=timeout)

    async def evaluate(self, js: str) -> Any:
        """Run arbitrary JavaScript on the page."""
        page = await self._get_page()
        return await page.evaluate(js)

    async def get_page_info(self) -> dict[str, Any]:
        """Get structured info about the current page for LLM reasoning."""
        page = await self._get_page()
        url = page.url
        title = await page.title()
        text = await page.inner_text("body")
        # Get all interactive elements
        forms = await page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input, textarea, select, button, a'));
            return inputs.map(el => ({
                tag: el.tagName.toLowerCase(),
                type: el.type || '',
                name: el.name || '',
                id: el.id || '',
                placeholder: el.placeholder || '',
                text: el.innerText?.substring(0, 100) || '',
                href: el.href || '',
                selector: el.id ? '#' + el.id : (el.name ? `[name="${el.name}"]` : '')
            })).filter(el => el.selector);
        }""")
        return {
            "url": url,
            "title": title,
            "text": text[:3000],  # Truncate for LLM context
            "interactive_elements": forms[:50],  # Cap elements
        }

    async def fill_form(self, fields: dict[str, str]):
        """Fill a form with multiple fields at once."""
        page = await self._get_page()
        for selector, value in fields.items():
            await page.fill(selector, value)
            logger.info(f"Filled {selector}")

    async def submit_form(self, selector: str = "form"):
        """Submit a form."""
        page = await self._get_page()
        await page.evaluate(f'document.querySelector("{selector}").submit()')

    def _current_proxy_url(self) -> str | None:
        """Return the proxy URL the current browser context was launched with."""
        return getattr(self, "_active_proxy_url", None)

    async def _recreate_context_with_proxy(self, proxy_url: str) -> None:
        """Tear down the current browser context and create a new one with a
        different proxy.  The browser instance itself is kept alive.

        This is the mechanism that makes mid-session proxy switching
        transparent to the target platform.
        """
        # Close existing context (pages and cookies go away — intentional)
        if self._context:
            await self._context.close()
            self._context = None

        # Close old browser — Playwright requires proxy at browser level
        if self._browser:
            await self._browser.close()
            self._browser = None

        if not self._playwright:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()

        launch_args = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=WebGLDraftExtensions",
                "--disable-webgl",
                "--disable-webgl2",
            ],
        }
        if proxy_url:
            launch_args["proxy"] = {"server": proxy_url}
        self._browser = await self._playwright.chromium.launch(**launch_args)

        fp = self._anonymizer.get_browser_fingerprint()
        self._context = await self._browser.new_context(
            viewport=fp["viewport"],
            user_agent=fp["user_agent"],
            timezone_id=fp["timezone_id"],
            locale=fp["locale"],
            color_scheme=fp["color_scheme"],
            device_scale_factor=fp["device_scale_factor"],
            permissions=[],
        )

        # Re-inject anti-fingerprinting and anti-headless-detection scripts
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'mediaDevices', { get: () => undefined });
            window.RTCPeerConnection = undefined;
            window.RTCSessionDescription = undefined;
            window.RTCIceCandidate = undefined;
            window.webkitRTCPeerConnection = undefined;
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 4 });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(param) {
                if (param === 37445) return 'Intel Inc.';
                if (param === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.call(this, param);
            };
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {
                const ctx = this.getContext('2d');
                if (ctx) {
                    const imgData = ctx.getImageData(0, 0, this.width, this.height);
                    for (let i = 0; i < imgData.data.length; i += 4) {
                        imgData.data[i] ^= 1;
                    }
                    ctx.putImageData(imgData, 0, 0);
                }
                return origToDataURL.call(this, type);
            };
        """)

        self._active_proxy_url = proxy_url
        logger.info(f"Browser context recreated with proxy: {proxy_url}")

    async def _get_page(self):
        if not self._context:
            await self.start()
        pages = self._context.pages
        if not pages:
            return await self._context.new_page()
        return pages[-1]

    async def new_page(self) -> Any:
        if not self._context:
            await self.start()
        return await self._context.new_page()


def run_browser_task(config: Config, coro):
    """Helper to run an async browser task from sync code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    return asyncio.run(coro)
