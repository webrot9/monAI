"""Tests for Pipeline — conversion pipeline CRM."""

import json
from unittest.mock import MagicMock

import pytest

from monai.business.pipeline import Pipeline, STAGES
from monai.db.database import Database
from tests.conftest_schema import TEST_SCHEMA


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    with d.connect() as conn:
        conn.executescript(TEST_SCHEMA)
    return d


@pytest.fixture
def pipeline(db):
    return Pipeline(db)


# ── Schema ────────────────────────────────────────────────────


class TestSchema:
    def test_creates_tables(self, pipeline, db):
        for table in ("pipeline_leads", "pipeline_events", "pipeline_revenue"):
            rows = db.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            assert len(rows) == 1, f"Table {table} not created"

    def test_stages_defined(self):
        assert "impression" in STAGES
        assert "customer" in STAGES
        assert "lost" in STAGES
        assert len(STAGES) == 7


# ── Lead Management ──────────────────────────────────────────


class TestLeadManagement:
    def test_create_lead(self, pipeline):
        lead_id = pipeline.create_lead(
            brand="micro_saas", source_platform="twitter",
            email="test@example.com", name="Test User",
        )
        assert lead_id > 0

    def test_get_lead(self, pipeline):
        lead_id = pipeline.create_lead(
            brand="newsletter", email="reader@example.com",
        )
        lead = pipeline.get_lead(lead_id)
        assert lead is not None
        assert lead["brand"] == "newsletter"
        assert lead["email"] == "reader@example.com"
        assert lead["stage"] == "impression"

    def test_get_lead_not_found(self, pipeline):
        assert pipeline.get_lead(999) is None

    def test_get_leads_by_brand(self, pipeline):
        pipeline.create_lead(brand="micro_saas", email="a@test.com")
        pipeline.create_lead(brand="micro_saas", email="b@test.com")
        pipeline.create_lead(brand="newsletter", email="c@test.com")

        saas_leads = pipeline.get_leads_by_brand("micro_saas")
        assert len(saas_leads) == 2

        newsletter_leads = pipeline.get_leads_by_brand("newsletter")
        assert len(newsletter_leads) == 1

    def test_get_leads_by_brand_and_stage(self, pipeline):
        lid1 = pipeline.create_lead(brand="micro_saas", email="a@test.com")
        lid2 = pipeline.create_lead(brand="micro_saas", email="b@test.com")
        pipeline.advance_stage(lid1, "lead")

        leads = pipeline.get_leads_by_brand("micro_saas", stage="lead")
        assert len(leads) == 1
        assert leads[0]["email"] == "a@test.com"


class TestStageAdvancement:
    def test_advance_stage(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        result = pipeline.advance_stage(lead_id, "click")
        assert result["status"] == "advanced"
        assert result["from"] == "impression"
        assert result["to"] == "click"

    def test_advance_to_customer_sets_converted_at(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        pipeline.advance_stage(lead_id, "prospect")
        pipeline.advance_stage(lead_id, "customer")

        lead = pipeline.get_lead(lead_id)
        assert lead["converted_at"] is not None

    def test_advance_invalid_stage(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        result = pipeline.advance_stage(lead_id, "invalid")
        assert result["status"] == "invalid_stage"

    def test_advance_not_found(self, pipeline):
        result = pipeline.advance_stage(999, "click")
        assert result["status"] == "not_found"

    def test_score_lead(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        pipeline.score_lead(lead_id, 75)

        lead = pipeline.get_lead(lead_id)
        assert lead["score"] == 75

    def test_score_lead_clamped(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        pipeline.score_lead(lead_id, 150)

        lead = pipeline.get_lead(lead_id)
        assert lead["score"] == 100

    def test_score_lead_min_zero(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        pipeline.score_lead(lead_id, -10)

        lead = pipeline.get_lead(lead_id)
        assert lead["score"] == 0


class TestHotLeads:
    def test_get_hot_leads(self, pipeline):
        lid1 = pipeline.create_lead(brand="micro_saas", email="hot@test.com")
        lid2 = pipeline.create_lead(brand="micro_saas", email="cold@test.com")
        pipeline.score_lead(lid1, 80)
        pipeline.score_lead(lid2, 20)

        hot = pipeline.get_hot_leads()
        assert len(hot) == 1
        assert hot[0]["email"] == "hot@test.com"

    def test_hot_leads_excludes_customers(self, pipeline):
        lid = pipeline.create_lead(brand="micro_saas", email="customer@test.com")
        pipeline.score_lead(lid, 90)
        pipeline.advance_stage(lid, "customer")

        hot = pipeline.get_hot_leads()
        assert len(hot) == 0

    def test_hot_leads_filter_by_brand(self, pipeline):
        lid1 = pipeline.create_lead(brand="micro_saas", email="a@test.com")
        lid2 = pipeline.create_lead(brand="newsletter", email="b@test.com")
        pipeline.score_lead(lid1, 80)
        pipeline.score_lead(lid2, 80)

        hot = pipeline.get_hot_leads(brand="micro_saas")
        assert len(hot) == 1
        assert hot[0]["brand"] == "micro_saas"


# ── Events ────────────────────────────────────────────────────


class TestEvents:
    def test_log_event(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        event_id = pipeline.log_event(lead_id, "page_view", {"url": "/pricing"})
        assert event_id > 0

    def test_get_events(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        pipeline.log_event(lead_id, "page_view", {"url": "/pricing"})
        pipeline.log_event(lead_id, "email_open")

        events = pipeline.get_events(lead_id)
        assert len(events) == 2


# ── Revenue ───────────────────────────────────────────────────


class TestRevenue:
    def test_record_revenue(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        rev_id = pipeline.record_revenue(
            lead_id, "micro_saas", 49.99, product="Pro Plan",
        )
        assert rev_id > 0

    def test_revenue_auto_advances_to_customer(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        pipeline.record_revenue(lead_id, "micro_saas", 49.99)

        lead = pipeline.get_lead(lead_id)
        assert lead["stage"] == "customer"

    def test_revenue_logs_purchase_event(self, pipeline):
        lead_id = pipeline.create_lead(brand="micro_saas")
        pipeline.record_revenue(lead_id, "micro_saas", 49.99, product="Pro Plan")

        events = pipeline.get_events(lead_id)
        purchase_events = [e for e in events if e["event_type"] == "purchase"]
        assert len(purchase_events) == 1

    def test_get_revenue_by_brand(self, pipeline):
        lid = pipeline.create_lead(brand="micro_saas")
        pipeline.record_revenue(lid, "micro_saas", 49.99)
        pipeline.record_revenue(lid, "micro_saas", 29.99)

        rev = pipeline.get_revenue_by_brand("micro_saas")
        assert rev["transactions"] == 2
        assert abs(rev["total_revenue"] - 79.98) < 0.01

    def test_get_revenue_by_brand_empty(self, pipeline):
        rev = pipeline.get_revenue_by_brand("nonexistent")
        assert rev["transactions"] == 0
        assert rev["total_revenue"] == 0

    def test_get_revenue_by_source(self, pipeline):
        lid = pipeline.create_lead(brand="micro_saas", source_platform="twitter")
        pipeline.record_revenue(lid, "micro_saas", 49.99)

        sources = pipeline.get_revenue_by_source()
        assert len(sources) == 1
        assert sources[0]["source_platform"] == "twitter"
        assert sources[0]["total_revenue"] == 49.99


# ── Funnel Analytics ──────────────────────────────────────────


class TestFunnel:
    def test_get_funnel(self, pipeline):
        pipeline.create_lead(brand="micro_saas")
        pipeline.create_lead(brand="micro_saas")
        lid3 = pipeline.create_lead(brand="micro_saas")
        pipeline.advance_stage(lid3, "lead")

        funnel = pipeline.get_funnel("micro_saas")
        assert funnel["impression"] == 2
        assert funnel["lead"] == 1

    def test_get_funnel_empty(self, pipeline):
        funnel = pipeline.get_funnel("nonexistent")
        for stage in STAGES:
            assert funnel[stage] == 0

    def test_conversion_rates(self, pipeline):
        for _ in range(10):
            pipeline.create_lead(brand="micro_saas")
        lid = pipeline.create_lead(brand="micro_saas")
        pipeline.advance_stage(lid, "click")

        rates = pipeline.get_conversion_rates("micro_saas")
        assert "impression_to_click" in rates
        # 1 click out of 11 total (10 impression + 1 click)
        # But funnel counts current stage, so 10 at impression, 1 at click
        assert rates["impression_to_click"] == pytest.approx(0.1, abs=0.01)

    def test_all_brands_funnel(self, pipeline):
        pipeline.create_lead(brand="micro_saas")
        pipeline.create_lead(brand="newsletter")

        funnels = pipeline.get_all_brands_funnel()
        assert "micro_saas" in funnels
        assert "newsletter" in funnels

    def test_attribution_summary(self, pipeline):
        lid = pipeline.create_lead(
            brand="micro_saas", source_platform="twitter",
        )
        pipeline.record_revenue(lid, "micro_saas", 49.99)

        attr = pipeline.get_attribution_summary()
        assert len(attr) >= 1
        assert attr[0]["revenue"] == 49.99
