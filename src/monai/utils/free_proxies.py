"""Auto-scrape free public proxy lists as a fallback tier.

When Tor is blocked and no residential/datacenter proxy is configured,
the system scrapes free proxy lists to maintain anonymity without any
user configuration.  Proxies are validated before use and rotated
automatically.

Sources:
- free-proxy-list.net (HTTPS proxies)
- sslproxies.org (SSL proxies)
- geonode.com API (free tier, no key required)

These are NOT as reliable as paid proxies, but they're infinitely better
than being completely blocked.  The system validates each proxy before
adding it to the pool and tracks success rates to prefer working ones.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Sources of free proxy lists
FREE_PROXY_SOURCES = [
    {
        "name": "geonode",
        # Include HTTP/HTTPS too — SOCKS-only filter often returns empty
        "url": "https://proxylist.geonode.com/api/proxy-list?limit=50&page=1&sort_by=lastChecked&sort_type=desc&anonymityLevel=elite%2Canonymous",
        "parser": "geonode_json",
    },
    {
        # proxyscrape provides a plain-text API — no JS rendering needed
        "name": "proxyscrape-socks5",
        "url": "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all&ssl=all&anonymity=all",
        "parser": "plain_text_socks5",
    },
    {
        "name": "proxyscrape-http",
        "url": "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=yes&anonymity=elite",
        "parser": "plain_text_http",
    },
    {
        # proxifly GitHub maintains auto-updated proxy lists
        "name": "proxifly-socks5",
        "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
        "parser": "plain_text_socks5",
    },
    {
        "name": "free-proxy-list",
        "url": "https://free-proxy-list.net/",
        "parser": "html_table",
    },
    {
        "name": "sslproxies",
        "url": "https://www.sslproxies.org/",
        "parser": "html_table",
    },
]

# How often to refresh the proxy pool (seconds)
REFRESH_INTERVAL = 1800  # 30 minutes
# Minimum proxies to keep in pool before triggering refresh
MIN_POOL_SIZE = 5
# Validation timeout
VALIDATE_TIMEOUT = 10
# Max proxies to keep
MAX_POOL_SIZE = 30


class FreeProxyPool:
    """Maintains a pool of validated free proxies.

    Thread-safe.  Lazily fetches proxies on first use and refreshes
    periodically.  Tracks success/failure per proxy to prefer reliable ones.
    """

    def __init__(self, db=None):
        self._db = db
        self._lock = threading.Lock()
        self._proxies: list[dict[str, Any]] = []
        # proxy_url → {successes: int, failures: int, last_used: float}
        self._stats: dict[str, dict[str, Any]] = {}
        self._last_refresh: float = 0
        self._refreshing = False
        self._initialized = False

        self._init_db()

    def _init_db(self) -> None:
        """Create free_proxies table if DB is available."""
        if not self._db:
            return
        try:
            with self._db.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS free_proxies (
                        url TEXT PRIMARY KEY,
                        protocol TEXT NOT NULL,
                        country TEXT,
                        successes INTEGER DEFAULT 0,
                        failures INTEGER DEFAULT 0,
                        last_validated REAL,
                        last_used REAL
                    );
                """)
        except Exception as e:
            logger.debug(f"Failed to init free_proxies table: {e}")

    def get_proxy(self) -> str | None:
        """Get a working free proxy URL.  Returns None if pool is empty.

        Prefers proxies with better success rates.  Triggers background
        refresh if pool is running low.
        """
        with self._lock:
            if not self._initialized or (
                time.time() - self._last_refresh > REFRESH_INTERVAL
            ):
                self._trigger_refresh()

            if not self._proxies:
                return None

            # Sort by success rate (successes - failures), prefer less-used
            scored = []
            for p in self._proxies:
                url = p["url"]
                stats = self._stats.get(url, {"successes": 0, "failures": 0})
                score = stats["successes"] - stats["failures"] * 2
                scored.append((score, p))

            scored.sort(key=lambda x: x[0], reverse=True)

            # Pick from top 5 with some randomness to spread load
            top = scored[:min(5, len(scored))]
            chosen = random.choice(top)[1]
            return chosen["url"]

    def report_success(self, proxy_url: str) -> None:
        """Report that a proxy worked."""
        with self._lock:
            if proxy_url not in self._stats:
                self._stats[proxy_url] = {"successes": 0, "failures": 0}
            self._stats[proxy_url]["successes"] += 1
            self._stats[proxy_url]["last_used"] = time.time()
        self._persist_stats(proxy_url)

    def report_failure(self, proxy_url: str) -> None:
        """Report that a proxy failed.  Remove if too many failures."""
        with self._lock:
            if proxy_url not in self._stats:
                self._stats[proxy_url] = {"successes": 0, "failures": 0}
            self._stats[proxy_url]["failures"] += 1

            # Remove proxy if failure rate is too high
            stats = self._stats[proxy_url]
            total = stats["successes"] + stats["failures"]
            if total >= 3 and stats["failures"] / total > 0.7:
                self._proxies = [
                    p for p in self._proxies if p["url"] != proxy_url
                ]
                logger.info(f"Removed unreliable free proxy: {proxy_url}")

        self._persist_stats(proxy_url)

        # Trigger refresh if pool is getting small
        with self._lock:
            if len(self._proxies) < MIN_POOL_SIZE:
                self._trigger_refresh()

    def pool_size(self) -> int:
        """Number of proxies currently in the pool."""
        with self._lock:
            return len(self._proxies)

    def _trigger_refresh(self) -> None:
        """Trigger a proxy list refresh (runs in background thread)."""
        if self._refreshing:
            return
        self._refreshing = True
        thread = threading.Thread(target=self._refresh_pool, daemon=True)
        thread.start()

    def _refresh_pool(self) -> None:
        """Scrape free proxy lists and validate proxies."""
        try:
            candidates: list[dict[str, Any]] = []

            for source in FREE_PROXY_SOURCES:
                try:
                    scraped = self._scrape_source(source)
                    candidates.extend(scraped)
                    logger.info(
                        f"Scraped {len(scraped)} proxies from {source['name']}"
                    )
                except Exception as e:
                    logger.debug(
                        f"Failed to scrape {source['name']}: {e}"
                    )

            if not candidates:
                logger.warning("No free proxy candidates found from any source")
                return

            # Deduplicate
            seen: set[str] = set()
            unique: list[dict[str, Any]] = []
            for c in candidates:
                if c["url"] not in seen:
                    seen.add(c["url"])
                    unique.append(c)

            # Validate top candidates (limit to avoid slow startup)
            validated = self._validate_batch(unique[:50])

            with self._lock:
                # Merge with existing pool, preferring validated
                existing_urls = {p["url"] for p in self._proxies}
                for v in validated:
                    if v["url"] not in existing_urls:
                        self._proxies.append(v)

                # Cap pool size
                if len(self._proxies) > MAX_POOL_SIZE:
                    # Keep the ones with best stats
                    self._proxies.sort(
                        key=lambda p: self._stats.get(
                            p["url"], {}
                        ).get("successes", 0),
                        reverse=True,
                    )
                    self._proxies = self._proxies[:MAX_POOL_SIZE]

                self._last_refresh = time.time()
                self._initialized = True

            logger.info(
                f"Free proxy pool refreshed: {len(validated)} validated, "
                f"{len(self._proxies)} total in pool"
            )
        except Exception as e:
            logger.error(f"Free proxy pool refresh failed: {e}")
        finally:
            self._refreshing = False

    def _scrape_source(self, source: dict) -> list[dict[str, Any]]:
        """Scrape proxies from a single source."""
        # Use a direct connection for scraping proxy lists
        # (we can't use Tor to scrape proxy lists if Tor is blocked)
        client = httpx.Client(
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
                )
            },
            follow_redirects=True,
        )
        try:
            resp = client.get(source["url"])
            resp.raise_for_status()

            if source["parser"] == "geonode_json":
                return self._parse_geonode(resp.json())
            elif source["parser"] == "html_table":
                return self._parse_html_table(resp.text)
            elif source["parser"] == "plain_text_socks5":
                return self._parse_plain_text(resp.text, "socks5")
            elif source["parser"] == "plain_text_http":
                return self._parse_plain_text(resp.text, "http")
            return []
        finally:
            client.close()

    @staticmethod
    def _parse_geonode(data: dict) -> list[dict[str, Any]]:
        """Parse Geonode API response."""
        proxies = []
        for item in data.get("data", []):
            ip = item.get("ip", "")
            port = item.get("port", "")
            protocols = item.get("protocols", [])
            country = item.get("country", "")

            if not ip or not port:
                continue

            # Prefer SOCKS5, then SOCKS4, then HTTPS
            if "socks5" in protocols:
                url = f"socks5://{ip}:{port}"
            elif "socks4" in protocols:
                url = f"socks4://{ip}:{port}"
            elif "https" in protocols:
                url = f"http://{ip}:{port}"
            else:
                url = f"http://{ip}:{port}"

            proxies.append({
                "url": url,
                "protocol": protocols[0] if protocols else "http",
                "country": country,
            })
        return proxies

    @staticmethod
    def _parse_html_table(html: str) -> list[dict[str, Any]]:
        """Parse free-proxy-list.net style HTML tables.

        Tries two patterns: the classic 7-column format, and a more
        relaxed pattern that just extracts IP:Port from table rows
        (handles JS-rendered or restructured tables).
        """
        proxies = []
        # Pattern 1: Classic 7-column format
        rows = re.findall(
            r"<tr><td>(\d+\.\d+\.\d+\.\d+)</td><td>(\d+)</td>"
            r"<td>(\w+)</td><td>(\w+)</td>"
            r"<td>(.*?)</td><td>(.*?)</td>"
            r"<td>(yes|no)</td>",
            html,
            re.IGNORECASE,
        )
        for row in rows:
            ip, port = row[0], row[1]
            country = row[2]
            https = row[6].lower() == "yes"
            if https:
                proxies.append({
                    "url": f"http://{ip}:{port}",
                    "protocol": "https",
                    "country": country,
                })

        # Pattern 2: Relaxed fallback — extract any IP:Port from <td> tags
        if not proxies:
            rows = re.findall(
                r"<td[^>]*>(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})</td>"
                r"\s*<td[^>]*>(\d{2,5})</td>",
                html,
            )
            for ip, port in rows:
                proxies.append({
                    "url": f"http://{ip}:{port}",
                    "protocol": "http",
                    "country": "",
                })
        return proxies

    @staticmethod
    def _parse_plain_text(text: str, protocol: str) -> list[dict[str, Any]]:
        """Parse plain-text proxy lists (IP:Port per line)."""
        proxies = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{2,5})", line)
            if match:
                ip, port = match.group(1), match.group(2)
                proxies.append({
                    "url": f"{protocol}://{ip}:{port}",
                    "protocol": protocol,
                    "country": "",
                })
        return proxies

    def _validate_batch(
        self, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate proxies by testing connectivity.  Returns working ones."""
        validated = []
        # Test in parallel using threads (but limit concurrency)
        import concurrent.futures

        def _test(proxy: dict) -> dict | None:
            try:
                client = httpx.Client(
                    proxy=proxy["url"],
                    timeout=VALIDATE_TIMEOUT,
                    follow_redirects=True,
                )
                try:
                    resp = client.get("https://httpbin.org/ip")
                    if resp.status_code == 200:
                        return proxy
                finally:
                    client.close()
            except Exception:
                pass
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_test, p): p for p in candidates}
            for future in concurrent.futures.as_completed(futures, timeout=30):
                try:
                    result = future.result(timeout=VALIDATE_TIMEOUT + 2)
                    if result:
                        validated.append(result)
                        if len(validated) >= MAX_POOL_SIZE:
                            break
                except Exception:
                    pass

        return validated

    def _persist_stats(self, proxy_url: str) -> None:
        """Persist proxy stats to DB."""
        if not self._db:
            return
        stats = self._stats.get(proxy_url, {})
        try:
            self._db.execute(
                "INSERT INTO free_proxies (url, protocol, successes, failures, last_used) "
                "VALUES (?, 'auto', ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET "
                "successes = excluded.successes, "
                "failures = excluded.failures, "
                "last_used = excluded.last_used",
                (
                    proxy_url,
                    stats.get("successes", 0),
                    stats.get("failures", 0),
                    stats.get("last_used", time.time()),
                ),
            )
        except Exception:
            pass
