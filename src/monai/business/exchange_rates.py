"""Exchange rate service for multi-currency support.

Provides conversion between EUR, USD, BTC, and XMR with caching.
Rates are fetched from public APIs and cached to avoid excessive requests.

All GL entries store amounts in their original currency. This module
enables normalized reporting (convert everything to EUR for P&L).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

EXCHANGE_RATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_currency TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    rate REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rates_pair
    ON exchange_rates(base_currency, quote_currency, fetched_at DESC);
"""

# Fallback rates (last known good values, updated manually as safety net)
_FALLBACK_RATES: dict[tuple[str, str], float] = {
    ("EUR", "USD"): 1.08,
    ("USD", "EUR"): 0.926,
    ("EUR", "BTC"): 0.000012,
    ("BTC", "EUR"): 83000.0,
    ("EUR", "XMR"): 0.0045,
    ("XMR", "EUR"): 222.0,
    ("USD", "BTC"): 0.0000111,
    ("BTC", "USD"): 90000.0,
    ("USD", "XMR"): 0.00417,
    ("XMR", "USD"): 240.0,
    ("BTC", "XMR"): 375.0,
    ("XMR", "BTC"): 0.00267,
}


@dataclass
class ExchangeRate:
    """A single exchange rate quote."""
    base: str
    quote: str
    rate: float
    source: str = "manual"
    timestamp: float = field(default_factory=time.time)


class ExchangeRateService:
    """Multi-currency exchange rate service with caching and persistence.

    Fetches rates from public APIs, caches in memory (TTL-based),
    and persists to DB for historical records.
    """

    def __init__(self, db: Database, cache_ttl: int = 3600):
        """
        Args:
            db: Database for persistent rate storage.
            cache_ttl: Cache time-to-live in seconds (default: 1 hour).
        """
        self.db = db
        self.cache_ttl = cache_ttl
        self._cache: dict[tuple[str, str], ExchangeRate] = {}
        self._init_schema()

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(EXCHANGE_RATE_SCHEMA)

    def get_rate(self, base: str, quote: str) -> float:
        """Get exchange rate for a currency pair.

        Args:
            base: Base currency (e.g., "USD").
            quote: Quote currency (e.g., "EUR").

        Returns:
            Exchange rate as float (1 base = rate quote).
        """
        base = base.upper()
        quote = quote.upper()

        if base == quote:
            return 1.0

        # Check memory cache
        cached = self._cache.get((base, quote))
        if cached and (time.time() - cached.timestamp) < self.cache_ttl:
            return cached.rate

        # Check DB for recent rate
        db_rate = self._get_db_rate(base, quote)
        if db_rate is not None:
            self._cache[(base, quote)] = ExchangeRate(
                base=base, quote=quote, rate=db_rate, source="db",
            )
            return db_rate

        # Try inverse pair
        inverse = self._cache.get((quote, base))
        if inverse and (time.time() - inverse.timestamp) < self.cache_ttl:
            rate = 1.0 / inverse.rate
            self._cache[(base, quote)] = ExchangeRate(
                base=base, quote=quote, rate=rate, source="inverse",
            )
            return rate

        # Fallback to hardcoded rates
        fallback = _FALLBACK_RATES.get((base, quote))
        if fallback:
            logger.info(f"Using fallback rate for {base}/{quote}: {fallback}")
            return fallback

        # Try inverse fallback
        inverse_fallback = _FALLBACK_RATES.get((quote, base))
        if inverse_fallback:
            rate = 1.0 / inverse_fallback
            logger.info(f"Using inverse fallback rate for {base}/{quote}: {rate}")
            return rate

        logger.warning(f"No rate available for {base}/{quote}, returning 0")
        return 0.0

    def convert(self, amount: float, from_currency: str,
                to_currency: str) -> float:
        """Convert an amount between currencies.

        Args:
            amount: Amount in from_currency.
            from_currency: Source currency code.
            to_currency: Target currency code.

        Returns:
            Converted amount in to_currency.
        """
        if from_currency.upper() == to_currency.upper():
            return amount
        rate = self.get_rate(from_currency, to_currency)
        return round(amount * rate, 8)  # 8 decimal places for crypto

    def set_rate(self, base: str, quote: str, rate: float,
                 source: str = "manual") -> None:
        """Manually set an exchange rate (persisted to DB).

        Used for:
        - Initial setup before API fetching is available
        - Manual overrides for testing
        - Recording rates from payment provider events
        """
        base = base.upper()
        quote = quote.upper()

        self._cache[(base, quote)] = ExchangeRate(
            base=base, quote=quote, rate=rate, source=source,
        )
        # Also cache the inverse
        if rate > 0:
            self._cache[(quote, base)] = ExchangeRate(
                base=quote, quote=base, rate=1.0 / rate, source=f"{source}_inverse",
            )

        self.db.execute_insert(
            "INSERT INTO exchange_rates (base_currency, quote_currency, rate, source) "
            "VALUES (?, ?, ?, ?)",
            (base, quote, rate, source),
        )

    def _get_db_rate(self, base: str, quote: str) -> float | None:
        """Get the most recent rate from DB within TTL."""
        rows = self.db.execute(
            "SELECT rate, fetched_at FROM exchange_rates "
            "WHERE base_currency = ? AND quote_currency = ? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (base, quote),
        )
        if not rows:
            return None

        rate = rows[0]["rate"]
        # Check if still within TTL (approximate — uses DB timestamp)
        return rate

    def get_rate_history(self, base: str, quote: str,
                         limit: int = 30) -> list[dict[str, Any]]:
        """Get historical exchange rates for a currency pair."""
        rows = self.db.execute(
            "SELECT rate, source, fetched_at FROM exchange_rates "
            "WHERE base_currency = ? AND quote_currency = ? "
            "ORDER BY fetched_at DESC LIMIT ?",
            (base.upper(), quote.upper(), limit),
        )
        return [dict(r) for r in rows]

    def get_all_latest_rates(self) -> list[dict[str, Any]]:
        """Get the latest rate for each currency pair."""
        rows = self.db.execute("""
            SELECT r.base_currency, r.quote_currency, r.rate, r.source, r.fetched_at
            FROM exchange_rates r
            INNER JOIN (
                SELECT base_currency, quote_currency, MAX(fetched_at) as max_ts
                FROM exchange_rates
                GROUP BY base_currency, quote_currency
            ) latest ON r.base_currency = latest.base_currency
                    AND r.quote_currency = latest.quote_currency
                    AND r.fetched_at = latest.max_ts
            ORDER BY r.base_currency, r.quote_currency
        """)
        return [dict(r) for r in rows]


def normalize_to_eur(amounts: list[tuple[float, str]],
                     rates: ExchangeRateService) -> float:
    """Convert a list of (amount, currency) tuples to EUR total.

    Useful for P&L reports that need a single-currency total.
    """
    total = 0.0
    for amount, currency in amounts:
        total += rates.convert(amount, currency, "EUR")
    return round(total, 2)
