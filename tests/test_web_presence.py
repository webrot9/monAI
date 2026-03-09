"""Tests for WebPresence agent — per-brand websites and landing pages."""

import json
from unittest.mock import MagicMock

import pytest

from monai.agents.web_presence import WebPresence
from monai.config import Config
from monai.db.database import Database
from tests.conftest_schema import TEST_SCHEMA


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    with d.connect() as conn:
        conn.executescript(TEST_SCHEMA)
    return d


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def llm():
    mock = MagicMock()
    mock.quick.return_value = "mocked"
    mock.chat_json.return_value = {
        "headline": "Build Faster With Us",
        "subheadline": "The modern way to ship software",
        "features": [
            {"icon": "rocket", "title": "Fast", "description": "Ship in days"},
        ],
        "social_proof": "Trusted by 1000+ devs",
        "cta_text": "Get Started Free",
        "meta_description": "Build and ship software faster.",
        "meta_keywords": "saas, tools, software",
    }
    return mock


@pytest.fixture
def agent(config, db, llm):
    return WebPresence(config, db, llm)


# ── Schema ────────────────────────────────────────────────────


class TestSchema:
    def test_creates_tables(self, agent, db):
        for table in ("brand_websites", "brand_pages", "web_analytics"):
            rows = db.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            assert len(rows) == 1, f"Table {table} not created"

    def test_plan(self, agent):
        steps = agent.plan()
        assert len(steps) >= 4


# ── Site Management ───────────────────────────────────────────


class TestSiteManagement:
    def test_register_site(self, agent):
        result = agent.register_site(
            "micro_saas", "quicktools.dev",
            registrar="namecheap", hosting="vercel",
        )
        assert result["site_id"] > 0
        assert result["status"] == "planned"
        assert result["domain"] == "quicktools.dev"

    def test_register_duplicate_ignored(self, agent):
        agent.register_site("micro_saas", "quicktools.dev")
        result = agent.register_site("micro_saas", "quicktools.dev")
        # INSERT OR IGNORE returns 0 on duplicate
        assert result["status"] == "planned"

    def test_activate_site(self, agent):
        agent.register_site("micro_saas", "quicktools.dev")
        result = agent.activate_site(
            "micro_saas", "quicktools.dev", analytics_id="G-123456",
        )
        assert result["status"] == "live"

    def test_get_brand_sites(self, agent):
        agent.register_site("micro_saas", "quicktools.dev")
        agent.register_site("micro_saas", "quicktools.io")

        sites = agent._get_brand_sites("micro_saas")
        assert len(sites) == 2

    def test_get_all_sites(self, agent):
        agent.register_site("micro_saas", "quicktools.dev")
        agent.register_site("newsletter", "weeklydigest.com")

        sites = agent.get_all_sites()
        assert len(sites) == 2

    def test_brand_isolation(self, agent):
        agent.register_site("micro_saas", "quicktools.dev")
        agent.register_site("newsletter", "weeklydigest.com")

        saas_sites = agent._get_brand_sites("micro_saas")
        assert len(saas_sites) == 1
        assert saas_sites[0]["domain"] == "quicktools.dev"


# ── Page Management ───────────────────────────────────────────


class TestPageManagement:
    def test_create_page(self, agent):
        result = agent.register_site("micro_saas", "quicktools.dev")
        site_id = result["site_id"]

        page_id = agent.create_page(
            site_id, "micro_saas", "/", "landing",
            title="Welcome", content="<h1>Hello</h1>",
        )
        assert page_id > 0

    def test_generate_landing_page(self, agent):
        result = agent.register_site("micro_saas", "quicktools.dev")
        site_id = result["site_id"]

        page = agent.generate_landing_page("micro_saas", site_id)
        assert page["page_id"] > 0
        assert "headline" in page["content"]

    def test_get_brand_pages(self, agent):
        result = agent.register_site("micro_saas", "quicktools.dev")
        site_id = result["site_id"]

        agent.create_page(site_id, "micro_saas", "/", "landing")
        agent.create_page(site_id, "micro_saas", "/pricing", "pricing")

        pages = agent.get_brand_pages("micro_saas")
        assert len(pages) == 2

    def test_publish_page(self, agent):
        result = agent.register_site("micro_saas", "quicktools.dev")
        site_id = result["site_id"]
        page_id = agent.create_page(site_id, "micro_saas", "/", "landing")

        publish_result = agent.publish_page(page_id)
        assert publish_result["status"] == "published"

    def test_plan_pages_creates_landing(self, agent):
        result = agent.register_site("micro_saas", "quicktools.dev")
        agent.activate_site("micro_saas", "quicktools.dev")

        sites = agent._get_brand_sites("micro_saas")
        pages = agent._plan_pages("micro_saas", sites[0])
        assert len(pages) == 1  # Landing page created

    def test_plan_pages_skips_existing_landing(self, agent):
        result = agent.register_site("micro_saas", "quicktools.dev")
        site_id = result["site_id"]
        agent.activate_site("micro_saas", "quicktools.dev")
        agent.create_page(site_id, "micro_saas", "/", "landing")

        sites = agent._get_brand_sites("micro_saas")
        pages = agent._plan_pages("micro_saas", sites[0])
        assert len(pages) == 0  # Already has landing


# ── Analytics ─────────────────────────────────────────────────


class TestAnalytics:
    def test_record_analytics(self, agent):
        result = agent.register_site("micro_saas", "quicktools.dev")
        site_id = result["site_id"]

        rec_id = agent.record_analytics(
            site_id, "micro_saas", "2026-03-09",
            page_views=150, unique_visitors=80,
            bounce_rate=0.45, conversions=3,
        )
        assert rec_id > 0

    def test_get_analytics(self, agent):
        result = agent.register_site("micro_saas", "quicktools.dev")
        site_id = result["site_id"]

        agent.record_analytics(site_id, "micro_saas", "2026-03-08", page_views=100)
        agent.record_analytics(site_id, "micro_saas", "2026-03-09", page_views=150)

        analytics = agent.get_analytics("micro_saas", days=30)
        assert len(analytics) == 2

    def test_get_analytics_summary(self, agent):
        result = agent.register_site("micro_saas", "quicktools.dev")
        site_id = result["site_id"]

        agent.record_analytics(site_id, "micro_saas", "2026-03-08",
                               page_views=100, unique_visitors=50, conversions=2)
        agent.record_analytics(site_id, "micro_saas", "2026-03-09",
                               page_views=200, unique_visitors=100, conversions=5)

        summary = agent.get_analytics_summary("micro_saas")
        assert "micro_saas" in summary
        assert summary["micro_saas"]["total_views"] == 300
        assert summary["micro_saas"]["total_visitors"] == 150
        assert summary["micro_saas"]["total_conversions"] == 7

    def test_analytics_summary_all_brands(self, agent):
        r1 = agent.register_site("micro_saas", "quicktools.dev")
        r2 = agent.register_site("newsletter", "weeklydigest.com")

        agent.record_analytics(r1["site_id"], "micro_saas", "2026-03-09", page_views=100)
        agent.record_analytics(r2["site_id"], "newsletter", "2026-03-09", page_views=50)

        summary = agent.get_analytics_summary()
        assert "micro_saas" in summary
        assert "newsletter" in summary


# ── Run Cycle ─────────────────────────────────────────────────


class TestRunCycle:
    def test_run_no_sites(self, agent):
        result = agent.run(brand="micro_saas")
        assert result["brands_processed"] == 1

    def test_run_with_live_site(self, agent):
        agent.register_site("micro_saas", "quicktools.dev")
        agent.activate_site("micro_saas", "quicktools.dev")

        result = agent.run(brand="micro_saas")
        assert result["brands_processed"] == 1
        per_brand = result["per_brand"]["micro_saas"]
        assert per_brand["sites"] == 1
        assert per_brand["live"] == 1
        assert per_brand["pages_created"] >= 1
