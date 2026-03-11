"""Invoice generation module — HTML and optional PDF invoices.

Supports:
  - Client invoices (service-based billing)
  - Contractor invoices (LLC→contractor payouts)
  - PDF generation via weasyprint (optional dependency)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from monai.config import Config
from monai.db.database import Database

logger = logging.getLogger(__name__)

INVOICE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><style>
body {{ font-family: Arial, sans-serif; margin: 40px; color: #333; }}
.header {{ display: flex; justify-content: space-between; margin-bottom: 40px; }}
.invoice-title {{ font-size: 28px; font-weight: bold; color: #2c3e50; }}
.invoice-number {{ color: #7f8c8d; }}
.entity-info {{ text-align: right; }}
table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
th {{ background: #2c3e50; color: white; padding: 12px; text-align: left; }}
td {{ padding: 12px; border-bottom: 1px solid #eee; }}
.total {{ font-size: 20px; font-weight: bold; text-align: right; margin-top: 20px; }}
.period {{ margin: 10px 0; color: #555; }}
.payment-info {{ margin-top: 30px; padding: 15px; background: #f8f9fa; border-radius: 4px; }}
.notes {{ margin-top: 20px; color: #666; font-style: italic; }}
.footer {{ margin-top: 40px; color: #7f8c8d; font-size: 12px; }}
</style></head>
<body>
<div class="header">
    <div>
        <div class="invoice-title">INVOICE</div>
        <div class="invoice-number">#{invoice_number}</div>
    </div>
    <div class="entity-info">
        <div><strong>{from_name}</strong></div>
        {from_address_html}
        <div>{from_email}</div>
        {tax_id_html}
    </div>
</div>
<div>
    <p><strong>Bill To:</strong> {client_name}</p>
    {client_address_html}
    <p><strong>Email:</strong> {client_email}</p>
    <p><strong>Date:</strong> {date}</p>
    <p><strong>Due Date:</strong> {due_date}</p>
    {period_html}
</div>
<table>
    <tr><th>Description</th><th>Amount</th></tr>
    {line_items}
</table>
<div class="total">Total: {currency} {total:.2f}</div>
{payment_html}
{notes_html}
<div class="footer">
    <p>Payment terms: Net 15. Thank you for your business.</p>
</div>
</body>
</html>
"""


class Invoicing:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.invoice_dir = config.data_dir / "invoices"
        self.invoice_dir.mkdir(parents=True, exist_ok=True)

    def _next_invoice_number(self) -> str:
        rows = self.db.execute(
            "SELECT COUNT(*) as count FROM transactions WHERE category = 'invoice'"
        )
        count = rows[0]["count"] + 1
        return f"INV-{datetime.now().strftime('%Y%m')}-{count:04d}"

    def create_invoice(
        self,
        client_name: str,
        client_email: str,
        items: list[dict[str, Any]],  # [{"description": str, "amount": float}]
        project_id: int | None = None,
        strategy_id: int | None = None,
        *,
        client_address: str = "",
        entity_name: str = "",
        entity_address: str = "",
        entity_tax_id: str = "",
        period_start: str = "",
        period_end: str = "",
        payment_instructions: str = "",
        notes: str = "",
        generate_pdf: bool = False,
    ) -> dict[str, Any]:
        """Create and save an invoice.

        Args:
            client_name: Name of client being billed.
            client_email: Client email address.
            items: Line items, each with "description" and "amount".
            project_id: Optional project reference.
            strategy_id: Optional strategy reference.
            client_address: Optional billing address for client.
            entity_name: Override billing entity name (default: config comms name).
            entity_address: Billing entity address.
            entity_tax_id: Tax ID / EIN / P.IVA to show on invoice.
            period_start: Service period start (YYYY-MM-DD).
            period_end: Service period end (YYYY-MM-DD).
            payment_instructions: Bank/wire/crypto payment details.
            notes: Additional notes to include on the invoice.
            generate_pdf: If True, also generate a PDF (requires weasyprint).

        Returns:
            Invoice data dict with paths and metadata.
        """
        invoice_number = self._next_invoice_number()
        total = sum(item["amount"] for item in items)
        date = datetime.now().strftime("%Y-%m-%d")
        due_date = (datetime.now() + timedelta(days=15)).strftime("%Y-%m-%d")

        line_items_html = "\n".join(
            f'<tr><td>{item["description"]}</td><td>{self.config.currency} {item["amount"]:.2f}</td></tr>'
            for item in items
        )

        from_name = entity_name or self.config.comms.from_name

        # Build optional HTML sections
        from_address_html = f"<div>{entity_address}</div>" if entity_address else ""
        tax_id_html = f"<div>Tax ID: {entity_tax_id}</div>" if entity_tax_id else ""
        client_address_html = (
            f"<p><strong>Address:</strong> {client_address}</p>"
            if client_address else ""
        )
        period_html = ""
        if period_start and period_end:
            period_html = (
                f'<div class="period"><strong>Service Period:</strong> '
                f'{period_start} to {period_end}</div>'
            )
        payment_html = ""
        if payment_instructions:
            payment_html = (
                f'<div class="payment-info">'
                f'<strong>Payment Instructions:</strong><br>{payment_instructions}'
                f'</div>'
            )
        notes_html = (
            f'<div class="notes"><strong>Notes:</strong> {notes}</div>'
            if notes else ""
        )

        html = INVOICE_TEMPLATE.format(
            invoice_number=invoice_number,
            from_name=from_name,
            from_address_html=from_address_html,
            from_email=self.config.comms.from_email,
            tax_id_html=tax_id_html,
            client_name=client_name,
            client_email=client_email,
            client_address_html=client_address_html,
            date=date,
            due_date=due_date,
            period_html=period_html,
            line_items=line_items_html,
            currency=self.config.currency,
            total=total,
            payment_html=payment_html,
            notes_html=notes_html,
        )

        # Save HTML invoice
        html_path = self.invoice_dir / f"{invoice_number}.html"
        html_path.write_text(html)

        # Save invoice data as JSON for records
        invoice_data: dict[str, Any] = {
            "invoice_number": invoice_number,
            "client_name": client_name,
            "client_email": client_email,
            "items": items,
            "total": total,
            "currency": self.config.currency,
            "date": date,
            "due_date": due_date,
            "status": "sent",
            "html_path": str(html_path),
        }
        if entity_name:
            invoice_data["entity_name"] = entity_name
        if entity_tax_id:
            invoice_data["entity_tax_id"] = entity_tax_id
        if period_start:
            invoice_data["period_start"] = period_start
        if period_end:
            invoice_data["period_end"] = period_end

        json_path = self.invoice_dir / f"{invoice_number}.json"
        json_path.write_text(json.dumps(invoice_data, indent=2))

        # Optional PDF generation
        if generate_pdf:
            pdf_path = self._generate_pdf(html, invoice_number)
            if pdf_path:
                invoice_data["pdf_path"] = str(pdf_path)

        return invoice_data

    def create_contractor_invoice(
        self,
        contractor_name: str,
        contractor_email: str,
        contractor_tax_id: str,
        entity_name: str,
        entity_address: str,
        entity_tax_id: str,
        service_description: str,
        amount: float,
        period_start: str,
        period_end: str,
        *,
        payment_instructions: str = "",
        generate_pdf: bool = False,
    ) -> dict[str, Any]:
        """Create a contractor payout invoice (contractor bills the LLC).

        Used for the LLC→contractor payment flow where the contractor
        (the creator) invoices the LLC for services rendered.
        """
        items = [{"description": service_description, "amount": amount}]
        return self.create_invoice(
            client_name=entity_name,
            client_email="",
            items=items,
            entity_name=contractor_name,
            entity_address="",
            entity_tax_id=contractor_tax_id,
            client_address=entity_address,
            period_start=period_start,
            period_end=period_end,
            payment_instructions=payment_instructions,
            generate_pdf=generate_pdf,
        )

    def _generate_pdf(self, html: str, invoice_number: str) -> Path | None:
        """Generate PDF from HTML using weasyprint (optional dependency)."""
        try:
            from weasyprint import HTML  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("weasyprint not installed — skipping PDF generation")
            return None

        pdf_path = self.invoice_dir / f"{invoice_number}.pdf"
        try:
            HTML(string=html).write_pdf(str(pdf_path))
            logger.info(f"PDF invoice generated: {pdf_path}")
            return pdf_path
        except Exception as e:
            logger.error(f"PDF generation failed: {e}")
            return None
