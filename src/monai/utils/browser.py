"""Browser automation layer — gives monAI eyes and hands on the web.

Uses Playwright for full browser control: navigate, click, type, screenshot,
extract content. This is how monAI registers on platforms, manages accounts,
and interacts with any website autonomously.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from monai.config import Config

logger = logging.getLogger(__name__)


class Browser:
    """Autonomous browser that monAI uses to interact with the web."""

    def __init__(self, config: Config, headless: bool = True):
        self.config = config
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self.screenshots_dir = config.data_dir / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    async def start(self):
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        logger.info("Browser started")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    async def navigate(self, url: str, wait_for: str = "domcontentloaded") -> str:
        page = await self._get_page()
        await page.goto(url, wait_until=wait_for)
        logger.info(f"Navigated to {url}")
        return await page.content()

    async def screenshot(self, name: str = "page") -> Path:
        page = await self._get_page()
        path = self.screenshots_dir / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
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
    return asyncio.get_event_loop().run_until_complete(coro)
