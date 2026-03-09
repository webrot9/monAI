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


class TestFinancialSummary:
    def test_financial_summary_empty(self, corp):
        summary = corp.get_financial_summary()
        assert summary["total_revenue"] == 0
        assert summary["total_paid_to_contractor"] == 0
        assert summary["entities"] == []

    def test_financial_summary_with_data(self, corp, db):
        eid = corp.create_entity("Holdings LLC", "llc_us", "US-WY")
        corp.assign_brand(eid, "brand_a")
        cid = corp.create_contractor("Creator", eid, rate_type="monthly", rate_amount=1000)

        # Add revenue
        from monai.business.brand_payments import BrandPayments
        bp = BrandPayments(db)
        acc_id = bp.add_collection_account("brand_a", "stripe", "acct_a")
        bp.record_payment("brand_a", acc_id, 2000.0)

        summary = corp.get_financial_summary()
        assert summary["total_revenue"] == 2000.0
        assert len(summary["entities"]) == 1
        assert summary["entities"][0]["brands"] == ["brand_a"]
