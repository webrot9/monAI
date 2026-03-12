"""RetoSwap (Haveno) P2P exchange integration — XMR to fiat/cash.

RetoSwap is a decentralized P2P exchange based on Haveno (fork of Bisq).
It uses Monero as the base currency and supports trading XMR for:
- SEPA bank transfers (EUR)
- Revolut, Wise, PayPal
- Cash by mail / in-person
- Other crypto (BTC, ETH, USDT, LTC)

No KYC. No central server. All trades via Tor + Monero multisig.

The Haveno daemon exposes a gRPC API for programmatic trading.
This module wraps that API for autonomous XMR→EUR conversion.

Requires: haveno-daemon running locally (or via Docker).
    https://github.com/retoaccess1/haveno-reto
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# Default gRPC endpoint for local Haveno daemon
DEFAULT_DAEMON_HOST = "127.0.0.1"
DEFAULT_DAEMON_PORT = 9999


class TradeStatus(str, Enum):
    """Status of a P2P trade on RetoSwap."""
    OFFER_POSTED = "offer_posted"
    OFFER_TAKEN = "offer_taken"
    DEPOSIT_CONFIRMED = "deposit_confirmed"
    FIAT_SENT = "fiat_sent"          # Buyer sent fiat
    FIAT_RECEIVED = "fiat_received"  # Seller confirmed receipt
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"
    FAILED = "failed"


class PaymentMethod(str, Enum):
    """Supported fiat payment methods on RetoSwap."""
    SEPA = "SEPA"                    # EU bank transfer
    SEPA_INSTANT = "SEPA_INSTANT"    # EU instant transfer
    REVOLUT = "REVOLUT"
    WISE = "WISE"
    PAYPAL = "PAYPAL"              # PayPal Friends & Family (no fees, no invoice)
    CASH_BY_MAIL = "CASH_BY_MAIL"  # Physical cash sent via registered mail
    CASH_AT_ATM = "CASH_AT_ATM"
    NATIONAL_BANK = "NATIONAL_BANK"


@dataclass
class TradeOffer:
    """An offer to sell XMR on RetoSwap."""
    offer_id: str = ""
    direction: str = "SELL"         # We sell XMR, buyer pays fiat
    amount_xmr: float = 0.0
    min_amount_xmr: float = 0.0
    price_eur: float = 0.0         # Price per XMR in fiat
    payment_method: str = "SEPA"
    currency: str = "EUR"
    margin_pct: float = 0.0        # % above/below market price


@dataclass
class TradeResult:
    """Result of a completed trade."""
    trade_id: str = ""
    status: TradeStatus = TradeStatus.FAILED
    amount_xmr: float = 0.0
    amount_fiat: float = 0.0
    currency: str = "EUR"
    payment_method: str = ""
    fee_xmr: float = 0.0
    counterparty: str = ""         # Anonymized
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class RetoSwapClient:
    """Client for the RetoSwap/Haveno gRPC daemon API.

    Wraps the Haveno daemon's gRPC interface for autonomous trading:
    - Post sell offers (XMR → EUR)
    - Monitor and manage active trades
    - Confirm fiat payments received
    - Track trade history and fees

    The daemon must be running locally. It handles Tor connections,
    Monero multisig, and trade protocol internally.
    """

    # Trade timeout: cancel offer if not taken within this period
    OFFER_TIMEOUT_HOURS = 24

    # Maximum acceptable spread from market price (safety guard)
    MAX_PRICE_DEVIATION_PCT = 10.0

    # Minimum trade amount (RetoSwap has its own minimums too)
    MIN_TRADE_XMR = 0.01

    def __init__(
        self,
        daemon_host: str = DEFAULT_DAEMON_HOST,
        daemon_port: int = DEFAULT_DAEMON_PORT,
        daemon_password: str = "",
        preferred_payment_method: str = "PAYPAL",
        preferred_currency: str = "EUR",
        price_margin_pct: float = -1.0,  # Sell 1% below market for faster fills
        fallback_payment_methods: list[str] | None = None,
    ):
        self.daemon_host = daemon_host
        self.daemon_port = daemon_port
        self.daemon_password = daemon_password
        self.preferred_payment_method = preferred_payment_method
        self.preferred_currency = preferred_currency
        self.price_margin_pct = price_margin_pct
        self.fallback_payment_methods = fallback_payment_methods or ["CASH_BY_MAIL", "REVOLUT"]
        self._channel = None
        self._stubs: dict[str, Any] = {}

    async def connect(self) -> bool:
        """Connect to the Haveno daemon via gRPC."""
        try:
            import grpc
        except ImportError:
            logger.error(
                "grpcio not installed. Install with: pip install grpcio grpcio-tools"
            )
            return False

        try:
            # Haveno daemon gRPC endpoint
            target = f"{self.daemon_host}:{self.daemon_port}"
            self._channel = grpc.aio.insecure_channel(target)

            # Test connectivity
            await asyncio.wait_for(
                self._channel.channel_ready(),
                timeout=10.0,
            )
            logger.info(f"Connected to RetoSwap daemon at {target}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to RetoSwap daemon: {e}")
            return False

    async def disconnect(self) -> None:
        """Close gRPC channel."""
        if self._channel:
            await self._channel.close()
            self._channel = None

    # ── Wallet Operations ────────────────────────────────────────

    async def get_balance(self) -> dict[str, float]:
        """Get the Haveno internal wallet balance.

        Returns dict with available_balance and pending_balance in XMR.
        """
        return await self._call("GetBalances", {})

    # ── Market Data ──────────────────────────────────────────────

    async def get_market_price(self, currency: str = "EUR") -> float:
        """Get current XMR market price in the given fiat currency."""
        result = await self._call("GetMarketPrice", {
            "currency_code": currency,
        })
        return result.get("price", 0.0)

    async def get_offers(
        self,
        direction: str = "BUY",  # We want to see buyers (we sell)
        currency: str = "EUR",
    ) -> list[dict[str, Any]]:
        """Get available offers on the orderbook.

        direction=BUY means we see people who want to buy XMR (pay fiat).
        We can take these offers to sell our XMR for fiat.
        """
        result = await self._call("GetOffers", {
            "direction": direction,
            "currency_code": currency,
        })
        return result.get("offers", [])

    # ── Sell XMR for Fiat ────────────────────────────────────────

    async def create_sell_offer(
        self,
        amount_xmr: float,
        min_amount_xmr: float = 0.0,
        payment_method: str = "",
        currency: str = "",
        margin_pct: float | None = None,
    ) -> TradeOffer:
        """Post a sell offer: we sell XMR, buyer pays fiat.

        Args:
            amount_xmr: Maximum amount of XMR to sell
            min_amount_xmr: Minimum amount (for range offers)
            payment_method: SEPA, REVOLUT, etc. (defaults to preferred)
            currency: EUR, USD, etc. (defaults to preferred)
            margin_pct: Price margin from market price (negative = below market)

        Returns:
            TradeOffer with offer_id for tracking
        """
        method = payment_method or self.preferred_payment_method
        curr = currency or self.preferred_currency
        margin = margin_pct if margin_pct is not None else self.price_margin_pct

        if amount_xmr < self.MIN_TRADE_XMR:
            logger.warning(f"Amount {amount_xmr} XMR below minimum {self.MIN_TRADE_XMR}")
            return TradeOffer()

        # Safety: check market price before posting
        market_price = await self.get_market_price(curr)
        if market_price <= 0:
            logger.error("Could not get market price — aborting offer creation")
            return TradeOffer()

        result = await self._call("PostOffer", {
            "direction": "SELL",
            "currency_code": curr,
            "amount": int(amount_xmr * 1e12),  # Atomic units
            "min_amount": int((min_amount_xmr or amount_xmr * 0.1) * 1e12),
            "payment_method_id": method,
            "market_price_margin_pct": margin,
            "buyer_security_deposit_pct": 15.0,  # Standard security deposit
        })

        offer_id = result.get("id", "")
        if offer_id:
            logger.info(
                f"Posted sell offer: {amount_xmr:.4f} XMR via {method} "
                f"(margin: {margin}%, market: {market_price:.2f} {curr}/XMR)"
            )

        return TradeOffer(
            offer_id=offer_id,
            direction="SELL",
            amount_xmr=amount_xmr,
            min_amount_xmr=min_amount_xmr or amount_xmr * 0.1,
            price_eur=market_price * (1 + margin / 100),
            payment_method=method,
            currency=curr,
            margin_pct=margin,
        )

    async def take_buy_offer(
        self,
        offer_id: str,
        amount_xmr: float,
        payment_account_id: str = "",
    ) -> TradeResult:
        """Take an existing buy offer from the orderbook.

        Someone wants to buy XMR — we sell ours and receive fiat.

        Args:
            offer_id: ID of the offer to take
            amount_xmr: How much XMR to sell
            payment_account_id: Our payment account for receiving fiat
        """
        result = await self._call("TakeOffer", {
            "offer_id": offer_id,
            "amount": int(amount_xmr * 1e12),
            "payment_account_id": payment_account_id,
        })

        trade_id = result.get("trade_id", "")
        return TradeResult(
            trade_id=trade_id,
            status=TradeStatus.OFFER_TAKEN if trade_id else TradeStatus.FAILED,
            amount_xmr=amount_xmr,
            raw=result,
        )

    async def confirm_payment_received(self, trade_id: str) -> bool:
        """Confirm we received the fiat payment from the buyer.

        This releases the XMR from multisig escrow to the buyer.
        ONLY call this after verifying fiat is in your account.
        """
        result = await self._call("ConfirmPaymentReceived", {
            "trade_id": trade_id,
        })
        if result.get("success", False):
            logger.info(f"Confirmed fiat received for trade {trade_id}")
            return True
        return False

    # ── Trade Management ─────────────────────────────────────────

    async def get_trade(self, trade_id: str) -> TradeResult:
        """Get current status of a trade."""
        result = await self._call("GetTrade", {"trade_id": trade_id})
        status_map = {
            "DEPOSIT_PUBLISHED": TradeStatus.DEPOSIT_CONFIRMED,
            "DEPOSIT_CONFIRMED": TradeStatus.DEPOSIT_CONFIRMED,
            "FIAT_SENT": TradeStatus.FIAT_SENT,
            "FIAT_RECEIVED": TradeStatus.FIAT_RECEIVED,
            "COMPLETED": TradeStatus.COMPLETED,
            "FAILED": TradeStatus.FAILED,
        }
        return TradeResult(
            trade_id=trade_id,
            status=status_map.get(result.get("state", ""), TradeStatus.FAILED),
            amount_xmr=result.get("amount", 0) / 1e12,
            amount_fiat=result.get("trade_price", 0.0),
            currency=result.get("currency_code", "EUR"),
            payment_method=result.get("payment_method_id", ""),
            raw=result,
        )

    async def get_open_trades(self) -> list[TradeResult]:
        """Get all currently open/active trades."""
        result = await self._call("GetTrades", {})
        trades = []
        for t in result.get("trades", []):
            trades.append(TradeResult(
                trade_id=t.get("trade_id", ""),
                status=TradeStatus.OFFER_TAKEN,
                amount_xmr=t.get("amount", 0) / 1e12,
                amount_fiat=t.get("trade_price", 0.0),
                currency=t.get("currency_code", "EUR"),
                raw=t,
            ))
        return trades

    async def cancel_offer(self, offer_id: str) -> bool:
        """Cancel an open offer."""
        result = await self._call("CancelOffer", {"id": offer_id})
        return bool(result.get("success", False))

    # ── Payment Accounts ─────────────────────────────────────────

    async def get_payment_accounts(self) -> list[dict[str, Any]]:
        """List configured fiat payment accounts (SEPA, Revolut, etc.)."""
        result = await self._call("GetPaymentAccounts", {})
        return result.get("payment_accounts", [])

    async def create_payment_account(
        self,
        account_name: str,
        payment_method: str,
        account_payload: dict[str, str],
    ) -> dict[str, Any]:
        """Create a fiat payment account for receiving money.

        Example for SEPA:
            account_payload = {
                "holder_name": "Mario Rossi",
                "iban": "IT60X0542811101000000123456",
                "bic": "BPMOIT22XXX",
            }
        """
        result = await self._call("CreatePaymentAccount", {
            "account_name": account_name,
            "payment_method_id": payment_method,
            "account_payload": account_payload,
        })
        return result

    # ── Autonomous Sell Flow ─────────────────────────────────────

    async def auto_sell_xmr(
        self,
        amount_xmr: float,
        payment_account_id: str = "",
        strategy: str = "take_best",
    ) -> TradeResult:
        """Autonomously sell XMR for fiat.

        Tries preferred payment method first (default: PayPal F&F),
        then falls back to fallback methods (cash-by-mail, Revolut, etc.).

        Strategies:
            take_best: Take the best existing buy offer from orderbook
            post_offer: Post our own sell offer and wait for a taker
            aggressive: Take best offer even at slightly worse price

        This is the main entry point for the sweep engine's
        XMR→fiat conversion flow.
        """
        if amount_xmr < self.MIN_TRADE_XMR:
            return TradeResult(
                status=TradeStatus.FAILED,
                error=f"Amount {amount_xmr} XMR below minimum {self.MIN_TRADE_XMR}",
            )

        currency = self.preferred_currency
        market_price = await self.get_market_price(currency)
        if market_price <= 0:
            return TradeResult(
                status=TradeStatus.FAILED,
                error="Could not get market price",
            )

        # Build method list: preferred first, then fallbacks
        methods_to_try = [self.preferred_payment_method] + [
            m for m in self.fallback_payment_methods
            if m != self.preferred_payment_method
        ]

        if strategy in ("take_best", "aggressive"):
            # Look for existing buy offers to take
            offers = await self.get_offers(direction="BUY", currency=currency)

            # Try each payment method in priority order
            for method in methods_to_try:
                matching = self._filter_offers(
                    offers, method, amount_xmr, market_price, strategy,
                )
                if matching:
                    # Take the best-priced offer (highest price = most EUR per XMR)
                    matching.sort(key=lambda x: x[0], reverse=True)
                    best_price, best_offer = matching[0]

                    trade_amount = min(amount_xmr, best_offer.get("amount", 0) / 1e12)
                    logger.info(
                        f"Taking buy offer {best_offer.get('id')}: "
                        f"{trade_amount:.4f} XMR @ {best_price:.2f} {currency}/XMR "
                        f"via {method} (market: {market_price:.2f})"
                    )

                    return await self.take_buy_offer(
                        offer_id=best_offer["id"],
                        amount_xmr=trade_amount,
                        payment_account_id=payment_account_id,
                    )

                logger.debug(f"No matching {method} offers, trying next method...")

        # Fallback: post our own sell offer with preferred method
        logger.info(
            f"No matching offers found. Posting sell offer: "
            f"{amount_xmr:.4f} XMR via {self.preferred_payment_method}"
        )
        offer = await self.create_sell_offer(amount_xmr=amount_xmr)
        if offer.offer_id:
            return TradeResult(
                trade_id=offer.offer_id,
                status=TradeStatus.OFFER_POSTED,
                amount_xmr=amount_xmr,
                amount_fiat=amount_xmr * offer.price_eur,
                currency=currency,
                payment_method=self.preferred_payment_method,
            )

        return TradeResult(
            status=TradeStatus.FAILED,
            error="Failed to post sell offer",
        )

    def _filter_offers(
        self,
        offers: list[dict[str, Any]],
        payment_method: str,
        amount_xmr: float,
        market_price: float,
        strategy: str,
    ) -> list[tuple[float, dict[str, Any]]]:
        """Filter orderbook offers by payment method, amount, and price."""
        matching = []
        for offer in offers:
            offer_method = offer.get("payment_method_id", "")
            offer_min = offer.get("min_amount", 0) / 1e12

            if offer_method != payment_method:
                continue
            if amount_xmr < offer_min:
                continue

            offer_price = offer.get("price", 0.0)
            if offer_price <= 0:
                continue
            deviation = (offer_price - market_price) / market_price * 100
            max_deviation = (
                self.MAX_PRICE_DEVIATION_PCT * 1.5
                if strategy == "aggressive"
                else self.MAX_PRICE_DEVIATION_PCT
            )
            if abs(deviation) > max_deviation:
                continue

            matching.append((offer_price, offer))
        return matching

    # ── Health Check ─────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Check if RetoSwap daemon is reachable."""
        try:
            result = await self._call("GetVersion", {})
            return bool(result.get("version"))
        except Exception:
            return False

    # ── gRPC Call Wrapper ────────────────────────────────────────

    async def _call(self, method: str, params: dict) -> dict:
        """Make a gRPC call to the Haveno daemon.

        Uses direct gRPC unary calls with the Haveno daemon's service
        definitions. The daemon authenticates via a password header.
        """
        if not self._channel:
            connected = await self.connect()
            if not connected:
                raise RetoSwapError("Not connected to daemon")

        try:
            import grpc
        except ImportError:
            raise RetoSwapError(
                "grpcio not installed. Install with: pip install grpcio"
            )

        # Build gRPC metadata for authentication
        metadata = []
        if self.daemon_password:
            metadata.append(("password", self.daemon_password))

        # Map API method names to Haveno gRPC service paths and request builders
        service_map = {
            "GetBalances": ("/pb.Wallets/GetBalances", {}),
            "GetMarketPrice": ("/pb.Price/GetMarketPrice", {
                "currency_code": params.get("currency_code", "EUR"),
            }),
            "GetOffers": ("/pb.Offers/GetOffers", {
                "direction": params.get("direction", "BUY"),
                "currency_code": params.get("currency_code", "EUR"),
            }),
            "PostOffer": ("/pb.Offers/PostOffer", params),
            "TakeOffer": ("/pb.Trades/TakeOffer", params),
            "GetTrade": ("/pb.Trades/GetTrade", {
                "trade_id": params.get("trade_id", ""),
            }),
            "GetTrades": ("/pb.Trades/GetTrades", {}),
            "CancelOffer": ("/pb.Offers/CancelOffer", {
                "id": params.get("id", ""),
            }),
            "ConfirmPaymentReceived": ("/pb.Trades/ConfirmPaymentReceived", {
                "trade_id": params.get("trade_id", ""),
            }),
            "GetPaymentAccounts": ("/pb.PaymentAccounts/GetPaymentAccounts", {}),
            "CreatePaymentAccount": ("/pb.PaymentAccounts/CreatePaymentAccount", params),
            "GetVersion": ("/pb.GetVersion/GetVersion", {}),
        }

        if method not in service_map:
            raise RetoSwapError(f"Unknown method: {method}")

        grpc_path, request_params = service_map[method]

        try:
            # Use generic JSON-encoded unary call via channel
            import json as _json
            request_bytes = _json.dumps(request_params).encode("utf-8")

            # Generic unary-unary call using the full method path
            call = self._channel.unary_unary(
                grpc_path,
                request_serializer=lambda x: x,
                response_deserializer=lambda x: x,
            )
            response_bytes = await call(
                request_bytes,
                metadata=metadata,
                timeout=30.0,
            )

            # Parse response — Haveno daemon returns protobuf, but we use
            # grpc-json-transcoding when available, otherwise raw bytes
            try:
                return _json.loads(response_bytes)
            except (ValueError, TypeError):
                # Raw protobuf response — return as opaque result
                return {"raw": response_bytes.hex() if response_bytes else ""}

        except grpc.RpcError as e:
            code = e.code() if hasattr(e, "code") else "UNKNOWN"
            details = e.details() if hasattr(e, "details") else str(e)
            raise RetoSwapError(
                f"gRPC call '{method}' failed: {code} — {details}"
            ) from e
        except Exception as e:
            raise RetoSwapError(f"gRPC call '{method}' failed: {e}") from e


class RetoSwapError(Exception):
    """Error from RetoSwap/Haveno daemon."""
    pass
