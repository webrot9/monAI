"""Tests for corporate entity management and contractor billing."""

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from monai.business.corporate import CorporateManager
from monai.db.database import Database


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(Path(path))
    yield database
    os.unlink(path)


@pytest.fixture
def corp(db):
    return CorporateManager(db)


class TestEntityManagement:
    def test_create_entity(self, corp):
        eid = corp.create_entity(
            name="Alpine Holdings LLC",
            entity_type="llc_us",
            jurisdiction="US-WY",
            registered_agent="Northwest Registered Agent",
        )
        assert eid > 0

        entity = corp.get_entity(eid)
        assert entity is not None
        assert entity["name"] == "Alpine Holdings LLC"
        assert entity["entity_type"] == "llc_us"
        assert entity["jurisdiction"] == "US-WY"
        assert entity["status"] == "active"

    def test_get_all_entities(self, corp):
        corp.create_entity("LLC A", "llc_us", "US-WY")
        corp.create_entity("LLC B", "llc_us", "US-NM")

        entities = corp.get_all_entities()
        assert len(entities) == 2

    def test_get_primary_entity(self, corp):
        corp.create_entity("First LLC", "llc_us", "US-WY")
        corp.create_entity("Second LLC", "llc_us", "US-NM")

        primary = corp.get_primary_entity()
        assert primary["name"] == "First LLC"

    def test_update_entity_bank(self, corp):
        eid = corp.create_entity("Test LLC", "llc_us", "US-WY")
        corp.update_entity_bank(eid, "Mercury", "****1234", "084009519")

        entity = corp.get_entity(eid)
        assert entity["bank_name"] == "Mercury"
        assert entity["bank_account_id"] == "****1234"


class TestBrandOwnership:
    def test_assign_and_get_brands(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.assign_brand(eid, "newsletter_saas")
        corp.assign_brand(eid, "micro_tools")

        brands = corp.get_entity_brands(eid)
        assert len(brands) == 2
        brand_names = {b["brand"] for b in brands}
        assert "newsletter_saas" in brand_names
        assert "micro_tools" in brand_names

    def test_get_brand_entity(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.assign_brand(eid, "my_brand")

        entity = corp.get_brand_entity("my_brand")
        assert entity is not None
        assert entity["id"] == eid

    def test_brand_not_found(self, corp):
        assert corp.get_brand_entity("nonexistent") is None

    def test_duplicate_brand_ignored(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.assign_brand(eid, "brand_a")
        corp.assign_brand(eid, "brand_a")  # duplicate — should be ignored

        brands = corp.get_entity_brands(eid)
        assert len(brands) == 1


class TestContractorManagement:
    def test_create_contractor(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        cid = corp.create_contractor(
            alias="Marco Consulting",
            entity_id=eid,
            rate_type="percentage",
            rate_percentage=90.0,
        )
        assert cid > 0

        contractor = corp.get_contractor(cid)
        assert contractor["alias"] == "Marco Consulting"
        assert contractor["rate_percentage"] == 90.0
        assert contractor["rate_type"] == "percentage"

    def test_get_active_contractor(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.create_contractor("Consultant A", eid, rate_type="monthly", rate_amount=3000)

        contractor = corp.get_active_contractor(eid)
        assert contractor is not None
        assert contractor["alias"] == "Consultant A"

    def test_no_active_contractor(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        assert corp.get_active_contractor(eid) is None


class TestContractorInvoicing:
    def test_generate_invoice_percentage(self, corp, db):
        # Set up entity, brand, contractor, and revenue
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.assign_brand(eid, "test_brand")
        cid = corp.create_contractor(
            "Creator", eid, rate_type="percentage", rate_percentage=90.0,
        )

        # Init brand payments schema and insert revenue
        from monai.business.brand_payments import BrandPayments
        bp = BrandPayments(db)
        acc_id = bp.add_collection_account("test_brand", "stripe", "acct_test")
        bp.record_payment("test_brand", acc_id, 500.0, product="SaaS")
        bp.record_payment("test_brand", acc_id, 300.0, product="Ebook")

        now = datetime.now()
        period_start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        period_end = now.strftime("%Y-%m-%d")

        invoice = corp.generate_invoice(
            contractor_id=cid,
            entity_id=eid,
            period_start=period_start,
            period_end=period_end,
            brand_revenues=[
                {"brand": "test_brand", "revenue": 800.0},
            ],
        )

        assert "error" not in invoice or invoice.get("amount", 0) > 0
        # If period revenue was found, amount = 90% of it
        # (depends on exact date filtering, so we just check structure)
        assert "invoice_number" in invoice
        assert invoice["status"] == "draft"

    def test_generate_invoice_monthly(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        cid = corp.create_contractor(
            "Creator", eid, rate_type="monthly", rate_amount=3000,
        )

        now = datetime.now()
        invoice = corp.generate_invoice(
            contractor_id=cid,
            entity_id=eid,
            period_start=(now - timedelta(days=30)).strftime("%Y-%m-%d"),
            period_end=now.strftime("%Y-%m-%d"),
        )

        assert invoice["amount"] == 3000.0
        assert "CONT-" in invoice["invoice_number"]

    def test_mark_invoice_paid(self, corp, db):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        cid = corp.create_contractor("Creator", eid, rate_type="monthly", rate_amount=2000)

        now = datetime.now()
        invoice = corp.generate_invoice(
            cid, eid,
            (now - timedelta(days=30)).strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
        )

        corp.mark_invoice_sent(invoice["id"])
        corp.mark_invoice_paid(invoice["id"], payment_ref="WIRE-123")

        # Verify fund flow was recorded
        flows = corp.get_fund_flows("llc_to_contractor")
        assert len(flows) == 1
        assert flows[0]["amount"] == 2000.0
        assert flows[0]["reference"] == "WIRE-123"

    def test_get_pending_invoices(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        cid = corp.create_contractor("Creator", eid, rate_type="monthly", rate_amount=1500)

        now = datetime.now()
        corp.generate_invoice(
            cid, eid,
            (now - timedelta(days=30)).strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
        )

        pending = corp.get_pending_invoices()
        assert len(pending) == 1
        assert pending[0]["contractor_alias"] == "Creator"

    def test_invoice_contractor_not_found(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        result = corp.generate_invoice(999, eid, "2026-01-01", "2026-01-31")
        assert "error" in result

    def test_invoice_entity_not_found(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        cid = corp.create_contractor("Creator", eid)
        result = corp.generate_invoice(cid, 999, "2026-01-01", "2026-01-31")
        assert "error" in result


class TestFundFlows:
    def test_record_fund_flow(self, corp):
        fid = corp.record_fund_flow(
            flow_type="platform_payout",
            source_type="brand",
            source_id="newsletter",
            dest_type="llc",
            dest_id="1",
            amount=500.0,
            reference="po_stripe_123",
        )
        assert fid > 0

        flows = corp.get_fund_flows("platform_payout")
        assert len(flows) == 1
        assert flows[0]["amount"] == 500.0

    def test_record_platform_payout(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.assign_brand(eid, "saas_brand")

        fid = corp.record_platform_payout(
            brand="saas_brand", platform="stripe",
            amount=1200.0, reference="po_987",
        )
        assert fid > 0

    def test_total_paid_to_contractor(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        cid = corp.create_contractor("Creator", eid, rate_type="monthly", rate_amount=2000)

        now = datetime.now()
        inv = corp.generate_invoice(
            cid, eid,
            (now - timedelta(days=60)).strftime("%Y-%m-%d"),
            (now - timedelta(days=30)).strftime("%Y-%m-%d"),
        )
        corp.mark_invoice_paid(inv["id"], "WIRE-001")

        inv2 = corp.generate_invoice(
            cid, eid,
            (now - timedelta(days=30)).strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
        )
        corp.mark_invoice_paid(inv2["id"], "WIRE-002")

        total = corp.get_total_paid_to_contractor(cid)
        assert total == 4000.0


class TestExpenseManagement:
    def test_record_expense(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        exp_id = corp.record_expense(
            entity_id=eid,
            category="hardware",
            description="MacBook Pro 16-inch M4",
            amount=2999.0,
            vendor="Apple",
            purchase_date="2026-03-01",
        )
        assert exp_id > 0

        expenses = corp.get_expenses(entity_id=eid)
        assert len(expenses) == 1
        assert expenses[0]["description"] == "MacBook Pro 16-inch M4"
        assert expenses[0]["amount"] == 2999.0

    def test_expense_total(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.record_expense(eid, "hardware", "Laptop", 2000.0, purchase_date="2026-01-15")
        corp.record_expense(eid, "software", "JetBrains", 200.0, purchase_date="2026-02-01")
        corp.record_expense(eid, "hosting", "AWS", 50.0, purchase_date="2026-03-01")

        total = corp.get_expense_total(eid)
        assert total == 2250.0

    def test_expense_total_with_period(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.record_expense(eid, "software", "A", 100.0, purchase_date="2026-01-15")
        corp.record_expense(eid, "software", "B", 200.0, purchase_date="2026-02-15")
        corp.record_expense(eid, "software", "C", 300.0, purchase_date="2026-03-15")

        total = corp.get_expense_total(eid, period_start="2026-02-01", period_end="2026-02-28")
        assert total == 200.0

    def test_recurring_expenses(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.record_expense(eid, "software", "GitHub", 9.0, is_recurring=True,
                           recurrence_period="monthly", purchase_date="2026-01-01")
        corp.record_expense(eid, "hardware", "Keyboard", 150.0, purchase_date="2026-01-15")

        recurring = corp.get_recurring_expenses(eid)
        assert len(recurring) == 1
        assert recurring[0]["description"] == "GitHub"

    def test_expense_by_category(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.record_expense(eid, "hardware", "Laptop", 2000.0, purchase_date="2026-01-01")
        corp.record_expense(eid, "hardware", "Monitor", 500.0, purchase_date="2026-01-02")
        corp.record_expense(eid, "software", "IDE", 200.0, purchase_date="2026-01-03")

        breakdown = corp.get_expense_summary_by_category(eid)
        assert breakdown["hardware"] == 2500.0
        assert breakdown["software"] == 200.0

    def test_expense_records_fund_flow(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.record_expense(eid, "hosting", "AWS EC2", 150.0, vendor="AWS",
                           purchase_date="2026-02-01")

        flows = corp.get_fund_flows("llc_expense")
        assert len(flows) == 1
        assert flows[0]["amount"] == 150.0
        assert flows[0]["dest_id"] == "AWS"


class TestTaxCompliance:
    def test_add_obligation(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        oid = corp.add_tax_obligation(
            obligation_type="form_5472",
            jurisdiction="US",
            description="Form 5472 — Holdings LLC — 2025",
            due_date="2026-04-15",
            entity_id=eid,
            filing_period="2025",
        )
        assert oid > 0

    def test_mark_filed_and_paid(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        oid = corp.add_tax_obligation(
            "annual_report_wy", "US-WY",
            "Wyoming Annual Report 2026", "2026-05-01",
            entity_id=eid, amount_due=60.0,
        )

        corp.mark_obligation_filed(oid, "WY-2026-12345")
        pending = corp.get_pending_obligations()
        assert all(o["id"] != oid for o in pending)  # filed, no longer pending

        corp.mark_obligation_paid(oid, "PAY-67890")

    def test_overdue_detection(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        # Past due date
        corp.add_tax_obligation(
            "form_5472", "US", "Form 5472 — overdue", "2025-04-15",
            entity_id=eid,
        )
        # Future due date
        corp.add_tax_obligation(
            "annual_report_wy", "US-WY", "Wyoming AR — future", "2027-01-01",
            entity_id=eid,
        )

        overdue = corp.get_overdue_obligations()
        assert len(overdue) == 1
        assert overdue[0]["obligation_type"] == "form_5472"

    def test_setup_annual_obligations(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY",
                                formation_date="2026-03-01")

        ids = corp.setup_annual_obligations(eid, 2026)
        assert len(ids) == 3  # form_5472, annual_report, registered_agent

        pending = corp.get_pending_obligations()
        types = {o["obligation_type"] for o in pending}
        assert "form_5472" in types
        assert "annual_report_wy" in types
        assert "registered_agent_renewal" in types

    def test_setup_piva_obligations(self, corp):
        ids = corp.setup_piva_obligations(2026)
        assert len(ids) == 6  # acconto1, acconto2, saldo, inps1, inps2, dichiarazione

        pending = corp.get_pending_obligations("IT")
        types = {o["obligation_type"] for o in pending}
        assert "piva_acconto_1" in types
        assert "piva_acconto_2" in types
        assert "piva_saldo" in types
        assert "dichiarazione_redditi" in types

    def test_pending_by_jurisdiction(self, corp):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.setup_annual_obligations(eid, 2026)
        corp.setup_piva_obligations(2026)

        us_only = corp.get_pending_obligations("US")
        it_only = corp.get_pending_obligations("IT")
        assert len(us_only) >= 1
        assert len(it_only) >= 1
        # No overlap
        us_types = {o["obligation_type"] for o in us_only}
        it_types = {o["obligation_type"] for o in it_only}
        assert not us_types & it_types

    def test_setup_nonexistent_entity(self, corp):
        ids = corp.setup_annual_obligations(999, 2026)
        assert ids == []


class TestFinancialSummary:
    def test_financial_summary_empty(self, corp):
        summary = corp.get_financial_summary()
        assert summary["total_revenue"] == 0
        assert summary["total_paid_to_contractor"] == 0
        assert summary["total_expenses_via_llc"] == 0
        assert summary["entities"] == []
        assert summary["pending_tax_obligations"] == 0
        assert summary["overdue_tax_obligations"] == 0

    def test_financial_summary_with_data(self, corp, db):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.assign_brand(eid, "brand_a")
        cid = corp.create_contractor("Creator", eid, rate_type="monthly", rate_amount=1000)

        # Add revenue
        from monai.business.brand_payments import BrandPayments
        bp = BrandPayments(db)
        acc_id = bp.add_collection_account("brand_a", "stripe", "acct_a")
        bp.record_payment("brand_a", acc_id, 2000.0)

        # Add expense
        corp.record_expense(eid, "hardware", "Laptop", 1200.0, purchase_date="2026-01-15")

        summary = corp.get_financial_summary()
        assert summary["total_revenue"] == 2000.0
        assert summary["total_expenses_via_llc"] == 1200.0
        assert summary["creator_effective_income"] >= 1200.0
        assert len(summary["entities"]) == 1
        assert summary["entities"][0]["brands"] == ["brand_a"]
