"""Sweep Engine — automated profit transfer from brands to creator.

Supports two flows:

    Flow A (LLC + Contractor — primary, no crypto required):
        1. Platforms (Stripe/Gumroad/LS) auto-payout to LLC bank account
        2. Sweep engine tracks these platform payouts
        3. Generates monthly contractor invoice (creator bills the LLC)
        4. LLC pays contractor invoice via bank transfer
        5. Money arrives in creator's personal account

        Mixed strategy: LLC also buys things for the creator (expenses).
        Revenue is split: part as contractor invoices (P.IVA), part as LLC expenses.

        Multi-LLC: if config.llc.multi_llc=True, invoices rotate across entities
        to avoid suspicious single-client pattern.

    Flow B (Crypto — optional, for maximum anonymity):
        1. Check brand crypto wallet balance
        2. Send XMR to creator's wallet directly
        3. Track with tx hash

The flow is selected based on config: if LLC is configured, use Flow A.
If crypto wallets are configured, use Flow B. Both can coexist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from monai.business.brand_payments import BrandPayments
from monai.config import Config
from monai.db.database import Database
from monai.payments.types import (
    PaymentStatus,
    SweepRequest,
    SweepResult,
    SweepStatus,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 5  # seconds

# Per-brand locks to prevent concurrent sweeps/refunds from racing
_brand_locks: dict[str, asyncio.Lock] = {}
_brand_locks_mutex = threading.Lock()


def _get_brand_lock(brand: str) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a brand (thread-safe creation)."""
    with _brand_locks_mutex:
        if brand not in _brand_locks:
            _brand_locks[brand] = asyncio.Lock()
        return _brand_locks[brand]


class SweepEngine:
    """Orchestrates profit sweeps from brand accounts to creator.

    Adapts to configured payout method:
    - LLC mode: tracks platform payouts, generates contractor invoices
    - Crypto mode: sends XMR from brand wallet to creator wallet
    """

    # Sweeps above this EUR threshold trigger a Telegram notification to creator
    CONFIRMATION_THRESHOLD_EUR = 50.0

    def __init__(self, config: Config, db: Database, telegram_bot=None):
        self.config = config
        self.db = db
        self.brand_payments = BrandPayments(db)
        self.telegram_bot = telegram_bot  # TelegramBot for sweep confirmations
        self._monero = None  # Lazy-loaded only if crypto is configured

        # Lazy import corporate module
        self._corporate = None

    @property
    def corporate(self):
        if self._corporate is None:
            from monai.business.corporate import CorporateManager
            self._corporate = CorporateManager(self.db)
        return self._corporate

    @property
    def monero(self):
        if self._monero is None and self.config.monero.wallet_rpc_url:
            from monai.payments.monero_provider import MoneroProvider
            self._monero = MoneroProvider(
                wallet_rpc_url=self.config.monero.wallet_rpc_url,
                rpc_user=self.config.monero.rpc_user,
                rpc_password=self.config.monero.rpc_password,
                min_confirmations=self.config.creator_wallet.min_confirmations_xmr,
                proxy_url=self.config.monero.proxy_url,
            )
        return self._monero

    @property
    def retoswap(self):
        if not hasattr(self, "_retoswap") or self._retoswap is None:
            self._retoswap = None
            if getattr(self.config, "retoswap", None) and self.config.retoswap.enabled:
                from monai.payments.retoswap_provider import RetoSwapClient
                self._retoswap = RetoSwapClient(
                    daemon_host=self.config.retoswap.daemon_host,
                    daemon_port=self.config.retoswap.daemon_port,
                    daemon_password=self.config.retoswap.daemon_password,
                    preferred_payment_method=self.config.retoswap.preferred_payment_method,
                    preferred_currency=self.config.retoswap.preferred_currency,
                    price_margin_pct=self.config.retoswap.price_margin_pct,
                    fallback_payment_methods=self.config.retoswap.fallback_payment_methods,
                )
        return self._retoswap

    def get_active_flow(self) -> str:
        """Determine which payout flow is active.

        Priority: RetoSwap (P2P, no KYC) > direct XMR > LLC (requires bank).
        RetoSwap is preferred because it converts XMR → PayPal F&F or cash
        without needing a bank account or LLC setup.
        """
        # RetoSwap: XMR → fiat via P2P exchange (no KYC, anonymous cash-out)
        if getattr(self.config, "retoswap", None) and self.config.retoswap.enabled:
            return "crypto_retoswap"

        # Direct XMR transfer to creator wallet
        if self.config.creator_wallet.xmr_address:
            return "crypto_xmr"

        # LLC mode — requires bank account, most bureaucracy
        entities = self.corporate.get_all_entities()
        for entity in entities:
            contractor = self.corporate.get_active_contractor(entity["id"])
            if contractor:
                return "llc_contractor"

        return "none"

    def _get_invoice_target_entity(self) -> dict[str, Any] | None:
        """Pick which LLC to invoice next (rotation for multi-LLC).

        If multi_llc is enabled, rotates across entities to avoid
        always invoicing the same one. Uses round-robin based on
        recent invoice counts.
        """
        entities = self.corporate.get_all_entities()
        if not entities:
            return None

        if not getattr(self.config, 'llc', None) or not self.config.llc.multi_llc:
            # Single LLC mode — use primary
            return self.corporate.get_primary_entity()

        # Multi-LLC rotation: pick the entity with fewest recent invoices
        entity_scores = []
        for entity in entities:
            contractor = self.corporate.get_active_contractor(entity["id"])
            if not contractor:
                continue
            recent_invoices = self.db.execute(
                "SELECT COUNT(*) as cnt FROM contractor_invoices "
                "WHERE entity_id = ? AND created_at > datetime('now', '-6 months')",
                (entity["id"],),
            )
            count = recent_invoices[0]["cnt"] if recent_invoices else 0
            entity_scores.append((count, entity))

        if not entity_scores:
            return self.corporate.get_primary_entity()

        # Pick the one with fewest recent invoices
        entity_scores.sort(key=lambda x: x[0])
        return entity_scores[0][1]

    # ── LLC + Contractor Flow ──────────────────────────────────

    async def run_sweep_cycle(self) -> dict[str, Any]:
        """Run a full sweep cycle. Adapts to configured flow."""
        flow = self.get_active_flow()

        if flow == "llc_contractor":
            return await self._run_llc_sweep_cycle()
        elif flow == "crypto_retoswap":
            return await self._run_retoswap_sweep_cycle()
        elif flow == "crypto_xmr":
            return await self._run_crypto_sweep_cycle()
        else:
            return {
                "status": "skipped",
                "reason": "no_payout_method_configured",
                "hint": "Configure LLC, RetoSwap, or set creator_wallet.xmr_address",
            }

    async def _run_llc_sweep_cycle(self) -> dict[str, Any]:
        """LLC flow: track platform payouts and generate contractor invoices.

        This doesn't move money — it tracks what platforms already moved
        and generates invoices for the contractor payment.

        Mixed strategy: revenue splits between LLC expenses (tax-free for
        creator) and contractor invoices (P.IVA income).
        Multi-LLC: rotates invoice target across entities.
        """
        # Pick which entity to invoice (rotation if multi-LLC)
        entity = self._get_invoice_target_entity()
        if not entity:
            return {"status": "error", "reason": "no_entity_with_contractor"}

        contractor = self.corporate.get_active_contractor(entity["id"])
        if not contractor:
            return {"status": "error", "reason": "no_active_contractor"}

        # Aggregate revenue across ALL entities (not just the target)
        all_entities = self.corporate.get_all_entities()
        all_revenue = self.brand_payments.get_all_brands_revenue()
        brand_revenues = []

        for brand_data in all_revenue:
            brand = brand_data["brand"]
            brand_entity = self.corporate.get_brand_entity(brand)
            if not brand_entity:
                continue
            # Only count brands belonging to any of our entities
            if brand_entity["id"] not in {e["id"] for e in all_entities}:
                continue

            revenue = brand_data["total_revenue"]
            if revenue > 0:
                brand_revenues.append({
                    "brand": brand,
                    "revenue": revenue,
                    "transactions": brand_data["transactions"],
                    "entity_id": brand_entity["id"],
                })

        # Expense summary
        total_expenses = sum(
            self.corporate.get_expense_total(e["id"]) for e in all_entities
        )
        recurring_expenses = []
        for e in all_entities:
            recurring_expenses.extend(self.corporate.get_recurring_expenses(e["id"]))

        # Check if it's time to generate a contractor invoice
        invoice_result = self._maybe_generate_invoice(
            contractor, entity, brand_revenues
        )

        # Check overdue tax obligations
        overdue = self.corporate.get_overdue_obligations()

        result: dict[str, Any] = {
            "flow": "llc_contractor",
            "entity": entity["name"],
            "contractor": contractor["alias"],
            "brands_tracked": len(brand_revenues),
            "total_revenue": sum(br["revenue"] for br in brand_revenues),
            "total_expenses_via_llc": total_expenses,
            "recurring_expenses": len(recurring_expenses),
            "invoice": invoice_result,
            "status": "ok",
        }

        if len(all_entities) > 1:
            result["multi_llc"] = True
            result["entities_count"] = len(all_entities)

        if overdue:
            result["overdue_tax_obligations"] = len(overdue)
            result["overdue_details"] = [
                {"type": o["obligation_type"], "due": o["due_date"],
                 "jurisdiction": o["jurisdiction"]}
                for o in overdue
            ]

        return result

    def _maybe_generate_invoice(self, contractor: dict, entity: dict,
                                brand_revenues: list[dict]) -> dict[str, Any]:
        """Generate a contractor invoice if we're in a new billing period."""
        now = datetime.now()

        # Check last invoice date
        last_invoices = self.db.execute(
            "SELECT * FROM contractor_invoices "
            "WHERE contractor_id = ? AND entity_id = ? "
            "ORDER BY period_end DESC LIMIT 1",
            (contractor["id"], entity["id"]),
        )

        if last_invoices:
            last = dict(last_invoices[0])
            last_end = datetime.strptime(last["period_end"], "%Y-%m-%d")

            # Only generate if the last period ended at least a month ago
            if now - last_end < timedelta(days=28):
                return {
                    "status": "not_due",
                    "last_invoice": last["invoice_number"],
                    "last_period_end": last["period_end"],
                    "next_due": (last_end + timedelta(days=28)).strftime("%Y-%m-%d"),
                }

            period_start = (last_end + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            # First invoice: period starts at entity formation or 30 days ago
            period_start = (now - timedelta(days=30)).strftime("%Y-%m-%d")

        period_end = now.strftime("%Y-%m-%d")

        # Generate the invoice
        invoice = self.corporate.generate_invoice(
            contractor_id=contractor["id"],
            entity_id=entity["id"],
            period_start=period_start,
            period_end=period_end,
            brand_revenues=brand_revenues,
        )

        if invoice.get("error"):
            return {"status": "no_revenue", "detail": invoice["error"]}

        return {
            "status": "generated",
            "invoice_number": invoice.get("invoice_number", ""),
            "amount": invoice.get("amount", 0),
            "period": invoice.get("period", ""),
        }

    async def sweep_brand(self, brand: str,
                          amount: float | None = None) -> SweepResult:
        """Manually trigger a sweep for a specific brand."""
        # Validate amount if provided
        if amount is not None and amount <= 0:
            return SweepResult(
                success=False,
                error=f"Invalid sweep amount: {amount} (must be positive)",
                status=SweepStatus.FAILED,
            )

        flow = self.get_active_flow()

        if flow == "llc_contractor":
            # In LLC mode, "sweep" means tracking the payout and invoicing
            return SweepResult(
                success=True,
                status=SweepStatus.COMPLETED,
                metadata={
                    "flow": "llc_contractor",
                    "note": "Platform payouts are automatic. "
                            "Use contractor invoicing to collect.",
                },
            )

        if flow == "crypto_xmr":
            return await self._crypto_sweep_brand(brand, amount)

        return SweepResult(
            success=False,
            error="No payout method configured",
            status=SweepStatus.FAILED,
        )

    # ── Crypto Flow (Monero) ───────────────────────────────────

    async def _run_retoswap_sweep_cycle(self) -> dict[str, Any]:
        """RetoSwap flow: sell XMR on P2P exchange for fiat (EUR/cash).

        Flow: Brand XMR wallet → RetoSwap P2P trade → fiat to creator's bank/PayPal.
        No KYC. Fully decentralized via Haveno protocol.
        """
        if not self.monero:
            return {"status": "error", "reason": "monero_not_configured"}
        if not self.retoswap:
            return {"status": "error", "reason": "retoswap_not_configured"}

        if not await self.monero.health_check():
            return {"status": "error", "reason": "monero_offline"}
        if not await self.retoswap.health_check():
            logger.warning("RetoSwap daemon unreachable — skipping conversion cycle")
            return {"status": "error", "reason": "retoswap_offline"}

        threshold = self.config.creator_wallet.sweep_threshold_eur
        results = []
        all_revenue = self.brand_payments.get_all_brands_revenue()

        for brand_data in all_revenue:
            brand = brand_data["brand"]
            sweepable = self.brand_payments.get_sweepable_balance(brand)
            if sweepable < threshold:
                continue

            lock = _get_brand_lock(brand)
            async with lock:
                # Get brand wallet balance in XMR
                balance = await self.monero.get_balance(brand)
                xmr_available = balance.available
                if xmr_available < 0.01:
                    continue

                # Sell XMR for fiat via P2P exchange
                logger.info(
                    f"RetoSwap: selling {xmr_available:.4f} XMR from brand '{brand}' "
                    f"(~€{sweepable:.2f})"
                )
                trade_result = await self.retoswap.auto_sell_xmr(
                    amount_xmr=xmr_available,
                )

                results.append({
                    "brand": brand,
                    "amount_xmr": xmr_available,
                    "trade_id": trade_result.trade_id,
                    "status": trade_result.status.value,
                    "amount_fiat": trade_result.amount_fiat,
                    "error": trade_result.error,
                })

                # Notify creator
                if self.telegram_bot and trade_result.trade_id:
                    try:
                        self.telegram_bot.notify_creator(
                            f"💱 *RetoSwap trade initiated*\n\n"
                            f"Brand: `{brand}`\n"
                            f"Selling: {xmr_available:.4f} XMR\n"
                            f"Expected: ~€{trade_result.amount_fiat:.2f}\n"
                            f"Method: {self.config.retoswap.preferred_payment_method}\n"
                            f"Trade: `{trade_result.trade_id[:16]}...`\n\n"
                            f"Fiat will arrive in your account when trade completes."
                        )
                    except Exception:
                        pass

        successful = sum(1 for r in results if r.get("trade_id"))
        return {
            "flow": "crypto_retoswap",
            "trades_initiated": len(results),
            "trades_successful": successful,
            "results": results,
            "status": "ok",
        }

    async def _run_crypto_sweep_cycle(self) -> dict[str, Any]:
        """Crypto flow: send XMR from brand wallets to creator."""
        if not self.monero:
            return {"status": "error", "reason": "monero_not_configured"}

        if not await self.monero.health_check():
            logger.error("Monero wallet RPC unreachable — skipping sweep cycle")
            return {"status": "error", "reason": "monero_offline"}

        creator_address = self.config.creator_wallet.xmr_address
        if not creator_address:
            # No creator address yet — notify via Telegram and hold funds
            if self.telegram_bot:
                try:
                    self.telegram_bot.notify_creator(
                        "💰 *Funds ready for sweep* but no XMR address configured.\n\n"
                        "Send me your Monero address to start receiving payouts.\n"
                        "Use: `/set_wallet <your_xmr_address>`"
                    )
                except Exception:
                    pass
            logger.info("No creator XMR address — holding funds until configured")
            return {"status": "holding", "reason": "awaiting_creator_xmr_address"}

        threshold = self.config.creator_wallet.sweep_threshold_eur
        results: list[SweepResult] = []
        all_revenue = self.brand_payments.get_all_brands_revenue()

        for brand_data in all_revenue:
            brand = brand_data["brand"]
            sweepable = self.brand_payments.get_sweepable_balance(brand)

            if sweepable < threshold:
                continue

            from_account = self._find_sweep_source(brand)
            if not from_account:
                continue

            result = await self._crypto_sweep_brand(brand, sweepable)
            results.append(result)

        completed = sum(1 for r in results if r.success)
        total_swept = sum(r.amount_crypto for r in results if r.success)

        return {
            "flow": "crypto_xmr",
            "sweeps_attempted": len(results),
            "sweeps_successful": completed,
            "total_xmr_swept": total_swept,
            "status": "ok",
        }

    async def _crypto_sweep_brand(self, brand: str,
                                  amount: float | None = None) -> SweepResult:
        """Execute a crypto sweep for a single brand.

        Acquires a per-brand lock to prevent race conditions with concurrent
        refund webhooks modifying the same brand's balance mid-sweep.
        """
        lock = _get_brand_lock(brand)
        async with lock:
            return await self._crypto_sweep_brand_locked(brand, amount)

    async def _crypto_sweep_brand_locked(self, brand: str,
                                         amount: float | None = None) -> SweepResult:
        """Actual sweep logic — must be called under brand lock."""
        from monai.payments.monero_provider import MoneroRPCError

        creator_address = self.config.creator_wallet.xmr_address
        if not creator_address:
            return SweepResult(
                success=False, error="No creator XMR address configured",
                status=SweepStatus.FAILED,
            )

        sweepable = amount or self.brand_payments.get_sweepable_balance(brand)
        if sweepable <= 0:
            return SweepResult(
                success=False, error=f"Nothing to sweep for brand {brand}",
                status=SweepStatus.FAILED,
            )

        # Deduct outstanding deficits (refunds after previous sweeps)
        try:
            deficits = self.db.execute(
                "SELECT COALESCE(SUM(amount), 0) as total FROM sweep_deficits "
                "WHERE brand = ? AND status = 'outstanding'",
                (brand,),
            )
            deficit_total = deficits[0]["total"] if deficits else 0
            if deficit_total > 0:
                sweepable -= deficit_total
                logger.info(
                    f"Brand {brand}: deducting {deficit_total:.2f} deficit from sweepable"
                )
                if sweepable <= 0:
                    return SweepResult(
                        success=False,
                        error=f"Sweepable balance ({sweepable + deficit_total:.2f}) "
                              f"consumed by deficit ({deficit_total:.2f})",
                        status=SweepStatus.FAILED,
                    )
        except Exception:
            pass  # Table may not exist yet

        from_account = self._find_sweep_source(brand)
        if not from_account:
            return SweepResult(
                success=False, error=f"No crypto account for brand {brand}",
                status=SweepStatus.FAILED,
            )

        if not self.monero:
            return SweepResult(
                success=False, error="Monero provider not configured",
                status=SweepStatus.FAILED,
            )

        to_account = self._ensure_sweep_destination(brand, creator_address)

        sweep_id = self.brand_payments.initiate_sweep(
            brand=brand,
            from_account_id=from_account["id"],
            to_account_id=to_account["id"],
            amount=sweepable,
            sweep_method="crypto_xmr",
        )

        # Get available XMR
        balance = await self.monero.get_balance()
        if balance.available <= 0:
            self.brand_payments.fail_sweep(sweep_id, "No XMR available")
            return SweepResult(
                success=False, sweep_id=sweep_id,
                error="No XMR available in wallet",
                status=SweepStatus.FAILED,
            )

        xmr_to_send = balance.available
        fee_estimate = await self.monero.estimate_fee(xmr_to_send)
        xmr_to_send -= fee_estimate

        if xmr_to_send <= 0:
            self.brand_payments.fail_sweep(sweep_id, "Balance too low after fee")
            return SweepResult(
                success=False, sweep_id=sweep_id,
                error="Balance too low after fee estimate",
                status=SweepStatus.FAILED,
            )

        # Attempt send with retries
        last_error = ""
        for attempt in range(MAX_RETRIES):
            try:
                result = await self.monero.send_payout(
                    to_address=creator_address,
                    amount=xmr_to_send,
                    priority="normal",
                )

                if result.success:
                    tx_hash = result.payment_ref
                    fee = result.raw.get("fee", 0)
                    self.brand_payments.complete_sweep(sweep_id, tx_reference=tx_hash)

                    # Notify creator (non-blocking — sweep already done)
                    if self.telegram_bot and sweepable >= self.CONFIRMATION_THRESHOLD_EUR:
                        try:
                            self.telegram_bot.notify_creator(
                                f"💰 *Sweep completed*\n\n"
                                f"Brand: `{brand}`\n"
                                f"Amount: {xmr_to_send:.8f} XMR (~€{sweepable:.2f})\n"
                                f"Fee: {fee:.8f} XMR\n"
                                f"TX: `{tx_hash[:16]}...`"
                            )
                        except Exception:
                            pass  # Notification failure must never block sweep result

                    return SweepResult(
                        success=True,
                        sweep_id=sweep_id,
                        tx_hash=tx_hash,
                        status=SweepStatus.COMPLETED,
                        amount_crypto=xmr_to_send,
                        fee=fee,
                        metadata=result.raw,
                    )
                else:
                    last_error = result.error

            except MoneroRPCError as e:
                last_error = str(e)
                logger.warning(f"Sweep attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")

            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                await asyncio.sleep(wait)

        self.brand_payments.fail_sweep(sweep_id, last_error)
        return SweepResult(
            success=False,
            sweep_id=sweep_id,
            error=f"Failed after {MAX_RETRIES} attempts: {last_error}",
            status=SweepStatus.FAILED,
        )

    async def check_pending_sweeps(self) -> list[dict[str, Any]]:
        """Check status of pending crypto sweeps."""
        if not self.monero:
            return []

        from monai.payments.monero_provider import MoneroRPCError

        pending = self.db.execute(
            "SELECT * FROM brand_profit_sweeps WHERE status IN ('pending', 'mixing')"
        )
        results = []
        for sweep in pending:
            sweep = dict(sweep)
            tx_ref = sweep.get("tx_reference", "")
            if not tx_ref:
                continue

            try:
                confirmations = await self.monero.get_tx_confirmations(tx_ref)
                if confirmations >= self.config.creator_wallet.min_confirmations_xmr:
                    self.brand_payments.complete_sweep(sweep["id"], tx_ref)
                    sweep["status"] = "completed"
                sweep["confirmations"] = confirmations
            except MoneroRPCError as e:
                sweep["error"] = str(e)

            results.append(sweep)

        return results

    # ── Helpers ─────────────────────────────────────────────────

    def _find_sweep_source(self, brand: str) -> dict[str, Any] | None:
        """Find the best crypto collection account for sweeping."""
        accounts = self.brand_payments.get_collection_accounts(brand)
        xmr = [a for a in accounts if a["provider"] == "crypto_xmr"]
        if xmr:
            return xmr[0]
        btc = [a for a in accounts if a["provider"] == "crypto_btc"]
        if btc:
            return btc[0]
        return None

    def _ensure_sweep_destination(self, brand: str,
                                  creator_address: str) -> dict[str, Any]:
        """Ensure a sweep destination account exists for the brand."""
        existing = self.brand_payments.get_sweep_accounts(brand)
        for acc in existing:
            if acc["account_id"] == creator_address:
                return acc

        acc_id = self.brand_payments.add_sweep_account(
            brand=brand,
            provider="crypto_xmr",
            account_id=creator_address,
            label="creator_wallet",
        )
        return {
            "id": acc_id,
            "account_id": creator_address,
            "provider": "crypto_xmr",
        }

    def get_sweep_summary(self) -> dict[str, Any]:
        """Get overview of sweep status across all brands."""
        flow = self.get_active_flow()
        total_swept = self.brand_payments.get_total_swept()
        history = self.brand_payments.get_sweep_history(limit=10)
        pending_count = len([
            s for s in history if s["status"] in ("pending", "mixing")
        ])

        summary: dict[str, Any] = {
            "active_flow": flow,
            "total_swept_eur": total_swept,
            "recent_sweeps": len(history),
            "pending_sweeps": pending_count,
            "sweep_threshold_eur": self.config.creator_wallet.sweep_threshold_eur,
        }

        if flow == "llc_contractor":
            entities = self.corporate.get_all_entities()
            entity = self.corporate.get_primary_entity()
            if entity:
                contractor = self.corporate.get_active_contractor(entity["id"])
                summary["llc_name"] = entity["name"]
                summary["contractor_alias"] = contractor["alias"] if contractor else "NOT SET"
                summary["total_paid_to_contractor"] = self.corporate.get_total_paid_to_contractor()
                summary["total_expenses_via_llc"] = sum(
                    self.corporate.get_expense_total(e["id"]) for e in entities
                )
                summary["entities_count"] = len(entities)
                overdue = self.corporate.get_overdue_obligations()
                if overdue:
                    summary["overdue_tax_obligations"] = len(overdue)
        elif flow == "crypto_xmr":
            addr = self.config.creator_wallet.xmr_address
            summary["creator_xmr_address"] = addr[:12] + "..." if addr else "NOT SET"

        return summary
