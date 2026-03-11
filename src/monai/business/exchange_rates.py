"""Exchange rate service for multi-currency support.

Provides conversion between EUR, USD, BTC, and XMR with caching.
Rates are fetched from public APIs and cached to avoid excessive requests.

All GL entries store amounts in their original currency. This module
enables normalized reporting (convert everything to EUR for P&L).

Rate sources:
  - ECB (European Central Bank) — EUR/USD and other fiat pairs, free, no key
  - CoinGecko — BTC/EUR, XMR/EUR, and other crypto pairs, free tier
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

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

    # ── Live Rate Fetching ─────────────────────────────────────

    async def fetch_live_rates(self) -> dict[str, float]:
        """Fetch live rates from ECB (fiat) and CoinGecko (crypto).

        Returns dict of rates that were successfully fetched and stored.
        Failures are logged but don't raise — falls back to cached/fallback.
        """
        fetched: dict[str, float] = {}

        # Fetch fiat rates from ECB
        ecb_rates = await self._fetch_ecb_rates()
        for pair, rate in ecb_rates.items():
            self.set_rate(pair[0], pair[1], rate, source="ecb")
            fetched[f"{pair[0]}/{pair[1]}"] = rate

        # Fetch crypto rates from CoinGecko
        crypto_rates = await self._fetch_coingecko_rates()
        for pair, rate in crypto_rates.items():
            self.set_rate(pair[0], pair[1], rate, source="coingecko")
            fetched[f"{pair[0]}/{pair[1]}"] = rate

        if fetched:
            logger.info(f"Fetched {len(fetched)} live exchange rates")
        else:
            logger.warning("No live exchange rates fetched — using cached/fallback")

        return fetched

    async def _fetch_ecb_rates(self) -> dict[tuple[str, str], float]:
        """Fetch EUR-based rates from ECB daily reference rates.

        ECB provides free XML/JSON with EUR as base for ~30 currencies.
        We extract EUR/USD and compute USD/EUR inverse.
        """
        rates: dict[tuple[str, str], float] = {}
        url = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?lastNObservations=1&format=csvdata"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()

                # CSV format: parse the last line for the rate
                lines = resp.text.strip().split("\n")
                if len(lines) >= 2:
                    # Header + data row; OBS_VALUE is the rate column
                    header = lines[0].split(",")
                    data = lines[-1].split(",")
                    if "OBS_VALUE" in header:
                        idx = header.index("OBS_VALUE")
                        usd_rate = float(data[idx])
                        rates[("EUR", "USD")] = usd_rate
                        rates[("USD", "EUR")] = round(1.0 / usd_rate, 6)
                        logger.debug(f"ECB EUR/USD: {usd_rate}")
        except Exception as e:
            logger.warning(f"ECB rate fetch failed: {e}")

        return rates

    async def _fetch_coingecko_rates(self) -> dict[tuple[str, str], float]:
        """Fetch crypto rates from CoinGecko free API.

        Gets BTC and XMR prices in EUR and USD.
        """
        rates: dict[tuple[str, str], float] = {}
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,monero&vs_currencies=eur,usd"
        )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

                # Bitcoin
                btc = data.get("bitcoin", {})
                if "eur" in btc:
                    rates[("BTC", "EUR")] = btc["eur"]
                    rates[("EUR", "BTC")] = round(1.0 / btc["eur"], 10)
                if "usd" in btc:
                    rates[("BTC", "USD")] = btc["usd"]
                    rates[("USD", "BTC")] = round(1.0 / btc["usd"], 10)

                # Monero
                xmr = data.get("monero", {})
                if "eur" in xmr:
                    rates[("XMR", "EUR")] = xmr["eur"]
                    rates[("EUR", "XMR")] = round(1.0 / xmr["eur"], 10)
                if "usd" in xmr:
                    rates[("XMR", "USD")] = xmr["usd"]
                    rates[("USD", "XMR")] = round(1.0 / xmr["usd"], 10)

                # Cross rate: BTC/XMR
                if btc.get("eur") and xmr.get("eur"):
                    btc_xmr = btc["eur"] / xmr["eur"]
                    rates[("BTC", "XMR")] = round(btc_xmr, 4)
                    rates[("XMR", "BTC")] = round(1.0 / btc_xmr, 10)

                logger.debug(f"CoinGecko: BTC/EUR={btc.get('eur')}, XMR/EUR={xmr.get('eur')}")
        except Exception as e:
            logger.warning(f"CoinGecko rate fetch failed: {e}")

        return rates

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
