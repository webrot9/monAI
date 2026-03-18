"""Finance controller — ledger, reporting, payments, reconciliation, exchange rates."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from monai.business.commercialista import Commercialista
from monai.business.exchange_rates import ExchangeRateService
from monai.business.finance import Finance, GeneralLedger
from monai.business.reconciliation import ReconciliationEngine
from monai.business.reporting import FinancialReporter
from monai.business.spending_guard import SpendingGuard
from monai.config import Config
from monai.db.database import Database
from monai.utils.privacy import get_anonymizer

logger = logging.getLogger(__name__)


class FinanceController:
    """Manages all financial operations: ledger, reporting, payments, reconciliation."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.finance = Finance(db)
        self.ledger = GeneralLedger(db)
        self.commercialista = Commercialista(config, db)
        self.spending_guard = SpendingGuard(db, config)
        self.exchange_rates = ExchangeRateService(
            db, anonymizer=get_anonymizer(config),
        )
        self.reconciliation = ReconciliationEngine(db)
        self._reporter: FinancialReporter | None = None
        self._bootstrap_wallet: Any = None

    def set_bootstrap_wallet(self, wallet: Any) -> None:
        self._bootstrap_wallet = wallet

    @property
    def reporter(self) -> FinancialReporter:
        if self._reporter is None:
            self._reporter = FinancialReporter(
                self.db, self.ledger, self.finance,
                bootstrap=self._bootstrap_wallet,
            )
        return self._reporter

    def get_budget(self) -> dict[str, Any]:
        return self.commercialista.get_budget()

    def refresh_exchange_rates(self) -> int:
        """Fetch live exchange rates. Returns number of rates fetched."""
        try:
            fetched = asyncio.get_event_loop().run_until_complete(
                self.exchange_rates.fetch_live_rates()
            )
            return len(fetched)
        except Exception as e:
            logger.warning(f"Exchange rate refresh failed: {e}")
            return 0

    def verify_ledger_integrity(self, *, audit: Any) -> dict[str, Any]:
        """Check ledger integrity and log critical issues."""
        try:
            integrity = self.ledger.verify_integrity()
            if not integrity["balanced"]:
                logger.critical(
                    f"LEDGER IMBALANCE: {len(integrity['unbalanced_entries'])} "
                    f"unbalanced entries detected"
                )
                audit.log("orchestrator", "system", "ledger_imbalance",
                          details=integrity, success=False, risk_level="critical")
            return integrity
        except Exception as e:
            logger.error(f"Ledger integrity check failed: {e}")
            return {"error": str(e)}

    def run_payment_cycle(
        self, *, payment_manager: Any, audit: Any,
        notify_creator: Any, log_action: Any, run_async: Any,
    ) -> dict[str, Any]:
        """Run payment processing — track payouts and generate contractor invoices."""
        try:
            sweep_result = run_async(payment_manager.run_sweep_cycle())
            health = run_async(payment_manager.health_check())

            result = {
                "sweep": sweep_result,
                "health": health,
                "status": payment_manager.get_status(),
            }

            flow = sweep_result.get("flow", "")
            if flow == "llc_contractor":
                invoice = sweep_result.get("invoice", {})
                if invoice.get("status") == "generated":
                    log_action("payment_invoice",
                               f"Invoice {invoice['invoice_number']}: €{invoice['amount']:.2f}")
                    audit.log("orchestrator", "payment", "invoice_generated",
                              details={"invoice_number": invoice["invoice_number"],
                                       "amount": invoice["amount"]},
                              risk_level="high")
                    notify_creator(
                        f"Invoice generated: {invoice['invoice_number']} — "
                        f"€{invoice['amount']:.2f}. Ready for payment."
                    )
            elif flow == "crypto_xmr":
                if sweep_result.get("sweeps_successful", 0) > 0:
                    xmr = sweep_result.get("total_xmr_swept", 0)
                    log_action("payment_sweep", f"Swept {xmr:.8f} XMR")
                    audit.log("orchestrator", "payment", "xmr_sweep",
                              details={"total_xmr": xmr}, risk_level="high")
                    notify_creator(f"Swept {xmr:.8f} XMR to your wallet.")

            return result

        except Exception as e:
            logger.error(f"Payment cycle failed: {e}")
            audit.log("orchestrator", "payment", "payment_cycle_failed",
                       details={"error": str(e)}, success=False, risk_level="high")
            return {"status": "error", "error": str(e)}

    def send_financial_reports(
        self, *, notify_creator: Any, log_action: Any, cycle: int,
    ) -> None:
        """Send periodic financial reports to creator via Telegram."""
        try:
            if self.reporter.should_send_monthly_report():
                report = self.reporter.generate_monthly_report()
                msg = self.reporter.format_telegram_report(report)
                notify_creator(msg)
                log_action("monthly_report", f"Sent monthly P&L for {report['period']}")
            elif self.reporter.should_send_weekly_report():
                dashboard = self.reporter.generate_strategy_dashboard()
                notify_creator(dashboard)
                log_action("weekly_dashboard", "Sent strategy performance dashboard")

                recon = self.reconciliation.run_reconciliation()
                if not recon.is_clean:
                    msg = self.reconciliation.format_telegram_report(recon)
                    notify_creator(msg)
                log_action("reconciliation",
                           f"Run #{recon.run_id}: {recon.matched} matched, "
                           f"{recon.discrepancy_count} discrepancies")
            elif cycle % 10 == 0:
                snapshot = self.reporter.generate_daily_snapshot()
                notify_creator(snapshot)
        except Exception as e:
            logger.error(f"Financial reporting failed: {e}")

    def evaluate_strategy_performance(self) -> dict[str, Any]:
        """Get strategy performance data for auto-pause/scale decisions."""
        return self.reporter.get_strategy_performance()

    def compute_reinvestment(
        self, *, log_action: Any,
    ) -> dict[str, Any]:
        """Compute reinvestment allocations and apply them."""
        try:
            reinvest_result = self.commercialista.compute_reinvestment()

            if reinvest_result.get("reinvest", 0) > 0:
                strat_perf = []
                for s in self.finance.get_strategy_pnl():
                    rev = s.get("revenue", 0)
                    exp = s.get("expenses", 0)
                    roi = rev / exp if exp > 0 else 0
                    strat_perf.append({
                        "name": s["name"], "revenue": rev,
                        "expenses": exp, "roi": roi,
                    })

                allocations = self.commercialista.allocate_to_strategies(
                    reinvest_result["reinvest"], strat_perf,
                )

                from datetime import datetime
                for alloc in allocations:
                    if alloc.get("amount", 0) > 0:
                        self.db.execute(
                            "UPDATE strategies SET allocated_budget = allocated_budget + ? "
                            "WHERE name = ?",
                            (alloc["amount"], alloc["strategy"]),
                        )
                    elif alloc.get("action") == "reduce":
                        self.db.execute(
                            "UPDATE strategies SET allocated_budget = MAX(0, allocated_budget * 0.5) "
                            "WHERE name = ?",
                            (alloc["strategy"],),
                        )

                self.commercialista.record_reinvestment(
                    reinvest=reinvest_result["reinvest"],
                    reserve=reinvest_result["reserve"],
                    creator=reinvest_result["creator_sweep"],
                    allocations=allocations,
                )

                logger.info(
                    f"Reinvestment: €{reinvest_result['reinvest']:.2f} allocated"
                )

            return reinvest_result
        except Exception as e:
            logger.error(f"Reinvestment cycle failed: {e}")
            return {"status": "error", "error": str(e)}
