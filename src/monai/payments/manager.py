"""Unified Payment Manager — central registry for all payment providers.

Coordinates payment collection, webhook processing, and profit sweeping.
Integrates with the orchestrator cycle to run periodically.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from monai.business.brand_payments import BrandPayments
from monai.config import Config
from monai.db.database import Database
from monai.payments.base import PaymentProvider
from monai.payments.monero_provider import MoneroProvider
from monai.payments.sweep_engine import SweepEngine
from monai.payments.types import (
    PaymentIntent,
    PaymentResult,
    ProviderBalance,
    WebhookEvent,
    WebhookEventType,
)
from monai.payments.webhook_server import WebhookServer

logger = logging.getLogger(__name__)


class UnifiedPaymentManager:
    """Central payment system coordinating all providers and sweeps.

    Responsibilities:
    1. Provider registry — know which providers are configured
    2. Payment creation — create payment links/invoices per brand
    3. Webhook handling — route incoming payments to correct brand
    4. Sweep scheduling — periodically sweep profits to creator
    5. Balance tracking — aggregate balances across providers
    6. Health monitoring — check all providers are operational
    """

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.brand_payments = BrandPayments(db)
        self.sweep_engine = SweepEngine(config, db)
        self.webhook_server = WebhookServer()

        self._providers: dict[str, PaymentProvider] = {}
        self._brand_providers: dict[str, dict[str, PaymentProvider]] = {}

        # Auto-register Monero if configured
        if config.monero.wallet_rpc_url:
            self._register_monero(config)

        # Register webhook event handler
        self.webhook_server.on_event(self._handle_webhook_event)

        # Init webhook log schema
        self._init_schema()

    def _init_schema(self):
        with self.db.connect() as conn:
            conn.executescript("""
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

                CREATE TABLE IF NOT EXISTS processed_webhooks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(provider, event_id)
                );
            """)

    def _register_monero(self, config: Config) -> None:
        """Register Monero provider from config."""
        monero = MoneroProvider(
            wallet_rpc_url=config.monero.wallet_rpc_url,
            rpc_user=config.monero.rpc_user,
            rpc_password=config.monero.rpc_password,
            min_confirmations=config.creator_wallet.min_confirmations_xmr,
            proxy_url=config.monero.proxy_url,
        )
        self.register_provider("crypto_xmr", monero)

    def register_provider(self, name: str, provider: PaymentProvider) -> None:
        """Register a payment provider globally."""
        self._providers[name] = provider
        # Also register with webhook server
        self.webhook_server.register_provider(name, provider)
        logger.info(f"Payment provider registered: {name}")

    def register_brand_provider(self, brand: str, provider_name: str,
                                provider: PaymentProvider) -> None:
        """Register a provider for a specific brand."""
        if brand not in self._brand_providers:
            self._brand_providers[brand] = {}
        self._brand_providers[brand][provider_name] = provider
        logger.info(f"Brand provider registered: {brand}/{provider_name}")

    def get_provider(self, name: str) -> PaymentProvider | None:
        """Get a provider by name."""
        return self._providers.get(name)

    def get_brand_provider(self, brand: str, provider_name: str) -> PaymentProvider | None:
        """Get a brand-specific provider, falling back to global."""
        brand_provs = self._brand_providers.get(brand, {})
        return brand_provs.get(provider_name) or self._providers.get(provider_name)

    # ── Payment Operations ──────────────────────────────────────

    async def create_payment(self, brand: str, provider_name: str,
                             intent: PaymentIntent) -> PaymentResult:
        """Create a payment link/invoice for a brand."""
        provider = self.get_brand_provider(brand, provider_name)
        if not provider:
            return PaymentResult(
                success=False,
                error=f"Provider {provider_name} not registered for brand {brand}",
            )

        intent.brand = brand
        result = await provider.create_payment(intent)

        if result.success:
            logger.info(
                f"Payment created: {brand}/{provider_name} "
                f"amount={result.amount} {result.currency} "
                f"url={result.checkout_url[:50]}..."
            )

        return result

    async def verify_payment(self, provider_name: str,
                             payment_ref: str) -> PaymentResult:
        """Verify a payment across any provider."""
        provider = self._providers.get(provider_name)
        if not provider:
            return PaymentResult(
                success=False,
                error=f"Provider {provider_name} not registered",
            )
        return await provider.verify_payment(payment_ref)

    # ── Sweep Operations ────────────────────────────────────────

    async def run_sweep_cycle(self) -> dict[str, Any]:
        """Run the sweep cycle — transfer profits to creator."""
        results = await self.sweep_engine.run_sweep_cycle()
        pending = await self.sweep_engine.check_pending_sweeps()

        return {
            "sweeps_attempted": len(results),
            "sweeps_successful": sum(1 for r in results if r.success),
            "total_xmr_swept": sum(r.amount_crypto for r in results if r.success),
            "pending_sweeps_checked": len(pending),
        }

    async def sweep_brand(self, brand: str,
                          amount: float | None = None) -> dict[str, Any]:
        """Manually trigger a sweep for a specific brand."""
        result = await self.sweep_engine.sweep_brand(brand, amount)
        return {
            "success": result.success,
            "tx_hash": result.tx_hash,
            "amount_xmr": result.amount_crypto,
            "error": result.error,
        }

    # ── Balance Aggregation ─────────────────────────────────────

    async def get_all_balances(self) -> dict[str, Any]:
        """Get balances across all providers."""
        balances: dict[str, Any] = {}
        for name, provider in self._providers.items():
            try:
                bal = await provider.get_balance()
                balances[name] = {
                    "available": bal.available,
                    "pending": bal.pending,
                    "currency": bal.currency,
                }
            except Exception as e:
                balances[name] = {"error": str(e)}

        return balances

    # ── Webhook Event Processing ────────────────────────────────

    def _is_duplicate_webhook(self, provider: str, event_id: str) -> bool:
        """Check if this webhook event was already processed (idempotency)."""
        if not event_id:
            return False
        try:
            self.db.execute_insert(
                "INSERT INTO processed_webhooks (provider, event_id) VALUES (?, ?)",
                (provider, event_id),
            )
            return False  # Successfully inserted — first time seeing this event
        except Exception:
            return True  # UNIQUE constraint violated — duplicate

    async def _handle_webhook_event(self, event: WebhookEvent) -> None:
        """Process a parsed webhook event — record payment in DB.

        Uses idempotency check to prevent double-processing of webhooks.
        """
        # Idempotency: derive a unique event ID from provider + payment_ref + event_type
        event_id = f"{event.payment_ref}:{event.event_type.value}"
        if self._is_duplicate_webhook(event.provider, event_id):
            logger.info(
                f"Duplicate webhook ignored: {event.provider}/{event_id}"
            )
            return

        # Log the event atomically with processing
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO webhook_events "
                "(provider, event_type, payment_ref, amount, currency, brand, raw_payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.provider,
                    event.event_type.value,
                    event.payment_ref,
                    event.amount,
                    event.currency,
                    event.metadata.get("brand", ""),
                    json.dumps(event.raw) if event.raw else None,
                ),
            )

        # Route to appropriate handler based on event type
        if event.event_type == WebhookEventType.PAYMENT_COMPLETED:
            await self._handle_payment_completed(event)
        elif event.event_type == WebhookEventType.PAYMENT_REFUNDED:
            await self._handle_payment_refunded(event)
        elif event.event_type == WebhookEventType.PAYMENT_DISPUTED:
            await self._handle_payment_disputed(event)

    async def _handle_payment_completed(self, event: WebhookEvent) -> None:
        """Handle a completed payment — record it for the brand."""
        brand = event.metadata.get("brand", "")
        if not brand:
            logger.warning(f"Payment without brand attribution: {event.payment_ref}")
            return

        # Find the brand's collection account for this provider
        accounts = self.brand_payments.get_collection_accounts(brand)
        matching = [a for a in accounts if a["provider"] == event.provider]

        if not matching:
            logger.warning(
                f"No collection account for {brand}/{event.provider}, "
                f"recording without account link"
            )
            account_id = 0
        else:
            account_id = matching[0]["id"]

        lead_id = event.metadata.get("lead_id")
        if lead_id:
            try:
                lead_id = int(lead_id)
            except (ValueError, TypeError):
                lead_id = None

        pay_id = self.brand_payments.record_payment(
            brand=brand,
            account_id=account_id,
            amount=event.amount,
            product=event.product,
            customer_email=event.customer_email,
            payment_ref=event.payment_ref,
            lead_id=lead_id,
            currency=event.currency,
        )

        # Auto-record platform fees
        self.brand_payments.record_platform_fee(
            brand=brand,
            provider=event.provider,
            payment_id=pay_id,
            gross_amount=event.amount,
            fee_currency=event.currency,
        )

        logger.info(
            f"Payment recorded: {brand} received {event.amount} {event.currency} "
            f"via {event.provider} for '{event.product}'"
        )

    async def _handle_payment_refunded(self, event: WebhookEvent) -> None:
        """Handle a refund."""
        # Find the payment by ref and refund it
        payments = self.db.execute(
            "SELECT id FROM brand_payments_received WHERE payment_ref = ? LIMIT 1",
            (event.payment_ref,),
        )
        if payments:
            self.brand_payments.refund_payment(payments[0]["id"])
            logger.info(f"Payment refunded: {event.payment_ref}")

    async def _handle_payment_disputed(self, event: WebhookEvent) -> None:
        """Handle a dispute."""
        self.db.execute(
            "UPDATE brand_payments_received SET status = 'disputed' "
            "WHERE payment_ref = ?",
            (event.payment_ref,),
        )
        logger.warning(f"Payment disputed: {event.payment_ref}")

    # ── Health & Status ─────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Check health of all registered providers."""
        status: dict[str, Any] = {}
        for name, provider in self._providers.items():
            try:
                ok = await provider.health_check()
                status[name] = "healthy" if ok else "unhealthy"
            except Exception as e:
                status[name] = f"error: {e}"

        status["webhook_server"] = "running" if self.webhook_server._server else "stopped"
        status["creator_wallet_configured"] = bool(self.config.creator_wallet.xmr_address)
        status["sweep_summary"] = self.sweep_engine.get_sweep_summary()

        return status

    async def start_webhook_server(self) -> None:
        """Start the webhook server if not already running."""
        if not self.webhook_server._server:
            await self.webhook_server.start()

    async def stop_webhook_server(self) -> None:
        """Stop the webhook server."""
        await self.webhook_server.stop()

    def get_status(self) -> dict[str, Any]:
        """Get payment system status summary."""
        return {
            "registered_providers": list(self._providers.keys()),
            "brand_providers": {
                brand: list(provs.keys())
                for brand, provs in self._brand_providers.items()
            },
            "sweep_summary": self.sweep_engine.get_sweep_summary(),
        }
