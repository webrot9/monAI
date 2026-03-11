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
from monai.payments.lemonsqueezy_provider import LemonSqueezyProvider
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

        # Auto-register LemonSqueezy providers from DB (previously provisioned keys)
        self._register_lemonsqueezy_from_db()

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

    def _register_lemonsqueezy_from_db(self) -> None:
        """Auto-register LemonSqueezy providers from previously provisioned API keys.

        Checks brand_api_keys table for active LemonSqueezy credentials
        and registers a provider for each brand that has them.
        """
        try:
            rows = self.db.execute(
                "SELECT DISTINCT brand FROM brand_api_keys "
                "WHERE provider = 'lemonsqueezy' AND status = 'active'"
            )
        except Exception:
            # Table may not exist yet (APIProvisioner not initialized)
            return

        for row in rows:
            brand = row["brand"]
            try:
                keys = self.db.execute(
                    "SELECT key_type, key_value FROM brand_api_keys "
                    "WHERE brand = ? AND provider = 'lemonsqueezy' AND status = 'active'",
                    (brand,),
                )
                key_map = {k["key_type"]: k["key_value"] for k in keys}

                api_key = key_map.get("access_token", "")
                store_id = key_map.get("store_id", "")
                webhook_secret = key_map.get("webhook_secret", "")

                if not api_key or not store_id:
                    continue

                # Decrypt keys if encrypted
                try:
                    from monai.utils.crypto import decrypt_value
                    api_key = decrypt_value(api_key)
                    if webhook_secret:
                        webhook_secret = decrypt_value(webhook_secret)
                except Exception:
                    pass  # Keys may not be encrypted

                provider = LemonSqueezyProvider(
                    api_key=api_key,
                    store_id=store_id,
                    webhook_secret=webhook_secret,
                )
                self.register_brand_provider(brand, "lemonsqueezy", provider)
                logger.info(f"Auto-registered LemonSqueezy for brand: {brand}")
            except Exception as e:
                logger.warning(f"Failed to register LemonSqueezy for {brand}: {e}")

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

    async def _handle_webhook_event(self, event: WebhookEvent) -> None:
        """Process a parsed webhook event — record payment in DB.

        Uses ATOMIC idempotency check: the INSERT into processed_webhooks
        and webhook_events happens in a single transaction to prevent
        race conditions from concurrent webhook deliveries.
        """
        event_id = f"{event.payment_ref}:{event.event_type.value}"
        if not event_id or not event.payment_ref:
            logger.warning("Webhook missing payment_ref — ignoring")
            return

        # Validate webhook amount (reject obviously wrong values)
        if event.amount < 0:
            logger.error(f"Webhook with negative amount rejected: {event.amount}")
            return
        if event.amount > 1_000_000:  # €1M safety cap per single webhook
            logger.error(f"Webhook with suspicious amount rejected: {event.amount}")
            return

        # ATOMIC: idempotency check + event log in single transaction
        with self.db.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO processed_webhooks (provider, event_id) VALUES (?, ?)",
                    (event.provider, event_id),
                )
            except Exception:
                # UNIQUE constraint violated — duplicate webhook
                logger.info(f"Duplicate webhook ignored: {event.provider}/{event_id}")
                return

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

        # Record payment + fee atomically in a single transaction
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO brand_payments_received "
                "(brand, account_id, lead_id, amount, currency, product, "
                "customer_email, payment_ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (brand, account_id, lead_id, event.amount, event.currency,
                 event.product, event.customer_email, event.payment_ref),
            )
            pay_id = cursor.lastrowid

            # Auto-record platform fees in same transaction
            rates = self.brand_payments.PLATFORM_FEE_RATES.get(event.provider)
            if rates:
                from decimal import Decimal
                gross = Decimal(str(event.amount))
                fee_amount = float(
                    gross * Decimal(str(rates["rate"])) + Decimal(str(rates["fixed"]))
                )
                fee_currency = event.currency  # Fee in same currency as payment
            else:
                fee_amount = 0.0
                fee_currency = event.currency

            conn.execute(
                "INSERT INTO platform_fees "
                "(brand, provider, payment_id, gross_amount, fee_amount, fee_currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (brand, event.provider, pay_id, event.amount, fee_amount, fee_currency),
            )

        logger.info(
            f"Payment recorded: {brand} received {event.amount} {event.currency} "
            f"via {event.provider} for '{event.product}' (fee: {fee_amount:.2f} {fee_currency})"
        )

    async def _handle_payment_refunded(self, event: WebhookEvent) -> None:
        """Handle a refund — mark payment and check if sweep already occurred."""
        payments = self.db.execute(
            "SELECT id, brand, amount, currency FROM brand_payments_received "
            "WHERE payment_ref = ? LIMIT 1",
            (event.payment_ref,),
        )
        if not payments:
            logger.warning(f"Refund for unknown payment: {event.payment_ref}")
            return

        payment = dict(payments[0])
        self.brand_payments.refund_payment(payment["id"])

        # Check if this brand's funds were already swept to creator
        swept = self.db.execute(
            "SELECT id, status, tx_reference FROM brand_profit_sweeps "
            "WHERE brand = ? AND status = 'completed' "
            "ORDER BY completed_at DESC LIMIT 1",
            (payment["brand"],),
        )
        if swept:
            logger.critical(
                f"REFUND AFTER SWEEP: Payment {event.payment_ref} for "
                f"{payment['amount']} {payment['currency']} was already swept. "
                f"Brand {payment['brand']} has NEGATIVE sweepable balance. "
                f"Manual intervention required."
            )

        logger.info(f"Payment refunded: {event.payment_ref}")

    async def _handle_payment_disputed(self, event: WebhookEvent) -> None:
        """Handle a dispute — mark and alert."""
        payments = self.db.execute(
            "SELECT id, brand, amount, currency FROM brand_payments_received "
            "WHERE payment_ref = ? LIMIT 1",
            (event.payment_ref,),
        )
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE brand_payments_received SET status = 'disputed' "
                "WHERE payment_ref = ?",
                (event.payment_ref,),
            )

        brand = dict(payments[0])["brand"] if payments else "unknown"
        amount = dict(payments[0])["amount"] if payments else 0
        logger.critical(
            f"PAYMENT DISPUTED: {event.payment_ref} — {amount} from brand {brand}. "
            f"Action required: respond to dispute within deadline."
        )

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
