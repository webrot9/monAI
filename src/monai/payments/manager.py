"""Unified Payment Manager — central registry for all payment providers.

Coordinates payment collection, webhook processing, and profit sweeping.
Integrates with the orchestrator cycle to run periodically.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
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

    def __init__(self, config: Config, db: Database, ledger=None):
        self.config = config
        self.db = db
        self.ledger = ledger  # GeneralLedger for double-entry bookkeeping
        self.brand_payments = BrandPayments(db)
        self.sweep_engine = SweepEngine(config, db)
        self.webhook_server = WebhookServer(db=db)

        self._providers: dict[str, PaymentProvider] = {}
        self._brand_providers: dict[str, dict[str, PaymentProvider]] = {}

        # Auto-register Monero if configured
        if config.monero.wallet_rpc_url:
            self._register_monero(config)

        # Auto-register LemonSqueezy providers from DB (previously provisioned keys)
        self._register_lemonsqueezy_from_db()

        # Register webhook event handler
        self.webhook_server.on_event(self._handle_webhook_event)

        # Init webhook log schema + deficit tracking
        self._init_schema()
        self._ensure_deficit_table()

    def _ensure_deficit_table(self) -> None:
        """Create sweep_deficits table for tracking refund-after-sweep gaps."""
        with self.db.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sweep_deficits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand TEXT NOT NULL,
                    payment_ref TEXT NOT NULL,
                    amount REAL NOT NULL,
                    currency TEXT DEFAULT 'EUR',
                    sweep_id INTEGER,
                    status TEXT DEFAULT 'outstanding',
                    resolved_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

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

                CREATE TABLE IF NOT EXISTS webhook_dead_letter (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    raw_payload TEXT NOT NULL,
                    headers TEXT,
                    error TEXT,
                    retry_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_retry_at TIMESTAMP
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

        # Record sweep in GL
        if result.success and result.amount_crypto > 0 and self.ledger:
            try:
                self.ledger.record_sweep(
                    amount=result.amount_crypto,
                    from_account="1050",  # Cash - Monero
                    description=f"Sweep to creator: {brand} ({result.tx_hash or 'n/a'})",
                    reference=result.tx_hash or "",
                    source="sweep_engine",
                    brand=brand,
                )
            except Exception as e:
                logger.error(f"Failed to record GL sweep for {brand}: {e}")

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

        # Validate webhook amount — raise so webhook server returns 500 and saves to DLQ
        if event.amount < 0:
            raise ValueError(f"Webhook with negative amount rejected: {event.amount}")
        if event.amount == 0 and event.event_type == WebhookEventType.PAYMENT_COMPLETED:
            raise ValueError("Webhook with zero amount rejected for payment.completed")
        if event.amount > 1_000_000:  # €1M safety cap per single webhook
            raise ValueError(f"Webhook with suspicious amount rejected: {event.amount}")
        # NaN check
        if event.amount != event.amount:
            raise ValueError("Webhook with NaN amount rejected")

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

        # Use Decimal for precise fee calculation, round to 2 decimals before storage
        gross_dec = Decimal(str(event.amount))
        rates = self.brand_payments.PLATFORM_FEE_RATES.get(event.provider)
        if rates:
            fee_dec = gross_dec * Decimal(str(rates["rate"])) + Decimal(str(rates["fixed"]))
        else:
            fee_dec = Decimal("0")
        # Round to 2 decimal places for storage (avoids floating-point drift)
        amount_rounded = float(gross_dec.quantize(Decimal("0.01")))
        fee_amount = float(fee_dec.quantize(Decimal("0.01")))
        fee_currency = event.currency  # Fee in same currency as payment

        # Record payment + fee atomically in a single transaction
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO brand_payments_received "
                "(brand, account_id, lead_id, amount, currency, product, "
                "customer_email, payment_ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (brand, account_id, lead_id, amount_rounded, event.currency,
                 event.product, event.customer_email, event.payment_ref),
            )
            pay_id = cursor.lastrowid

            conn.execute(
                "INSERT INTO platform_fees "
                "(brand, provider, payment_id, gross_amount, fee_amount, fee_currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (brand, event.provider, pay_id, amount_rounded, fee_amount, fee_currency),
            )

        logger.info(
            f"Payment recorded: {brand} received {event.amount} {event.currency} "
            f"via {event.provider} for '{event.product}' (fee: {fee_amount:.2f} {fee_currency})"
        )

        # Record in double-entry ledger
        self._record_payment_gl(brand, event, fee_amount)

    def _record_payment_gl(self, brand: str, event: WebhookEvent,
                           fee_amount: float) -> None:
        """Record a payment in the general ledger (double-entry)."""
        if not self.ledger:
            return
        # Round to 2 decimal places to avoid floating-point balance mismatches
        fee_amount = round(fee_amount, 2)
        try:
            # Map provider to cash account
            cash_accounts = {
                "stripe": "1010",
                "gumroad": "1020",
                "lemonsqueezy": "1030",
                "btcpay": "1040",
                "crypto_xmr": "1050",
            }
            cash_acct = cash_accounts.get(event.provider, "1000")

            # Determine revenue account based on product type
            revenue_acct = "4900"  # Default: other revenue
            product = (event.product or "").lower()
            if any(w in product for w in ("service", "freelance", "consult")):
                revenue_acct = "4000"
            elif any(w in product for w in ("ebook", "template", "digital", "download")):
                revenue_acct = "4100"
            elif any(w in product for w in ("subscription", "saas", "plan")):
                revenue_acct = "4200"
            elif any(w in product for w in ("affiliate", "referral")):
                revenue_acct = "4300"

            if fee_amount > 0:
                self.ledger.record_platform_fee(
                    gross=event.amount,
                    fee=fee_amount,
                    revenue_account=revenue_acct,
                    cash_account=cash_acct,
                    description=f"Payment via {event.provider}: {event.product or 'sale'}",
                    currency=event.currency,
                    reference=event.payment_ref,
                    source="webhook",
                    brand=brand,
                )
            else:
                self.ledger.record_revenue(
                    amount=event.amount,
                    revenue_account=revenue_acct,
                    cash_account=cash_acct,
                    description=f"Payment via {event.provider}: {event.product or 'sale'}",
                    currency=event.currency,
                    reference=event.payment_ref,
                    source="webhook",
                    brand=brand,
                )
        except Exception as e:
            logger.error(f"Failed to record GL entry for payment {event.payment_ref}: {e}")

    async def _handle_payment_refunded(self, event: WebhookEvent) -> None:
        """Handle a refund — mark payment and check if sweep already occurred.

        Acquires the per-brand lock so refunds cannot race with an in-progress sweep.
        """
        from monai.payments.sweep_engine import _get_brand_lock

        # Determine brand from the original payment
        pre_payments = self.db.execute(
            "SELECT brand FROM brand_payments_received WHERE payment_ref = ? LIMIT 1",
            (event.payment_ref,),
        )
        brand_for_lock = dict(pre_payments[0])["brand"] if pre_payments else ""
        lock = _get_brand_lock(brand_for_lock) if brand_for_lock else None

        if lock:
            async with lock:
                await self._handle_refund_inner(event)
        else:
            await self._handle_refund_inner(event)

    async def _handle_refund_inner(self, event: WebhookEvent) -> None:
        """Refund logic — must be called under brand lock when brand is known."""
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
            # Track the deficit so it can be recovered from future sweeps
            try:
                self._ensure_deficit_table()
                self.db.execute_insert(
                    "INSERT INTO sweep_deficits "
                    "(brand, payment_ref, amount, currency, sweep_id, status) "
                    "VALUES (?, ?, ?, ?, ?, 'outstanding')",
                    (payment["brand"], event.payment_ref,
                     payment["amount"], payment["currency"],
                     swept[0]["id"]),
                )
            except Exception as e:
                logger.error(f"Failed to record sweep deficit: {e}")

            logger.critical(
                f"REFUND AFTER SWEEP: Payment {event.payment_ref} for "
                f"{payment['amount']} {payment['currency']} was already swept. "
                f"Brand {payment['brand']} has NEGATIVE sweepable balance. "
                f"Deficit tracked for recovery."
            )

        # Record refund in GL (reverse the original entry)
        if self.ledger and payment:
            try:
                cash_accounts = {
                    "stripe": "1010", "gumroad": "1020",
                    "lemonsqueezy": "1030", "btcpay": "1040", "crypto_xmr": "1050",
                }
                cash_acct = cash_accounts.get(event.provider, "1000")
                self.ledger.record_entry(
                    date=__import__("datetime").datetime.now().strftime("%Y-%m-%d"),
                    description=f"Refund: {event.payment_ref}",
                    lines=[
                        {"account_code": "4900", "debit": payment["amount"],
                         "currency": payment["currency"], "memo": "Refund reversal"},
                        {"account_code": cash_acct, "credit": payment["amount"],
                         "currency": payment["currency"]},
                    ],
                    reference=event.payment_ref,
                    source="webhook_refund",
                    brand=payment["brand"],
                )
            except Exception as e:
                logger.error(f"Failed to record GL refund for {event.payment_ref}: {e}")

        logger.info(f"Payment refunded: {event.payment_ref}")

    async def _handle_payment_disputed(self, event: WebhookEvent) -> None:
        """Handle a dispute — mark and alert."""
        payments = self.db.execute(
            "SELECT id, brand, amount, currency, status FROM brand_payments_received "
            "WHERE payment_ref = ? LIMIT 1",
            (event.payment_ref,),
        )

        # Defense-in-depth: skip if already disputed
        if payments and dict(payments[0]).get("status") == "disputed":
            logger.info(f"Payment {event.payment_ref} already disputed — skipping")
            return

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

    # ── Webhook Replay ─────────────────────────────────────────

    async def replay_webhook(self, event_id: int) -> dict[str, Any]:
        """Replay a single webhook event by its ID from the webhook_events table.

        Re-processes the stored raw_payload through the normal handler pipeline.
        Idempotency is bypassed by removing the processed_webhooks entry first.
        """
        rows = self.db.execute(
            "SELECT * FROM webhook_events WHERE id = ?", (event_id,)
        )
        if not rows:
            return {"success": False, "error": f"Event {event_id} not found"}

        row = dict(rows[0])
        raw_payload = row.get("raw_payload")
        if not raw_payload:
            return {"success": False, "error": "No raw_payload stored for this event"}

        try:
            raw = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        except json.JSONDecodeError:
            return {"success": False, "error": "Corrupted raw_payload"}

        # Remove idempotency lock so re-processing can proceed
        idem_id = f"{row['payment_ref']}:{row['event_type']}"
        self.db.execute(
            "DELETE FROM processed_webhooks WHERE provider = ? AND event_id = ?",
            (row["provider"], idem_id),
        )
        # Remove original webhook_events entry (handler will re-insert)
        self.db.execute("DELETE FROM webhook_events WHERE id = ?", (event_id,))

        # Reconstruct the WebhookEvent
        try:
            event_type = WebhookEventType(row["event_type"])
        except ValueError:
            return {"success": False, "error": f"Unknown event type: {row['event_type']}"}

        event = WebhookEvent(
            event_type=event_type,
            provider=row["provider"],
            payment_ref=row["payment_ref"],
            amount=float(row.get("amount", 0)),
            currency=row.get("currency", "EUR"),
            metadata={"brand": row.get("brand", "")},
            raw=raw,
        )

        try:
            await self._handle_webhook_event(event)
            logger.info(f"Webhook replayed: event_id={event_id} ref={row['payment_ref']}")
            return {"success": True, "payment_ref": row["payment_ref"]}
        except Exception as e:
            logger.error(f"Webhook replay failed for event_id={event_id}: {e}")
            return {"success": False, "error": str(e)}

    async def replay_failed_webhooks(
        self, since_hours: int = 24, limit: int = 100
    ) -> dict[str, Any]:
        """Replay webhook events that have errors recorded.

        Returns summary of replay results.
        """
        rows = self.db.execute(
            "SELECT id FROM webhook_events "
            "WHERE error IS NOT NULL AND error != '' "
            "AND created_at >= datetime('now', ?) "
            "ORDER BY created_at ASC LIMIT ?",
            (f"-{since_hours} hours", limit),
        )

        results = {"total": len(rows), "succeeded": 0, "failed": 0, "errors": []}
        for row in rows:
            result = await self.replay_webhook(row["id"])
            if result["success"]:
                results["succeeded"] += 1
            else:
                results["failed"] += 1
                results["errors"].append(
                    {"event_id": row["id"], "error": result.get("error", "")}
                )

        logger.info(
            f"Webhook replay batch: {results['succeeded']}/{results['total']} succeeded"
        )
        return results

    def get_replayable_webhooks(self, limit: int = 50) -> list[dict]:
        """List webhook events available for replay."""
        rows = self.db.execute(
            "SELECT id, provider, event_type, payment_ref, amount, currency, "
            "brand, status, error, created_at FROM webhook_events "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]
