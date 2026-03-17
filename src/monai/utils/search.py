"""Tor-friendly web search — multiple engine fallback.

Provides reliable web search over Tor by trying multiple search engines
that don't block Tor exit nodes. Falls back through engines in order
until one returns results.

Supported engines (in priority order):
  1. DuckDuckGo Lite (lite.duckduckgo.com) — lightest, most Tor-friendly
  2. DuckDuckGo HTML (html.duckduckgo.com) — heavier but more results
  3. SearXNG public instances — meta-search, Tor-friendly by design
  4. Brave Search — often works over Tor
  5. Mojeek — independent index, Tor-friendly

All responses are parsed to extract search result titles, URLs, and snippets.
"""

from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx

logger = logging.getLogger(__name__)

# Search engines ordered by Tor-friendliness
SEARCH_ENGINES = [
    {
        "name": "duckduckgo_lite",
        "url": "https://lite.duckduckgo.com/lite/?q={query}",
        "parser": "_parse_ddg_lite",
    },
    {
        "name": "duckduckgo_html",
        "url": "https://html.duckduckgo.com/html/?q={query}",
        "parser": "_parse_ddg_html",
    },
    {
        "name": "searxng_1",
        "url": "https://search.sapti.me/search?q={query}&format=json",
        "parser": "_parse_searxng_json",
    },
    {
        "name": "searxng_2",
        "url": "https://searx.tiekoetter.com/search?q={query}&format=json",
        "parser": "_parse_searxng_json",
    },
    {
        "name": "mojeek",
        "url": "https://www.mojeek.com/search?q={query}",
        "parser": "_parse_mojeek",
    },
]


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def _parse_ddg_lite(html: str) -> list[dict[str, str]]:
    """Parse DuckDuckGo Lite results."""
    results = []
    # DDG Lite uses simple <a> tags in table rows
    for match in re.finditer(
        r'<a[^>]+href="([^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    ):
        url, title = match.group(1), _strip_html(match.group(2))
        if url and title and not url.startswith("/"):
            results.append({"title": title, "url": url, "snippet": ""})

    # Also try the simpler pattern (DDG Lite format varies)
    if not results:
        for match in re.finditer(
            r'<a[^>]+rel="nofollow"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL,
        ):
            url, title = match.group(1), _strip_html(match.group(2))
            if url and title and url.startswith("http"):
                results.append({"title": title, "url": url, "snippet": ""})

    # Extract snippets from <td> following result links
    snippet_matches = re.findall(
        r'class="result-snippet"[^>]*>(.*?)</td>', html, re.DOTALL
    )
    for i, snippet in enumerate(snippet_matches):
        if i < len(results):
            results[i]["snippet"] = _strip_html(snippet)[:300]

    return results


def _parse_ddg_html(html: str) -> list[dict[str, str]]:
    """Parse DuckDuckGo HTML results."""
    results = []
    # DDG HTML uses result__a class for links
    for match in re.finditer(
        r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    ):
        url, title = match.group(1), _strip_html(match.group(2))
        if url and title:
            results.append({"title": title, "url": url, "snippet": ""})

    # Extract snippets
    snippet_matches = re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL
    )
    for i, snippet in enumerate(snippet_matches):
        if i < len(results):
            results[i]["snippet"] = _strip_html(snippet)[:300]

    return results


def _parse_searxng_json(text: str) -> list[dict[str, str]]:
    """Parse SearXNG JSON response."""
    import json
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []

    results = []
    for r in data.get("results", []):
        url = r.get("url", "")
        title = r.get("title", "")
        if url and title:
            results.append({
                "title": title,
                "url": url,
                "snippet": r.get("content", "")[:300],
            })
    return results


def _parse_mojeek(html: str) -> list[dict[str, str]]:
    """Parse Mojeek search results."""
    results = []
    for match in re.finditer(
        r'<a[^>]+class="ob"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    ):
        url, title = match.group(1), _strip_html(match.group(2))
        if url and title and url.startswith("http"):
            results.append({"title": title, "url": url, "snippet": ""})

    # Snippets in <p class="s">
    snippet_matches = re.findall(r'<p class="s">(.*?)</p>', html, re.DOTALL)
    for i, snippet in enumerate(snippet_matches):
        if i < len(results):
            results[i]["snippet"] = _strip_html(snippet)[:300]

    return results


PARSERS = {
    "_parse_ddg_lite": _parse_ddg_lite,
    "_parse_ddg_html": _parse_ddg_html,
    "_parse_searxng_json": _parse_searxng_json,
    "_parse_mojeek": _parse_mojeek,
}


class TorSearch:
    """Search the web over Tor with multiple engine fallback."""

    def __init__(self, proxy_url: str = "socks5://127.0.0.1:9050"):
        self._proxy_url = proxy_url

    def search(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        """Search the web and return results.

        Tries multiple search engines until one works. Returns a list of
        dicts with keys: title, url, snippet.

        Args:
            query: Search query string
            max_results: Maximum results to return

        Returns:
            List of search results, or empty list if all engines fail.
        """
        encoded_query = quote_plus(query)

        for engine in SEARCH_ENGINES:
            url = engine["url"].format(query=encoded_query)
            parser = PARSERS[engine["parser"]]

            try:
                resp = httpx.get(
                    url,
                    proxy=self._proxy_url,
                    timeout=15,
                    follow_redirects=True,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; rv:128.0) "
                            "Gecko/20100101 Firefox/128.0"
                        ),
                        "Accept": "text/html,application/json",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )

                if resp.status_code == 403:
                    logger.info(
                        f"Search engine {engine['name']} returned 403 — trying next"
                    )
                    continue
                if resp.status_code == 429:
                    logger.info(
                        f"Search engine {engine['name']} rate-limited — trying next"
                    )
                    continue
                if resp.status_code != 200:
                    logger.debug(
                        f"Search engine {engine['name']} returned {resp.status_code}"
                    )
                    continue

                results = parser(resp.text)
                if results:
                    logger.info(
                        f"Search via {engine['name']}: {len(results)} results "
                        f"for '{query[:50]}'"
                    )
                    return results[:max_results]
                else:
                    logger.debug(
                        f"Search engine {engine['name']} returned 200 but "
                        f"parser found 0 results"
                    )

            except Exception as e:
                logger.debug(f"Search engine {engine['name']} failed: {e}")
                continue

        logger.warning(f"All search engines failed for query: {query[:80]}")
        return []
