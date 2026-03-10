"""Base platform integration with per-agent connections.

Every agent that talks to an external platform gets its own connection instance,
with independent rate limiting, error handling, and cost tracking.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from monai.config import Config
from monai.db.database import Database
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)

INTEGRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    api_key_ref TEXT,               -- Reference to encrypted config key (not the key itself)
    status TEXT DEFAULT 'active',    -- active, rate_limited, error, disabled
    requests_today INTEGER DEFAULT 0,
    requests_total INTEGER DEFAULT 0,
    last_request_at TIMESTAMP,
    last_error TEXT,
    cost_total REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, agent_name)
);

CREATE TABLE IF NOT EXISTS platform_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    method TEXT DEFAULT 'GET',
    status_code INTEGER,
    cost REAL DEFAULT 0.0,
    duration_ms INTEGER,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass
class RateLimitConfig:
    """Rate limiting configuration per platform."""
    requests_per_minute: int = 60
    requests_per_day: int = 1000
    retry_after_seconds: int = 60
    max_retries: int = 3
    backoff_factor: float = 2.0


@dataclass
class PlatformConnection:
    """A single agent's connection to a platform."""
    platform: str
    agent_name: str
    base_url: str
    api_key: str = ""
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    _request_times: list[float] = field(default_factory=list)
    _client: httpx.Client | None = field(default=None, repr=False)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            anonymizer = get_anonymizer()
            headers = {"User-Agent": anonymizer.get_user_agent()}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            proxy_url = anonymizer.get_proxy_url()
            client_kwargs: dict = {
                "base_url": self.base_url,
                "headers": headers,
                "timeout": 30,
                "follow_redirects": True,
            }
            if proxy_url:
                client_kwargs["proxy"] = proxy_url
            self._client = httpx.Client(**client_kwargs)
        return self._client

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def _check_rate_limit(self):
        """Check if we're within rate limits."""
        now = time.time()
        # Clean old entries
        minute_ago = now - 60
        self._request_times = [t for t in self._request_times if t > minute_ago]
        if len(self._request_times) >= self.rate_limit.requests_per_minute:
            wait = self._request_times[0] - minute_ago
            raise RateLimitError(
                f"Rate limit hit for {self.platform}/{self.agent_name}. "
                f"Wait {wait:.1f}s.",
                retry_after=wait,
            )

    def request(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        """Make a rate-limited request with retries."""
        self._check_rate_limit()

        last_error = None
        for attempt in range(1, self.rate_limit.max_retries + 1):
            try:
                start = time.time()
                response = self.client.request(method, endpoint, **kwargs)
                duration = int((time.time() - start) * 1000)
                self._request_times.append(time.time())

                if response.status_code == 429:
                    retry_after = int(response.headers.get(
                        "Retry-After", self.rate_limit.retry_after_seconds
                    ))
                    raise RateLimitError(
                        f"429 from {self.platform}", retry_after=retry_after
                    )

                response.raise_for_status()
                return response

            except RateLimitError as e:
                last_error = e
                if attempt < self.rate_limit.max_retries:
                    time.sleep(e.retry_after)
            except httpx.HTTPStatusError as e:
                last_error = e
                if attempt < self.rate_limit.max_retries and e.response.status_code >= 500:
                    time.sleep(self.rate_limit.backoff_factor ** attempt)
                else:
                    raise
            except httpx.RequestError as e:
                last_error = e
                if attempt < self.rate_limit.max_retries:
                    time.sleep(self.rate_limit.backoff_factor ** attempt)
                else:
                    raise

        raise last_error or Exception(f"Request failed after {self.rate_limit.max_retries} retries")

    def get(self, endpoint: str, **kwargs) -> httpx.Response:
        return self.request("GET", endpoint, **kwargs)

    def post(self, endpoint: str, **kwargs) -> httpx.Response:
        return self.request("POST", endpoint, **kwargs)

    def put(self, endpoint: str, **kwargs) -> httpx.Response:
        return self.request("PUT", endpoint, **kwargs)

    def delete(self, endpoint: str, **kwargs) -> httpx.Response:
        return self.request("DELETE", endpoint, **kwargs)


class RateLimitError(Exception):
    """Rate limit exceeded."""
    def __init__(self, message: str, retry_after: float = 60):
        super().__init__(message)
        self.retry_after = retry_after


class PlatformIntegration(ABC):
    """Base class for platform integrations.

    Each subclass represents a specific platform (Gumroad, Substack, etc.)
    and provides per-agent connection management.
    """

    platform_name: str = ""
    base_url: str = ""
    default_rate_limit: RateLimitConfig = RateLimitConfig()

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._connections: dict[str, PlatformConnection] = {}
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript(INTEGRATION_SCHEMA)

    def get_connection(self, agent_name: str, api_key: str = "") -> PlatformConnection:
        """Get or create a connection for a specific agent."""
        if agent_name not in self._connections:
            conn = PlatformConnection(
                platform=self.platform_name,
                agent_name=agent_name,
                base_url=self.base_url,
                api_key=api_key,
                rate_limit=self.default_rate_limit,
            )
            self._connections[agent_name] = conn
            self._register_connection(agent_name)
        return self._connections[agent_name]

    def _register_connection(self, agent_name: str):
        """Register the connection in the database."""
        existing = self.db.execute(
            "SELECT id FROM platform_connections WHERE platform = ? AND agent_name = ?",
            (self.platform_name, agent_name),
        )
        if not existing:
            self.db.execute_insert(
                "INSERT INTO platform_connections (platform, agent_name) VALUES (?, ?)",
                (self.platform_name, agent_name),
            )

    def log_request(self, agent_name: str, endpoint: str, method: str = "GET",
                    status_code: int = 200, cost: float = 0.0,
                    duration_ms: int = 0, error: str = ""):
        """Log a platform API request."""
        self.db.execute_insert(
            "INSERT INTO platform_requests "
            "(platform, agent_name, endpoint, method, status_code, cost, duration_ms, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.platform_name, agent_name, endpoint, method,
             status_code, cost, duration_ms, error),
        )
        # Update connection stats
        self.db.execute(
            "UPDATE platform_connections SET requests_total = requests_total + 1, "
            "requests_today = requests_today + 1, last_request_at = ?, "
            "cost_total = cost_total + ?, updated_at = ? WHERE platform = ? AND agent_name = ?",
            (datetime.now().isoformat(), cost, datetime.now().isoformat(),
             self.platform_name, agent_name),
        )

    def get_stats(self, agent_name: str = "") -> dict[str, Any]:
        """Get usage statistics for this platform."""
        if agent_name:
            rows = self.db.execute(
                "SELECT * FROM platform_connections WHERE platform = ? AND agent_name = ?",
                (self.platform_name, agent_name),
            )
        else:
            rows = self.db.execute(
                "SELECT * FROM platform_connections WHERE platform = ?",
                (self.platform_name,),
            )
        return [dict(r) for r in rows]

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Check if the platform API is accessible."""
        ...

    def close_all(self):
        """Close all connections."""
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()
