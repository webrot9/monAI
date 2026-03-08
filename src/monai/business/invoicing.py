"""Invoice generation module."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from monai.config import Config
from monai.db.database import Database

INVOICE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><style>
body {{ font-family: Arial, sans-serif; margin: 40px; color: #333; }}
.header {{ display: flex; justify-content: space-between; margin-bottom: 40px; }}
.invoice-title {{ font-size: 28px; font-weight: bold; color: #2c3e50; }}
.invoice-number {{ color: #7f8c8d; }}
table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
th {{ background: #2c3e50; color: white; padding: 12px; text-align: left; }}
td {{ padding: 12px; border-bottom: 1px solid #eee; }}
.total {{ font-size: 20px; font-weight: bold; text-align: right; margin-top: 20px; }}
.footer {{ margin-top: 40px; color: #7f8c8d; font-size: 12px; }}
</style></head>
<body>
<div class="header">
    <div>
        <div class="invoice-title">INVOICE</div>
        <div class="invoice-number">#{invoice_number}</div>
    </div>
    <div>
        <div><strong>{from_name}</strong></div>
        <div>{from_email}</div>
    </div>
</div>
<div>
    <p><strong>Bill To:</strong> {client_name}</p>
    <p><strong>Email:</strong> {client_email}</p>
    <p><strong>Date:</strong> {date}</p>
    <p><strong>Due Date:</strong> {due_date}</p>
</div>
<table>
    <tr><th>Description</th><th>Amount</th></tr>
    {line_items}
</table>
<div class="total">Total: {currency} {total:.2f}</div>
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
    ) -> dict[str, Any]:
        invoice_number = self._next_invoice_number()
        total = sum(item["amount"] for item in items)
        date = datetime.now().strftime("%Y-%m-%d")
        due_date = (datetime.now() + timedelta(days=15)).strftime("%Y-%m-%d")

        line_items_html = "\n".join(
            f'<tr><td>{item["description"]}</td><td>{self.config.currency} {item["amount"]:.2f}</td></tr>'
            for item in items
        )

        html = INVOICE_TEMPLATE.format(
            invoice_number=invoice_number,
            from_name=self.config.comms.from_name,
            from_email=self.config.comms.from_email,
            client_name=client_name,
            client_email=client_email,
            date=date,
            due_date=due_date,
            line_items=line_items_html,
            currency=self.config.currency,
            total=total,
        )

        # Save HTML invoice
        html_path = self.invoice_dir / f"{invoice_number}.html"
        html_path.write_text(html)

        # Save invoice data as JSON for records
        invoice_data = {
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
        json_path = self.invoice_dir / f"{invoice_number}.json"
        json_path.write_text(json.dumps(invoice_data, indent=2))

        return invoice_data
