"""Tor-friendly web search — multiple engine fallback.

Provides reliable web search over Tor by trying multiple search engines
that don't block Tor exit nodes. Falls back through engines in order
until one returns results.

Supported engines (in priority order):
  1. SearXNG public instances (HTML) — meta-search, Tor-friendly by design
  2. DuckDuckGo Lite — lightest DDG interface
  3. DuckDuckGo HTML — heavier but more results
  4. Mojeek — independent index, generally Tor-friendly
  5. SearXNG JSON (POST) — fallback for instances with JSON enabled

All responses are parsed to extract search result titles, URLs, and snippets.
"""

from __future__ import annotations

import json
import logging
import re
from html import unescape
from typing import Any
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

# -- Parsers ---------------------------------------------------------------


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


def _parse_searxng_html(html: str) -> list[dict[str, str]]:
    """Parse SearXNG HTML search results (simple theme).

    SearXNG wraps each result in <article class="result ..."> with:
      - <h3> containing a link (title + URL)
      - <p class="content"> or similar for snippet
    """
    results = []

    # Pattern 1: <article> blocks with <h3><a href=...>title</a></h3>
    # This is the main SearXNG simple theme pattern
    for match in re.finditer(
        r'<article[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</article>',
        html, re.DOTALL,
    ):
        block = match.group(1)
        # Extract title link from <h3>
        link_match = re.search(
            r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            block, re.DOTALL,
        )
        if not link_match:
            continue
        url = link_match.group(1)
        title = _strip_html(link_match.group(2))
        if not url or not title or not url.startswith("http"):
            continue

        # Extract snippet from <p class="content"> or generic <p>
        snippet = ""
        snippet_match = re.search(
            r'<p[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</p>',
            block, re.DOTALL,
        )
        if not snippet_match:
            # Fallback: any <p> that isn't the URL display
            snippet_match = re.search(
                r'<p(?![^>]*class="[^"]*url)[^>]*>(.*?)</p>',
                block, re.DOTALL,
            )
        if snippet_match:
            snippet = _strip_html(snippet_match.group(1))[:300]

        results.append({"title": title, "url": url, "snippet": snippet})

    # Pattern 2: Fallback for older SearXNG themes using <div class="result">
    if not results:
        for match in re.finditer(
            r'<div[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</div>\s*(?=<div|$)',
            html, re.DOTALL,
        ):
            block = match.group(1)
            link_match = re.search(
                r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                block, re.DOTALL,
            )
            if not link_match:
                continue
            url = link_match.group(1)
            title = _strip_html(link_match.group(2))
            if url and title and url.startswith("http"):
                results.append({"title": title, "url": url, "snippet": ""})

    return results


def _parse_mojeek(html: str) -> list[dict[str, str]]:
    """Parse Mojeek search results.

    Mojeek uses several patterns depending on version. Try multiple
    selectors to handle variations.
    """
    results = []

    # Pattern 1: <a class="ob" ...> (older versions)
    for match in re.finditer(
        r'<a[^>]+class="ob"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    ):
        url, title = match.group(1), _strip_html(match.group(2))
        if url and title and url.startswith("http"):
            results.append({"title": title, "url": url, "snippet": ""})

    # Pattern 2: Results in <li class="results-standard"> or similar
    if not results:
        for match in re.finditer(
            r'<li[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</li>',
            html, re.DOTALL,
        ):
            block = match.group(1)
            link_match = re.search(
                r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                block, re.DOTALL,
            )
            if link_match:
                url = link_match.group(1)
                title = _strip_html(link_match.group(2))
                if url and title:
                    results.append({"title": title, "url": url, "snippet": ""})

    # Pattern 3: Generic <h2><a href=...> inside result containers
    if not results:
        for match in re.finditer(
            r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL,
        ):
            url, title = match.group(1), _strip_html(match.group(2))
            if url and title:
                results.append({"title": title, "url": url, "snippet": ""})

    # Extract snippets: try multiple class patterns
    for cls in ("s", "cs", "desc", "snippet"):
        snippet_matches = re.findall(
            rf'<p[^>]*class="[^"]*{cls}[^"]*"[^>]*>(.*?)</p>',
            html, re.DOTALL,
        )
        if snippet_matches:
            for i, snippet in enumerate(snippet_matches):
                if i < len(results):
                    results[i]["snippet"] = _strip_html(snippet)[:300]
            break

    return results


# -- Engine registry -------------------------------------------------------

PARSERS = {
    "_parse_ddg_lite": _parse_ddg_lite,
    "_parse_ddg_html": _parse_ddg_html,
    "_parse_searxng_json": _parse_searxng_json,
    "_parse_searxng_html": _parse_searxng_html,
    "_parse_mojeek": _parse_mojeek,
}

# SearXNG instances that are known to work over Tor.
# HTML endpoints are tried first (JSON is often disabled on public instances).
_SEARXNG_INSTANCES = [
    "https://search.sapti.me",
    "https://searx.tiekoetter.com",
    "https://searx.be",
    "https://search.ononoki.org",
    "https://paulgo.io",
    "https://opnxng.com",
]

# Search engines ordered by reliability over Tor.
# SearXNG HTML first (most reliable), DDG next, Mojeek last.
SEARCH_ENGINES: list[dict[str, str]] = []

# Add SearXNG HTML instances first (most likely to work over Tor)
for _inst in _SEARXNG_INSTANCES:
    _name = _inst.split("//")[1].replace(".", "_")
    SEARCH_ENGINES.append({
        "name": f"searxng_html_{_name}",
        "url": f"{_inst}/search?q={{query}}",
        "parser": "_parse_searxng_html",
        "method": "GET",
    })

# DuckDuckGo (sometimes works, sometimes 403)
SEARCH_ENGINES.extend([
    {
        "name": "duckduckgo_lite",
        "url": "https://lite.duckduckgo.com/lite/?q={query}",
        "parser": "_parse_ddg_lite",
        "method": "GET",
    },
    {
        "name": "duckduckgo_html",
        "url": "https://html.duckduckgo.com/html/?q={query}",
        "parser": "_parse_ddg_html",
        "method": "GET",
    },
])

# Mojeek (independent index)
SEARCH_ENGINES.append({
    "name": "mojeek",
    "url": "https://www.mojeek.com/search?q={query}",
    "parser": "_parse_mojeek",
    "method": "GET",
})

# SearXNG JSON via POST as last resort (works on some instances)
for _inst in _SEARXNG_INSTANCES[:3]:
    _name = _inst.split("//")[1].replace(".", "_")
    SEARCH_ENGINES.append({
        "name": f"searxng_json_{_name}",
        "url": f"{_inst}/search",
        "parser": "_parse_searxng_json",
        "method": "POST",
    })


def _is_homepage_redirect(url: str, response_url: str) -> bool:
    """Detect if we were redirected to the homepage (no results)."""
    # If the final URL lost the query parameters, it's a redirect to homepage
    if "?q=" in url and "?q=" not in str(response_url):
        return True
    # If we asked for /search but ended up at /
    if "/search" in url and str(response_url).rstrip("/").endswith(
        url.split("/search")[0].rstrip("/")
    ):
        return True
    return False


# -- TorSearch class --------------------------------------------------------


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
            method = engine.get("method", "GET")

            try:
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; rv:128.0) "
                        "Gecko/20100101 Firefox/128.0"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                }

                if method == "POST":
                    resp = httpx.post(
                        url,
                        data={"q": query, "format": "json"},
                        proxy=self._proxy_url,
                        timeout=15,
                        follow_redirects=True,
                        headers=headers,
                    )
                else:
                    resp = httpx.get(
                        url,
                        proxy=self._proxy_url,
                        timeout=15,
                        follow_redirects=True,
                        headers=headers,
                    )

                if resp.status_code in (403, 429):
                    logger.info(
                        f"Search engine {engine['name']} returned "
                        f"{resp.status_code} — trying next"
                    )
                    continue

                if resp.status_code != 200:
                    logger.debug(
                        f"Search engine {engine['name']} returned "
                        f"{resp.status_code}"
                    )
                    continue

                # Detect redirect to homepage (SearXNG instances do this
                # when they don't support the requested format)
                if _is_homepage_redirect(url, resp.url):
                    logger.info(
                        f"Search engine {engine['name']} redirected to "
                        f"homepage — trying next"
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

            except httpx.TimeoutException:
                logger.debug(
                    f"Search engine {engine['name']} timed out"
                )
                continue
            except Exception as e:
                logger.debug(f"Search engine {engine['name']} failed: {e}")
                continue

        logger.warning(f"All search engines failed for query: {query[:80]}")
        return []
