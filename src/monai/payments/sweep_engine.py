"""Sweep Engine — automated profit transfer from brands to creator.

The sweep engine periodically checks brand balances and transfers
profits to the creator's anonymous wallet via Monero.

Flow:
    1. Check sweepable balance per brand (received - already swept)
    2. If balance > threshold, initiate sweep
    3. Convert to XMR if source is fiat (via brand's crypto holdings)
    4. Send XMR to creator's wallet
    5. Track the sweep in DB with tx hash
    6. Retry on failure with exponential backoff
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from monai.business.brand_payments import BrandPayments
from monai.config import Config
from monai.db.database import Database
from monai.payments.monero_provider import MoneroProvider, MoneroRPCError
from monai.payments.types import (
    PaymentStatus,
    SweepRequest,
    SweepResult,
    SweepStatus,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 5  # seconds


class SweepEngine:
    """Orchestrates profit sweeps from brand accounts to creator wallet."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.brand_payments = BrandPayments(db)
        self.monero = MoneroProvider(
            wallet_rpc_url=config.monero.wallet_rpc_url,
            rpc_user=config.monero.rpc_user,
            rpc_password=config.monero.rpc_password,
            min_confirmations=config.creator_wallet.min_confirmations_xmr,
            proxy_url=config.monero.proxy_url,
        )

    async def run_sweep_cycle(self) -> list[SweepResult]:
        """Run a full sweep cycle across all brands.

        Called periodically by the orchestrator (every sweep_interval_hours).
        Returns list of sweep results.
        """
        results: list[SweepResult] = []

        # Pre-check: is Monero wallet available?
        if not await self.monero.health_check():
            logger.error("Monero wallet RPC unreachable — skipping sweep cycle")
            return results

        creator_address = self.config.creator_wallet.xmr_address
        if not creator_address:
            logger.error("No creator XMR address configured — skipping sweep cycle")
            return results

        threshold = self.config.creator_wallet.sweep_threshold_eur

        # Get all brands with revenue
        all_revenue = self.brand_payments.get_all_brands_revenue()

        for brand_data in all_revenue:
            brand = brand_data["brand"]
            sweepable = self.brand_payments.get_sweepable_balance(brand)

            if sweepable < threshold:
                logger.debug(
                    f"Sweep skip: {brand} has €{sweepable:.2f} "
                    f"(threshold: €{threshold:.2f})"
                )
                continue

            # Find the brand's crypto collection account (XMR preferred)
            from_account = self._find_sweep_source(brand)
            if not from_account:
                logger.warning(f"No sweepable crypto account for brand {brand}")
                continue

            # Find or create sweep destination account
            to_account = self._ensure_sweep_destination(brand, creator_address)

            result = await self._execute_sweep(
                SweepRequest(
                    brand=brand,
                    from_account_id=from_account["id"],
                    to_address=creator_address,
                    amount=sweepable,
                    currency="EUR",
                    method="crypto_xmr",
                )
            )
            results.append(result)

        if results:
            completed = sum(1 for r in results if r.success)
            total_swept = sum(r.amount_crypto for r in results if r.success)
            logger.info(
                f"Sweep cycle complete: {completed}/{len(results)} successful, "
                f"total {total_swept:.8f} XMR swept"
            )

        return results

    async def _execute_sweep(self, request: SweepRequest) -> SweepResult:
        """Execute a single sweep with retry logic."""
        # Record the sweep initiation in DB
        from_account = self._find_sweep_source(request.brand)
        to_account = self._ensure_sweep_destination(request.brand, request.to_address)

        sweep_id = self.brand_payments.initiate_sweep(
            brand=request.brand,
            from_account_id=request.from_account_id,
            to_account_id=to_account["id"],
            amount=request.amount,
            sweep_method=request.method,
            metadata=request.metadata,
        )

        # Get XMR amount from the wallet balance
        balance = await self.monero.get_balance()
        if balance.available <= 0:
            self.brand_payments.fail_sweep(sweep_id, "No XMR available in wallet")
            return SweepResult(
                success=False, sweep_id=sweep_id,
                error="No XMR available in wallet",
                status=SweepStatus.FAILED,
            )

        # Determine XMR amount to send
        # If the source is crypto_xmr, use the actual XMR balance
        # (the EUR amount is just for tracking)
        xmr_to_send = min(balance.available, balance.available)  # Send all available
        fee_estimate = await self.monero.estimate_fee(xmr_to_send)
        xmr_to_send -= fee_estimate  # Leave room for fee

        if xmr_to_send <= 0:
            self.brand_payments.fail_sweep(sweep_id, "Balance too low after fee estimate")
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
                    to_address=request.to_address,
                    amount=xmr_to_send,
                    currency="XMR",
                    priority="normal",
                )

                if result.success:
                    tx_hash = result.payment_ref
                    fee = result.raw.get("fee", 0)

                    self.brand_payments.complete_sweep(sweep_id, tx_reference=tx_hash)

                    logger.info(
                        f"Sweep completed: {request.brand} → creator, "
                        f"{xmr_to_send:.8f} XMR, tx={tx_hash[:16]}..."
                    )

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
                logger.warning(
                    f"Sweep attempt {attempt + 1}/{MAX_RETRIES} failed: {e}"
                )

            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.info(f"Retrying sweep in {wait}s...")
                await asyncio.sleep(wait)

        # All retries exhausted
        self.brand_payments.fail_sweep(sweep_id, last_error)
        return SweepResult(
            success=False,
            sweep_id=sweep_id,
            error=f"Failed after {MAX_RETRIES} attempts: {last_error}",
            status=SweepStatus.FAILED,
        )

    async def sweep_brand(self, brand: str,
                          amount: float | None = None) -> SweepResult:
        """Manually trigger a sweep for a specific brand.

        If amount is None, sweeps the full sweepable balance.
        """
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

        from_account = self._find_sweep_source(brand)
        if not from_account:
            return SweepResult(
                success=False, error=f"No crypto account for brand {brand}",
                status=SweepStatus.FAILED,
            )

        return await self._execute_sweep(
            SweepRequest(
                brand=brand,
                from_account_id=from_account["id"],
                to_address=creator_address,
                amount=sweepable,
                method="crypto_xmr",
            )
        )

    async def check_pending_sweeps(self) -> list[dict[str, Any]]:
        """Check status of pending/mixing sweeps and update them."""
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
                    logger.info(
                        f"Sweep {sweep['id']} confirmed: {confirmations} confirmations"
                    )
                else:
                    sweep["confirmations"] = confirmations
            except MoneroRPCError as e:
                sweep["error"] = str(e)

            results.append(sweep)

        return results

    def _find_sweep_source(self, brand: str) -> dict[str, Any] | None:
        """Find the best crypto collection account for sweeping.

        Prefers XMR, falls back to BTC.
        """
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

        # Create one
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
        total_swept = self.brand_payments.get_total_swept()
        history = self.brand_payments.get_sweep_history(limit=10)
        pending_count = len([
            s for s in history if s["status"] in ("pending", "mixing")
        ])

        return {
            "total_swept_eur": total_swept,
            "recent_sweeps": len(history),
            "pending_sweeps": pending_count,
            "creator_xmr_address": self.config.creator_wallet.xmr_address[:12] + "..."
            if self.config.creator_wallet.xmr_address else "NOT SET",
            "sweep_threshold_eur": self.config.creator_wallet.sweep_threshold_eur,
            "sweep_interval_hours": self.config.creator_wallet.sweep_interval_hours,
        }
