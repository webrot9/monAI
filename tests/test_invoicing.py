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

    def test_invoice_with_entity_info(self, invoicing):
        """Invoice includes entity name and tax ID in HTML."""
        items = [{"description": "Consulting", "amount": 500.0}]
        result = invoicing.create_invoice(
            "Client Co", "client@co.com", items,
            entity_name="MyLLC",
            entity_tax_id="12-3456789",
            entity_address="123 Main St, Cheyenne WY",
        )
        assert result["entity_name"] == "MyLLC"
        assert result["entity_tax_id"] == "12-3456789"

        from pathlib import Path
        html = Path(result["html_path"]).read_text()
        assert "MyLLC" in html
        assert "12-3456789" in html
        assert "123 Main St" in html

    def test_invoice_with_period(self, invoicing):
        """Service period shows in HTML."""
        items = [{"description": "Monthly retainer", "amount": 2000.0}]
        result = invoicing.create_invoice(
            "Corp", "corp@test.com", items,
            period_start="2026-01-01", period_end="2026-01-31",
        )
        assert result["period_start"] == "2026-01-01"
        assert result["period_end"] == "2026-01-31"

        from pathlib import Path
        html = Path(result["html_path"]).read_text()
        assert "Service Period" in html
        assert "2026-01-01" in html

    def test_invoice_with_payment_instructions(self, invoicing):
        """Payment instructions appear in HTML."""
        items = [{"description": "Work", "amount": 100.0}]
        result = invoicing.create_invoice(
            "X", "x@test.com", items,
            payment_instructions="Wire to IBAN IT12345",
        )
        from pathlib import Path
        html = Path(result["html_path"]).read_text()
        assert "Wire to IBAN IT12345" in html

    def test_invoice_with_notes(self, invoicing):
        """Notes appear in HTML."""
        items = [{"description": "Work", "amount": 100.0}]
        result = invoicing.create_invoice(
            "X", "x@test.com", items,
            notes="Thank you for the prompt payment!",
        )
        from pathlib import Path
        html = Path(result["html_path"]).read_text()
        assert "Thank you for the prompt payment!" in html

    def test_invoice_with_client_address(self, invoicing):
        """Client address shows in HTML."""
        items = [{"description": "Work", "amount": 100.0}]
        result = invoicing.create_invoice(
            "ACME", "acme@test.com", items,
            client_address="456 Oak Ave, New York NY",
        )
        from pathlib import Path
        html = Path(result["html_path"]).read_text()
        assert "456 Oak Ave" in html

    def test_pdf_generation_skipped_without_weasyprint(self, invoicing):
        """PDF generation gracefully skips when weasyprint is not installed."""
        items = [{"description": "Work", "amount": 100.0}]
        result = invoicing.create_invoice(
            "X", "x@test.com", items, generate_pdf=True,
        )
        # Should not crash — just skip PDF if weasyprint unavailable
        # pdf_path may or may not be present depending on environment
        assert result["total"] == 100.0


class TestContractorInvoice:
    @pytest.fixture
    def invoicing(self, config, db, tmp_dir):
        config.data_dir = tmp_dir
        return Invoicing(config, db)

    def test_create_contractor_invoice(self, invoicing):
        """Contractor invoice creates correct structure."""
        result = invoicing.create_contractor_invoice(
            contractor_name="Mario Rossi",
            contractor_email="mario@example.com",
            contractor_tax_id="RSSMRA80A01H501Z",
            entity_name="MonAI LLC",
            entity_address="123 Main St, Cheyenne WY 82001",
            entity_tax_id="88-1234567",
            service_description="AI development services — January 2026",
            amount=3000.0,
            period_start="2026-01-01",
            period_end="2026-01-31",
        )
        assert result["total"] == 3000.0
        assert result["client_name"] == "MonAI LLC"  # LLC is being billed
        assert result["entity_name"] == "Mario Rossi"  # Contractor is the issuer

        from pathlib import Path
        html = Path(result["html_path"]).read_text()
        assert "Mario Rossi" in html
        assert "RSSMRA80A01H501Z" in html
        assert "MonAI LLC" in html
        assert "3000.00" in html
        assert "Service Period" in html
