"""Lightweight webhook server for receiving payment notifications.

Runs as an async HTTP server that receives POST requests from
Stripe, BTCPay, Gumroad, and LemonSqueezy. Routes each webhook
to the appropriate provider handler, verifies signatures, and
records payments in the database.

Uses Python's built-in asyncio server — no framework dependency.
In production, this would sit behind a reverse proxy (Caddy/nginx)
with TLS termination and a public domain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from datetime import datetime
from http import HTTPStatus
from typing import Any, Callable, Awaitable
from urllib.parse import urlparse, parse_qs

from monai.payments.base import PaymentProvider
from monai.payments.types import WebhookEvent

logger = logging.getLogger(__name__)

# Optional audit trail — set by caller (e.g. orchestrator)
_audit_trail = None


def set_webhook_audit(audit) -> None:
    """Set the audit trail instance for webhook event logging."""
    global _audit_trail
    _audit_trail = audit

# Schema for webhook event log
WEBHOOK_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    event_type TEXT,
    payment_ref TEXT,
    amount REAL,
    currency TEXT,
    brand TEXT,
    status TEXT DEFAULT 'processed',
    raw_payload TEXT,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class _RateLimiter:
    """Simple in-memory rate limiter for webhook endpoints.

    Tracks per-IP request counts within second and minute windows.
    Includes memory protection to prevent unbounded dict growth from many IPs.
    """

    MAX_TRACKED_IPS = 10_000  # Memory cap: max unique IPs to track

    def __init__(self, max_per_second: int = 10, max_per_minute: int = 200):
        self._max_per_second = max_per_second
        self._max_per_minute = max_per_minute
        self._second_counts: dict[str, int] = {}
        self._minute_counts: dict[str, int] = {}
        self._current_second: int = 0
        self._current_minute: int = 0

    def is_allowed(self, client_ip: str) -> bool:
        import time
        now = time.time()
        second = int(now)
        minute = int(now / 60)

        # Reset per-second counters on new second
        if second != self._current_second:
            self._second_counts.clear()
            self._current_second = second

        # Reset per-minute counters on new minute
        if minute != self._current_minute:
            self._minute_counts.clear()
            self._current_minute = minute

        # Memory protection: clear if too many unique IPs
        if len(self._second_counts) >= self.MAX_TRACKED_IPS:
            self._second_counts.clear()
        if len(self._minute_counts) >= self.MAX_TRACKED_IPS:
            self._minute_counts.clear()

        sec_count = self._second_counts.get(client_ip, 0)
        min_count = self._minute_counts.get(client_ip, 0)

        if sec_count >= self._max_per_second or min_count >= self._max_per_minute:
            return False

        self._second_counts[client_ip] = sec_count + 1
        self._minute_counts[client_ip] = min_count + 1
        return True


class WebhookServer:
    """Async HTTP server for receiving payment webhooks."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8420, db=None):
        self.host = host
        self.port = port
        self.db = db  # Database for dead letter queue
        self._providers: dict[str, PaymentProvider] = {}
        self._event_handlers: list[Callable[[WebhookEvent], Awaitable[None]]] = []
        self._server: asyncio.Server | None = None
        self._rate_limiter = _RateLimiter(max_per_second=10, max_per_minute=200)

    def register_provider(self, route: str, provider: PaymentProvider) -> None:
        """Register a payment provider for a webhook route.

        Args:
            route: URL path suffix, e.g. "stripe", "btcpay"
            provider: The provider instance that handles webhooks
        """
        self._providers[route] = provider
        logger.info(f"Webhook route registered: /webhooks/{route}")

    def on_event(self, handler: Callable[[WebhookEvent], Awaitable[None]]) -> None:
        """Register a callback for processed webhook events.

        The handler receives the parsed WebhookEvent and can
        update the database, trigger sweeps, etc.
        """
        self._event_handlers.append(handler)

    async def start(self) -> None:
        """Start the webhook server."""
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port,
        )
        logger.info(f"Webhook server listening on {self.host}:{self.port}")

    async def stop(self) -> None:
        """Stop the webhook server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Webhook server stopped")

    async def _handle_connection(self, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter) -> None:
        """Handle a single HTTP connection."""
        try:
            # Rate limit by client IP
            peername = writer.get_extra_info("peername")
            client_ip = peername[0] if peername else "unknown"
            if not self._rate_limiter.is_allowed(client_ip):
                logger.warning(f"Rate limit exceeded for {client_ip}")
                await self._send_response(writer, 429, "Too Many Requests")
                return

            # Read the full HTTP request
            request_line = await asyncio.wait_for(
                reader.readline(), timeout=10.0
            )
            if not request_line:
                writer.close()
                return

            request_str = request_line.decode("utf-8", errors="replace").strip()
            parts = request_str.split(" ")
            if len(parts) < 2:
                await self._send_response(writer, 400, "Bad Request")
                return

            method = parts[0]
            path = parts[1]

            # Read headers
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    break
                if ":" in line_str:
                    key, _, value = line_str.partition(":")
                    headers[key.strip().lower()] = value.strip()

            # Read body
            content_length = int(headers.get("content-length", "0"))
            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=10.0
                )

            # Route the request
            if method == "POST" and path.startswith("/webhooks/"):
                await self._handle_webhook(writer, path, headers, body)
            elif method == "GET" and path == "/health":
                await self._send_response(writer, 200, json.dumps({
                    "status": "ok",
                    "providers": list(self._providers.keys()),
                }))
            else:
                await self._send_response(writer, 404, "Not Found")

        except asyncio.TimeoutError:
            await self._send_response(writer, 408, "Request Timeout")
        except Exception as e:
            logger.error(f"Webhook handler error: {e}\n{traceback.format_exc()}")
            await self._send_response(writer, 500, "Internal Server Error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_webhook(self, writer: asyncio.StreamWriter,
                              path: str, headers: dict[str, str],
                              body: bytes) -> None:
        """Route a webhook to the appropriate provider."""
        # Extract provider name from path: /webhooks/<provider>
        route = path.split("/webhooks/", 1)[-1].strip("/")
        route = route.split("?")[0]  # Remove query string

        provider = self._providers.get(route)
        if not provider:
            logger.warning(f"Unknown webhook route: {route}")
            await self._send_response(writer, 404, f"Unknown provider: {route}")
            return

        # Convert lowercase headers to original casing for signature verification
        original_headers = {
            "stripe-signature": headers.get("stripe-signature", ""),
            "btcpay-sig": headers.get("btcpay-sig", ""),
            "x-gumroad-signature": headers.get("x-gumroad-signature", ""),
            "x-signature": headers.get("x-signature", ""),
            "content-type": headers.get("content-type", ""),
        }

        try:
            event = await provider.handle_webhook(body, original_headers)
        except Exception as e:
            logger.error(f"Webhook processing error ({route}): {e}")
            if _audit_trail:
                _audit_trail.log(
                    "webhook_server", "api_call", "webhook_error",
                    details={"provider": route, "error": str(e)},
                    success=False, risk_level="high",
                )
            await self._send_response(writer, 500, str(e))
            return

        if event is None:
            logger.warning(f"Webhook from {route} could not be parsed/verified")
            if _audit_trail:
                _audit_trail.log(
                    "webhook_server", "api_call", "webhook_invalid",
                    details={"provider": route}, success=False,
                )
            await self._send_response(writer, 400, "Invalid webhook")
            return

        # Dispatch to event handlers — if ANY handler fails, return 500
        # so the payment provider retries delivery (never silently drop).
        for handler in self._event_handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error(f"Event handler error: {e}")
                if _audit_trail:
                    _audit_trail.log(
                        "webhook_server", "payment", "handler_error",
                        details={"provider": event.provider, "error": str(e),
                                 "payment_ref": event.payment_ref},
                        success=False, risk_level="critical",
                    )
                # Save to dead letter queue for later retry
                self._save_to_dead_letter(
                    provider=event.provider, raw_payload=body,
                    headers=headers, error=str(e),
                )
                await self._send_response(writer, 500, f"Handler error: {e}")
                return

        # Audit successful webhook
        if _audit_trail:
            _audit_trail.log(
                "webhook_server", "payment", "webhook_received",
                details={
                    "provider": event.provider,
                    "event_type": event.event_type.value,
                    "payment_ref": event.payment_ref,
                    "amount": str(event.amount),
                    "currency": event.currency,
                },
                brand=event.metadata.get("brand", ""),
            )

        logger.info(
            f"Webhook processed: {event.provider}/{event.event_type.value} "
            f"ref={event.payment_ref} amount={event.amount} {event.currency}"
        )

        await self._send_response(writer, 200, "OK")

    def _save_to_dead_letter(self, provider: str, raw_payload: bytes,
                             headers: dict[str, str], error: str) -> None:
        """Save a failed webhook to the dead letter queue for later retry."""
        if not self.db:
            logger.warning("No DB configured — cannot save to dead letter queue")
            return
        try:
            self.db.execute_insert(
                "INSERT INTO webhook_dead_letter "
                "(provider, raw_payload, headers, error) VALUES (?, ?, ?, ?)",
                (provider, raw_payload.decode("utf-8", errors="replace"),
                 json.dumps(headers), error),
            )
            logger.info(f"Webhook saved to dead letter queue: {provider} — {error}")
        except Exception as e:
            logger.error(f"Failed to save to dead letter queue: {e}")

    @staticmethod
    async def _send_response(writer: asyncio.StreamWriter,
                             status: int, body: str) -> None:
        """Send an HTTP response."""
        reason = HTTPStatus(status).phrase
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )
        writer.write(response.encode("utf-8"))
        await writer.drain()


def make_checkout_revenue_handler(db):
    """Create a webhook event handler that records revenue via checkout_links.

    When a payment webhook fires, looks up the payment_ref in checkout_links
    to find which strategy created it, then records revenue in transactions.

    Usage:
        server.on_event(make_checkout_revenue_handler(db))
    """
    async def _handler(event: WebhookEvent) -> None:
        if event.event_type.value not in ("payment_completed", "payment"):
            return

        rows = db.execute(
            "SELECT * FROM checkout_links WHERE payment_ref = ? AND status = 'pending'",
            (event.payment_ref,),
        )
        if not rows:
            return

        link = dict(rows[0])
        amount = event.amount if event.amount > 0 else link["amount"]

        db.execute(
            "UPDATE checkout_links SET status = 'paid', paid_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (link["id"],),
        )
        db.execute_insert(
            "INSERT INTO transactions (type, category, amount, description) "
            "VALUES ('revenue', ?, ?, ?)",
            (link["strategy_name"], amount,
             f"Sale via {link['provider']}: {link['product']}"),
        )
        logger.info(
            f"Revenue recorded: {link['strategy_name']} — "
            f"{amount} {event.currency} for {link['product']}"
        )

    return _handler
