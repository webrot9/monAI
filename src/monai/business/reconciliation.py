"""Reconciliation engine — matches GL entries with payment provider events.

Identifies:
  - Unreconciled GL entries (no matching webhook event)
  - Unreconciled webhook events (no matching GL entry)
  - Amount mismatches between GL and webhook
  - Marks matched pairs as reconciled in GL

Run periodically (weekly or on-demand) to ensure books match reality.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

RECONCILIATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    total_gl_entries INTEGER DEFAULT 0,
    total_webhook_events INTEGER DEFAULT 0,
    matched INTEGER DEFAULT 0,
    unmatched_gl INTEGER DEFAULT 0,
    unmatched_webhooks INTEGER DEFAULT 0,
    amount_mismatches INTEGER DEFAULT 0,
    status TEXT DEFAULT 'completed',
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reconciliation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES reconciliation_runs(id),
    item_type TEXT NOT NULL,     -- 'matched', 'unmatched_gl', 'unmatched_webhook', 'amount_mismatch'
    gl_entry_id INTEGER,
    webhook_event_id INTEGER,
    payment_ref TEXT,
    gl_amount REAL,
    webhook_amount REAL,
    currency TEXT,
    provider TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass
class ReconciliationResult:
    """Result of a reconciliation run."""
    run_id: int = 0
    total_gl: int = 0
    total_webhooks: int = 0
    matched: int = 0
    unmatched_gl: list[dict[str, Any]] = field(default_factory=list)
    unmatched_webhooks: list[dict[str, Any]] = field(default_factory=list)
    amount_mismatches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True if everything reconciles perfectly."""
        return (
            not self.unmatched_gl
            and not self.unmatched_webhooks
            and not self.amount_mismatches
        )

    @property
    def discrepancy_count(self) -> int:
        return len(self.unmatched_gl) + len(self.unmatched_webhooks) + len(self.amount_mismatches)


class ReconciliationEngine:
    """Matches GL journal entries against payment provider webhook events."""

    # Amount tolerance for matching (€0.01 to handle rounding)
    AMOUNT_TOLERANCE = 0.01

    def __init__(self, db: Database):
        self.db = db
        self._init_schema()

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(RECONCILIATION_SCHEMA)

    def run_reconciliation(self, start_date: str | None = None,
                           end_date: str | None = None) -> ReconciliationResult:
        """Run a full reconciliation between GL and webhook events.

        Args:
            start_date: Reconcile from this date (YYYY-MM-DD). Default: all time.
            end_date: Reconcile until this date. Default: today.
        """
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        # Get GL entries from webhook source (only these should match webhook events)
        gl_entries = self._get_gl_entries(start_date, end_date)

        # Get webhook events
        webhook_events = self._get_webhook_events(start_date, end_date)

        # Build lookup by payment_ref
        gl_by_ref: dict[str, list[dict]] = {}
        for entry in gl_entries:
            ref = entry.get("reference", "")
            if ref:
                gl_by_ref.setdefault(ref, []).append(entry)

        webhook_by_ref: dict[str, list[dict]] = {}
        for event in webhook_events:
            ref = event.get("payment_ref", "")
            if ref:
                webhook_by_ref.setdefault(ref, []).append(event)

        # Create reconciliation run record
        run_id = self.db.execute_insert(
            "INSERT INTO reconciliation_runs (run_date, total_gl_entries, total_webhook_events) "
            "VALUES (?, ?, ?)",
            (end_date, len(gl_entries), len(webhook_events)),
        )

        result = ReconciliationResult(
            run_id=run_id,
            total_gl=len(gl_entries),
            total_webhooks=len(webhook_events),
        )

        # Match GL entries to webhook events
        matched_gl_ids: set[int] = set()
        matched_webhook_ids: set[int] = set()

        all_refs = set(gl_by_ref.keys()) | set(webhook_by_ref.keys())

        for ref in all_refs:
            gl_items = gl_by_ref.get(ref, [])
            wh_items = webhook_by_ref.get(ref, [])

            if gl_items and wh_items:
                # Match pairs
                for gl, wh in zip(gl_items, wh_items):
                    gl_amount = self._get_gl_entry_amount(gl)
                    wh_amount = wh.get("amount", 0) or 0

                    if abs(gl_amount - wh_amount) <= self.AMOUNT_TOLERANCE:
                        # Perfect match
                        result.matched += 1
                        matched_gl_ids.add(gl["id"])
                        matched_webhook_ids.add(wh["id"])

                        self._record_item(run_id, "matched", gl, wh, ref)
                        self._mark_reconciled(gl["id"])
                    else:
                        # Amount mismatch
                        mismatch = {
                            "payment_ref": ref,
                            "gl_entry_id": gl["id"],
                            "webhook_event_id": wh["id"],
                            "gl_amount": gl_amount,
                            "webhook_amount": wh_amount,
                            "difference": round(gl_amount - wh_amount, 2),
                            "provider": wh.get("provider", ""),
                            "currency": wh.get("currency", "EUR"),
                        }
                        result.amount_mismatches.append(mismatch)
                        self._record_item(
                            run_id, "amount_mismatch", gl, wh, ref,
                            notes=f"GL={gl_amount}, WH={wh_amount}, diff={mismatch['difference']}",
                        )

                # Handle unmatched extras
                for gl in gl_items[len(wh_items):]:
                    if gl["id"] not in matched_gl_ids:
                        result.unmatched_gl.append({
                            "gl_entry_id": gl["id"],
                            "payment_ref": ref,
                            "amount": self._get_gl_entry_amount(gl),
                            "description": gl.get("description", ""),
                            "source": gl.get("source", ""),
                        })
                        self._record_item(run_id, "unmatched_gl", gl, None, ref)

                for wh in wh_items[len(gl_items):]:
                    if wh["id"] not in matched_webhook_ids:
                        result.unmatched_webhooks.append({
                            "webhook_event_id": wh["id"],
                            "payment_ref": ref,
                            "amount": wh.get("amount", 0),
                            "provider": wh.get("provider", ""),
                            "event_type": wh.get("event_type", ""),
                        })
                        self._record_item(run_id, "unmatched_webhook", None, wh, ref)

            elif gl_items and not wh_items:
                # GL entries with no matching webhook
                for gl in gl_items:
                    result.unmatched_gl.append({
                        "gl_entry_id": gl["id"],
                        "payment_ref": ref,
                        "amount": self._get_gl_entry_amount(gl),
                        "description": gl.get("description", ""),
                        "source": gl.get("source", ""),
                    })
                    self._record_item(run_id, "unmatched_gl", gl, None, ref)

            elif wh_items and not gl_items:
                # Webhook events with no GL entry
                for wh in wh_items:
                    result.unmatched_webhooks.append({
                        "webhook_event_id": wh["id"],
                        "payment_ref": ref,
                        "amount": wh.get("amount", 0),
                        "provider": wh.get("provider", ""),
                        "event_type": wh.get("event_type", ""),
                    })
                    self._record_item(run_id, "unmatched_webhook", None, wh, ref)

        # Update run record with results
        self.db.execute(
            "UPDATE reconciliation_runs SET matched = ?, unmatched_gl = ?, "
            "unmatched_webhooks = ?, amount_mismatches = ?, status = ? "
            "WHERE id = ?",
            (
                result.matched,
                len(result.unmatched_gl),
                len(result.unmatched_webhooks),
                len(result.amount_mismatches),
                "clean" if result.is_clean else "discrepancies",
                run_id,
            ),
        )

        if result.is_clean:
            logger.info(f"Reconciliation #{run_id}: CLEAN — {result.matched} matched")
        else:
            logger.warning(
                f"Reconciliation #{run_id}: {result.discrepancy_count} discrepancies "
                f"({len(result.unmatched_gl)} unmatched GL, "
                f"{len(result.unmatched_webhooks)} unmatched webhooks, "
                f"{len(result.amount_mismatches)} amount mismatches)"
            )

        return result

    def _get_gl_entries(self, start_date: str | None,
                        end_date: str) -> list[dict[str, Any]]:
        """Get GL entries with payment references for reconciliation."""
        query = (
            "SELECT e.id, e.entry_date, e.description, e.reference, "
            "e.source, e.brand, e.is_reconciled, "
            "COALESCE(SUM(l.debit), 0) as total_debit "
            "FROM gl_journal_entries e "
            "JOIN gl_journal_lines l ON l.entry_id = e.id "
            "WHERE e.reference IS NOT NULL AND e.reference != '' "
            "AND e.source IN ('webhook', 'webhook_refund') "
            "AND e.entry_date <= ? "
        )
        params: list = [end_date]

        if start_date:
            query += "AND e.entry_date >= ? "
            params.append(start_date)

        query += "GROUP BY e.id ORDER BY e.entry_date"
        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def _get_webhook_events(self, start_date: str | None,
                            end_date: str) -> list[dict[str, Any]]:
        """Get webhook events for reconciliation."""
        query = (
            "SELECT id, provider, event_type, payment_ref, amount, currency, "
            "brand, status, created_at "
            "FROM webhook_events "
            "WHERE payment_ref IS NOT NULL AND payment_ref != '' "
            "AND DATE(created_at) <= ? "
        )
        params: list = [end_date]

        if start_date:
            query += "AND DATE(created_at) >= ? "
            params.append(start_date)

        query += "ORDER BY created_at"
        rows = self.db.execute(query, tuple(params))
        return [dict(r) for r in rows]

    def _get_gl_entry_amount(self, entry: dict[str, Any]) -> float:
        """Get the total debit amount for a GL entry (represents money flow)."""
        return entry.get("total_debit", 0) or 0

    def _mark_reconciled(self, gl_entry_id: int) -> None:
        """Mark a GL entry as reconciled."""
        self.db.execute(
            "UPDATE gl_journal_entries SET is_reconciled = 1 WHERE id = ?",
            (gl_entry_id,),
        )

    def _record_item(self, run_id: int, item_type: str,
                     gl: dict | None, wh: dict | None,
                     ref: str, notes: str = "") -> None:
        """Record a reconciliation item."""
        self.db.execute_insert(
            "INSERT INTO reconciliation_items "
            "(run_id, item_type, gl_entry_id, webhook_event_id, payment_ref, "
            "gl_amount, webhook_amount, currency, provider, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                item_type,
                gl["id"] if gl else None,
                wh["id"] if wh else None,
                ref,
                self._get_gl_entry_amount(gl) if gl else None,
                wh.get("amount") if wh else None,
                (wh or {}).get("currency", "EUR"),
                (wh or {}).get("provider", ""),
                notes,
            ),
        )

    # ── Reporting ────────────────────────────────────────────────

    def get_run_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get recent reconciliation run results."""
        rows = self.db.execute(
            "SELECT * FROM reconciliation_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def get_run_items(self, run_id: int) -> list[dict[str, Any]]:
        """Get all items from a specific reconciliation run."""
        rows = self.db.execute(
            "SELECT * FROM reconciliation_items WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [dict(r) for r in rows]

    def get_unreconciled_gl_entries(self) -> list[dict[str, Any]]:
        """Get all GL entries that haven't been reconciled yet."""
        rows = self.db.execute(
            "SELECT e.id, e.entry_date, e.description, e.reference, e.source, "
            "COALESCE(SUM(l.debit), 0) as total_debit "
            "FROM gl_journal_entries e "
            "JOIN gl_journal_lines l ON l.entry_id = e.id "
            "WHERE e.is_reconciled = 0 AND e.reference IS NOT NULL "
            "AND e.reference != '' "
            "GROUP BY e.id ORDER BY e.entry_date"
        )
        return [dict(r) for r in rows]

    def format_telegram_report(self, result: ReconciliationResult) -> str:
        """Format reconciliation results for Telegram notification."""
        lines = [f"*Reconciliation Report #{result.run_id}*", "```"]

        if result.is_clean:
            lines.append(f"Status: CLEAN")
            lines.append(f"Matched: {result.matched} entries")
        else:
            lines.append(f"Status: DISCREPANCIES FOUND")
            lines.append(f"Matched:          {result.matched}")
            lines.append(f"Unmatched GL:     {len(result.unmatched_gl)}")
            lines.append(f"Unmatched WH:     {len(result.unmatched_webhooks)}")
            lines.append(f"Amt mismatches:   {len(result.amount_mismatches)}")

        lines.append(f"GL entries:       {result.total_gl}")
        lines.append(f"Webhook events:   {result.total_webhooks}")
        lines.append("```")

        # Show top mismatches
        if result.amount_mismatches:
            lines.append("\n*Amount Mismatches:*")
            for m in result.amount_mismatches[:5]:
                lines.append(
                    f"- `{m['payment_ref']}`: GL €{m['gl_amount']:.2f} vs "
                    f"WH €{m['webhook_amount']:.2f} (diff €{m['difference']:.2f})"
                )

        return "\n".join(lines)
