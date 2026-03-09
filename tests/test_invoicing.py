"""Tests for monai.business.invoicing."""

import json

import pytest

from monai.business.invoicing import Invoicing


class TestInvoicing:
    @pytest.fixture
    def invoicing(self, config, db, tmp_dir):
        config.data_dir = tmp_dir
        return Invoicing(config, db)

    def test_create_invoice(self, invoicing):
        items = [
            {"description": "Blog post writing", "amount": 150.0},
            {"description": "SEO optimization", "amount": 50.0},
        ]
        result = invoicing.create_invoice(
            client_name="Alice Corp",
            client_email="alice@example.com",
            items=items,
        )
        assert result["total"] == 200.0
        assert result["client_name"] == "Alice Corp"
        assert result["currency"] == "EUR"
        assert result["status"] == "sent"
        assert result["invoice_number"].startswith("INV-")

    def test_invoice_html_generated(self, invoicing):
        items = [{"description": "Service", "amount": 100.0}]
        result = invoicing.create_invoice("Bob", "bob@test.com", items)

        from pathlib import Path
        html_path = Path(result["html_path"])
        assert html_path.exists()
        html = html_path.read_text()
        assert "INVOICE" in html
        assert "Bob" in html
        assert "100.00" in html

    def test_invoice_json_saved(self, invoicing, tmp_dir):
        items = [{"description": "Work", "amount": 75.0}]
        result = invoicing.create_invoice("Client", "c@test.com", items)

        json_path = tmp_dir / "invoices" / f"{result['invoice_number']}.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert data["total"] == 75.0

    def test_sequential_invoice_numbers(self, invoicing, db):
        items = [{"description": "Work", "amount": 10.0}]
        r1 = invoicing.create_invoice("A", "a@test.com", items)
        # Simulate the invoice being recorded as a transaction (as the system would do)
        db.execute_insert(
            "INSERT INTO transactions (type, category, amount) VALUES ('revenue', 'invoice', ?)",
            (10.0,),
        )
        r2 = invoicing.create_invoice("B", "b@test.com", items)
        assert r1["invoice_number"] != r2["invoice_number"]

    def test_invoice_includes_due_date(self, invoicing):
        items = [{"description": "Work", "amount": 10.0}]
        result = invoicing.create_invoice("X", "x@test.com", items)
        assert "due_date" in result
        assert result["due_date"] > result["date"]
