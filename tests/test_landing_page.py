"""Tests for the crowdfunding landing page generator.

Covers:
- Template placeholder replacement
- Funding progress from DB
- Contribution recording and campaign updates
- Schema creation
- Preview generation
- Edge cases (no DB, empty campaigns, goal reached)
"""

import pytest
from pathlib import Path

from monai.config import Config
from monai.db.database import Database
from monai.web.landing.generator import (
    generate,
    generate_preview,
    record_contribution,
    ensure_crowdfunding_schema,
    _get_funding_progress,
    _build_stripe_link,
    _get_monero_address,
)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    ensure_crowdfunding_schema(d)
    return d


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def output_path(tmp_path):
    return tmp_path / "output" / "index.html"


class TestSchemaCreation:
    def test_creates_tables(self, db):
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('crowdfunding_campaigns', 'crowdfunding_contributions') "
            "ORDER BY name"
        )
        assert len(tables) == 2

    def test_idempotent(self, db):
        """Calling ensure_schema twice should not error."""
        ensure_crowdfunding_schema(db)
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name = 'crowdfunding_campaigns'"
        )
        assert len(tables) == 1


class TestFundingProgress:
    def test_default_when_empty(self, db):
        progress = _get_funding_progress(db)
        assert progress["raised"] == 0
        assert progress["goal"] == 500
        assert progress["backers"] == 0

    def test_from_campaign_table(self, db):
        db.execute_insert(
            "INSERT INTO crowdfunding_campaigns "
            "(name, goal_amount, raised_amount, backer_count, status) "
            "VALUES ('Test', 1000, 250, 5, 'active')"
        )
        progress = _get_funding_progress(db)
        assert progress["raised"] == 250
        assert progress["goal"] == 1000
        assert progress["backers"] == 5

    def test_from_contributions_fallback(self, db):
        """When no campaigns exist, sum contributions directly."""
        db.execute_insert(
            "INSERT INTO crowdfunding_contributions "
            "(amount, currency, status) VALUES (50, 'EUR', 'completed')"
        )
        db.execute_insert(
            "INSERT INTO crowdfunding_contributions "
            "(amount, currency, status) VALUES (30, 'EUR', 'completed')"
        )
        progress = _get_funding_progress(db)
        assert progress["raised"] == 80
        assert progress["backers"] == 2


class TestGenerate:
    def test_generates_html(self, config, db, output_path):
        result = generate(config, db, output_path)
        assert result.exists()
        content = result.read_text()
        assert "monAI" in content
        assert "{{" not in content  # No unreplaced placeholders

    def test_funding_data_in_output(self, config, db, output_path):
        db.execute_insert(
            "INSERT INTO crowdfunding_campaigns "
            "(name, goal_amount, raised_amount, backer_count, status) "
            "VALUES ('Test', 500, 123, 7, 'active')"
        )
        result = generate(config, db, output_path)
        content = result.read_text()
        assert "123" in content  # raised amount
        assert "7" in content  # backer count

    def test_stripe_links_replaced(self, config, db, output_path):
        links = {
            10: "https://buy.stripe.com/abc10",
            50: "https://buy.stripe.com/abc50",
            200: "https://buy.stripe.com/abc200",
        }
        result = generate(config, db, output_path, stripe_links=links)
        content = result.read_text()
        assert "buy.stripe.com/abc10" in content
        assert "buy.stripe.com/abc50" in content
        assert "buy.stripe.com/abc200" in content

    def test_monero_address_replaced(self, config, db, output_path):
        addr = "4" + "A" * 94
        result = generate(config, db, output_path, monero_address=addr)
        content = result.read_text()
        assert addr in content

    def test_no_db(self, config, output_path):
        """Should work without a database, using defaults."""
        result = generate(config, db=None, output_path=output_path)
        content = result.read_text()
        assert "monAI" in content


class TestGeneratePreview:
    def test_preview_generation(self, tmp_path):
        output = tmp_path / "preview.html"
        result = generate_preview(output)
        assert result.exists()
        content = result.read_text()
        assert "127" in content  # Demo raised amount
        assert "14" in content  # Demo backer count
        assert "{{" not in content


class TestStripeLinkBuilder:
    def test_empty_url_returns_anchor(self):
        assert _build_stripe_link("", 50) == "#tier-50"

    def test_payment_link_returned_as_is(self):
        url = "https://buy.stripe.com/abc123"
        assert _build_stripe_link(url, 50) == url

    def test_checkout_link_returned_as_is(self):
        url = "https://checkout.stripe.com/sess123"
        assert _build_stripe_link(url, 50) == url

    def test_template_url_fills_amount(self):
        url = "https://example.com/pay?amount={amount}"
        assert _build_stripe_link(url, 50) == "https://example.com/pay?amount=5000"


class TestMoneroAddress:
    def test_returns_config_address(self):
        config = Config()
        config.creator_wallet.xmr_address = "4" + "B" * 94
        assert _get_monero_address(config) == "4" + "B" * 94

    def test_returns_placeholder_when_empty(self):
        config = Config()
        config.creator_wallet.xmr_address = ""
        addr = _get_monero_address(config)
        assert "will appear" in addr


class TestRecordContribution:
    def test_creates_campaign_if_none(self, db):
        contrib_id = record_contribution(db, 50.0, tier="Early Supporter")
        assert contrib_id > 0

        campaigns = db.execute("SELECT * FROM crowdfunding_campaigns")
        assert len(campaigns) == 1
        assert campaigns[0]["raised_amount"] == 50.0
        assert campaigns[0]["backer_count"] == 1

    def test_updates_existing_campaign(self, db):
        db.execute_insert(
            "INSERT INTO crowdfunding_campaigns "
            "(name, goal_amount, raised_amount, backer_count, status) "
            "VALUES ('Test', 500, 100, 3, 'active')"
        )

        record_contribution(db, 25.0, payment_ref="ref_001")

        campaign = db.execute("SELECT * FROM crowdfunding_campaigns")[0]
        assert campaign["raised_amount"] == 125.0
        assert campaign["backer_count"] == 4

    def test_marks_funded_when_goal_reached(self, db):
        db.execute_insert(
            "INSERT INTO crowdfunding_campaigns "
            "(name, goal_amount, raised_amount, backer_count, status) "
            "VALUES ('Test', 100, 90, 9, 'active')"
        )

        record_contribution(db, 15.0)

        campaign = db.execute("SELECT * FROM crowdfunding_campaigns")[0]
        assert campaign["status"] == "funded"
        assert campaign["raised_amount"] == 105.0

    def test_multiple_contributions(self, db):
        for i in range(5):
            record_contribution(
                db, 10.0 + i,
                backer_email=f"user{i}@example.com",
                tier="Early Supporter",
            )

        campaign = db.execute("SELECT * FROM crowdfunding_campaigns")[0]
        assert campaign["backer_count"] == 5
        assert campaign["raised_amount"] == sum(10.0 + i for i in range(5))

        contribs = db.execute("SELECT * FROM crowdfunding_contributions")
        assert len(contribs) == 5

    def test_contribution_stores_details(self, db):
        record_contribution(
            db, 50.0,
            currency="EUR",
            payment_ref="pi_test_123",
            backer_email="test@example.com",
            tier="Builder",
        )
        contrib = db.execute("SELECT * FROM crowdfunding_contributions")[0]
        assert contrib["amount"] == 50.0
        assert contrib["payment_ref"] == "pi_test_123"
        assert contrib["backer_email"] == "test@example.com"
        assert contrib["tier"] == "Builder"
        assert contrib["status"] == "completed"


class TestQRCodeInTemplate:
    def test_canvas_element_present(self, config, db, output_path):
        """Verify the QR canvas element is in the generated HTML."""
        result = generate(config, db, output_path)
        content = result.read_text()
        assert 'id="monero-qr"' in content
        assert "<canvas" in content
        assert "drawQR" in content

    def test_qr_generator_js_present(self, config, db, output_path):
        """Verify the QR drawing function exists."""
        result = generate(config, db, output_path)
        content = result.read_text()
        assert "function drawQR" in content
        assert "getContext" in content
