"""Tests for Telegram Affiliate strategy agent."""

import json
from unittest.mock import MagicMock, patch

import pytest

from monai.strategies.telegram_affiliate import (
    DEAL_SOURCES,
    HIGH_COMMISSION_CATEGORIES,
    TelegramAffiliateAgent,
)


@pytest.fixture
def mock_config(tmp_path):
    config = MagicMock()
    config.data_dir = tmp_path
    config.llm.model = "test-model"
    config.llm.cheap_model = "test-cheap"
    return config


@pytest.fixture
def db(tmp_path):
    from monai.db.database import Database
    return Database(tmp_path / "test.db")


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.chat.return_value = "Promo content for social"
    llm.chat_json.return_value = {"recommendations": []}
    llm.quick.return_value = "Quick response"
    return llm


@pytest.fixture
def agent(mock_config, db, mock_llm):
    """Create TelegramAffiliateAgent with external deps mocked."""
    agent = TelegramAffiliateAgent(mock_config, db, mock_llm)

    # Mock identity (not testing account provisioning)
    agent._identity = MagicMock()
    agent._identity.get_api_key.side_effect = lambda key: {
        "telegram_channel_bot": "123:ABC",
        "telegram_channel_username": "@testdeals",
        "amazon_affiliate_tag": "monai-21",
    }.get(key, "")
    agent._identity.get_platform_credentials.return_value = None

    # Mock browser/executor (not testing real browsing)
    agent.browse_and_extract = MagicMock(return_value={
        "status": "completed",
        "result": {"deals": [
            {
                "product_name": "Cuffie Sony WH-1000XM5",
                "product_url": "https://www.amazon.it/dp/B09Y2GBX7V",
                "original_price": 399.99,
                "deal_price": 249.99,
                "discount_pct": 37,
                "image_url": "https://img.amazon.it/sony.jpg",
                "category": "electronics",
            },
            {
                "product_name": "Set Pentole Lagostina",
                "product_url": "https://www.amazon.it/dp/B08ABCDEF",
                "original_price": 89.99,
                "deal_price": 39.99,
                "discount_pct": 55,
                "image_url": "https://img.amazon.it/pots.jpg",
                "category": "kitchen",
            },
        ]},
    })
    agent.execute_task = MagicMock(return_value={"status": "completed"})
    agent.platform_action = MagicMock(return_value={"status": "completed"})
    agent.think = MagicMock(return_value="Scopri le offerte migliori su @testdeals!")
    agent.think_json = MagicMock(return_value={
        "recommendations": [{"action": "post more kitchen deals", "expected_impact": "high"}],
    })
    agent.log_action = MagicMock()
    agent.run_step = MagicMock(side_effect=lambda step, fn, *a, **kw: fn(*a, **kw))

    # Mock channel client
    mock_channel = MagicMock()
    mock_channel.post_message.return_value = {"message_id": 1}
    mock_channel.post_photo.return_value = {"message_id": 2}
    mock_channel.get_post_stats.return_value = {
        "total_posts": 10, "deal_posts": 8, "growth_posts": 2,
        "subscriber_count": 150,
    }
    agent._channel = mock_channel

    return agent


def _seed_deals(db, count=3, status="sourced", score=0):
    """Insert test deals into the DB."""
    deals = []
    for i in range(count):
        db.execute_insert(
            "INSERT INTO affiliate_deals "
            "(source, product_name, product_url, original_price, deal_price, "
            "discount_pct, image_url, category, score, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "Amazon IT",
                f"Product {i}",
                f"https://www.amazon.it/dp/B0{i:08d}",
                99.99,
                49.99 + i,
                50 - i,
                f"https://img.example.com/{i}.jpg",
                "kitchen",
                score,
                status,
            ),
        )
        deals.append(i)
    return deals


class TestDealScoring:
    def test_high_discount_high_score(self, agent):
        deal = {
            "discount_pct": 55, "deal_price": 49.99,
            "category": "kitchen", "image_url": "http://img", "source": "Amazon IT",
        }
        score = agent._calculate_deal_score(deal)
        # 40 (discount>=50) + 20 (price 10-100) + 20 (kitchen=high commission) + 10 (image) + 10 (amazon)
        assert score == 100

    def test_low_discount_low_score(self, agent):
        deal = {
            "discount_pct": 5, "deal_price": 500,
            "category": "unknown", "image_url": "", "source": "other",
        }
        score = agent._calculate_deal_score(deal)
        assert score == 0

    def test_medium_discount(self, agent):
        deal = {
            "discount_pct": 25, "deal_price": 30,
            "category": "sport", "image_url": "http://img", "source": "Pepper.it",
        }
        score = agent._calculate_deal_score(deal)
        # 20 (discount 20-30) + 20 (price 10-100) + 20 (sport) + 10 (image) + 8 (pepper)
        assert score == 78

    def test_score_capped_at_100(self, agent):
        deal = {
            "discount_pct": 99, "deal_price": 50,
            "category": "fashion", "image_url": "http://x", "source": "Amazon IT",
        }
        assert agent._calculate_deal_score(deal) == 100

    def test_missing_fields_dont_crash(self, agent):
        score = agent._calculate_deal_score({})
        assert score == 0

    def test_none_values_handled(self, agent):
        deal = {
            "discount_pct": None, "deal_price": None,
            "category": None, "image_url": None, "source": None,
        }
        score = agent._calculate_deal_score(deal)
        assert score == 0


class TestAffiliateURL:
    def test_amazon_url_gets_tag(self, agent):
        url = "https://www.amazon.it/dp/B09Y2GBX7V"
        result = agent._make_affiliate_url(url)
        assert result == "https://www.amazon.it/dp/B09Y2GBX7V?tag=monai-21"

    def test_amazon_url_with_existing_params(self, agent):
        url = "https://www.amazon.it/dp/B09Y2GBX7V?ref=deals"
        result = agent._make_affiliate_url(url)
        assert "tag=monai-21" in result
        assert "&tag=" in result

    def test_existing_tag_replaced(self, agent):
        url = "https://www.amazon.it/dp/B09Y2GBX7V?tag=oldtag-20"
        result = agent._make_affiliate_url(url)
        assert "tag=monai-21" in result
        assert "oldtag-20" not in result

    def test_non_amazon_url_unchanged(self, agent):
        url = "https://www.pepper.it/deals/12345"
        result = agent._make_affiliate_url(url)
        assert result == url

    def test_no_tag_returns_original(self, agent):
        agent._affiliate_tag = ""
        agent._identity.get_api_key.side_effect = lambda k: ""
        url = "https://www.amazon.it/dp/B09Y2GBX7V"
        result = agent._make_affiliate_url(url)
        assert result == url


class TestDealSourcing:
    def test_source_deals_saves_to_db(self, agent, db):
        agent._source_deals()

        rows = db.execute("SELECT * FROM affiliate_deals ORDER BY id")
        # browse_and_extract returns 2 deals, called for each source
        assert len(rows) >= 2

    def test_source_deals_skips_duplicates(self, agent, db):
        # Source twice — same URLs should not duplicate
        agent._source_deals()
        count1 = len(db.execute("SELECT * FROM affiliate_deals"))
        agent._source_deals()
        count2 = len(db.execute("SELECT * FROM affiliate_deals"))
        assert count2 == count1  # UNIQUE(product_url) prevents dupes

    def test_source_deals_scores_and_approves(self, agent, db):
        agent._source_deals()

        approved = db.execute(
            "SELECT * FROM affiliate_deals WHERE status = 'approved'"
        )
        # The kitchen deal with 55% discount should score >=50 and be approved
        assert len(approved) >= 1
        for a in approved:
            assert dict(a)["score"] >= 50

    def test_source_deals_generates_affiliate_urls(self, agent, db):
        agent._source_deals()

        approved = db.execute(
            "SELECT affiliate_url FROM affiliate_deals WHERE status = 'approved'"
        )
        for row in approved:
            url = dict(row)["affiliate_url"]
            if "amazon" in url:
                assert "tag=monai-21" in url

    def test_browse_error_doesnt_crash(self, agent, db):
        agent.browse_and_extract.side_effect = Exception("Network error")
        result = agent._source_deals()
        assert result["deals_found"] == 0


class TestDealPosting:
    def test_post_approved_deals(self, agent, db):
        _seed_deals(db, count=3, status="approved", score=80)

        result = agent._post_deals()

        assert result["deals_posted"] == 3
        # Verify status updated in DB
        posted = db.execute("SELECT * FROM affiliate_deals WHERE status = 'posted'")
        assert len(posted) == 3

    def test_post_deal_with_image_uses_photo(self, agent, db):
        _seed_deals(db, count=1, status="approved", score=80)

        agent._post_deals()

        # All seeded deals have images
        agent._channel.post_photo.assert_called()

    def test_post_deal_without_image_uses_text(self, agent, db):
        db.execute_insert(
            "INSERT INTO affiliate_deals "
            "(source, product_name, product_url, original_price, deal_price, "
            "discount_pct, image_url, category, score, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("Amazon", "Gadget", "https://amazon.it/dp/X", 50, 25, 50, "", "tools", 80, "approved"),
        )

        agent._post_deals()

        agent._channel.post_message.assert_called()

    def test_no_channel_returns_not_ready(self, agent):
        agent._channel = None
        # Override side_effect so all keys return None
        agent._identity.get_api_key.side_effect = None
        agent._identity.get_api_key.return_value = None
        # execute_task fails to create bot/channel
        agent.execute_task = MagicMock(return_value={"status": "failed"})

        result = agent._post_deals()
        assert result["status"] == "channel_not_ready"

    def test_post_error_doesnt_stop_batch(self, agent, db):
        _seed_deals(db, count=3, status="approved", score=80)
        # First post fails, rest succeed
        agent._channel.post_photo.side_effect = [
            Exception("API error"), {"message_id": 2}, {"message_id": 3},
        ]

        result = agent._post_deals()
        assert result["deals_posted"] == 2


class TestPostFormatting:
    def test_format_includes_product_name(self, agent):
        deal = {
            "product_name": "Cuffie Sony",
            "original_price": 399.99,
            "deal_price": 249.99,
            "discount_pct": 37,
            "affiliate_url": "https://amazon.it/dp/X?tag=monai-21",
            "category": "electronics",
        }
        msg = agent._format_deal_post(deal)
        assert "Cuffie Sony" in msg
        assert "<b>" in msg  # HTML formatting

    def test_format_shows_price_comparison(self, agent):
        deal = {
            "product_name": "Test",
            "original_price": 100.0,
            "deal_price": 50.0,
            "discount_pct": 50,
            "affiliate_url": "https://example.com",
        }
        msg = agent._format_deal_post(deal)
        assert "100.00" in msg
        assert "50.00" in msg
        assert "-50%" in msg
        assert "<s>" in msg  # Strikethrough for old price

    def test_format_includes_affiliate_link(self, agent):
        deal = {
            "product_name": "Test",
            "deal_price": 25.0,
            "affiliate_url": "https://amazon.it/dp/X?tag=monai-21",
        }
        msg = agent._format_deal_post(deal)
        assert "tag=monai-21" in msg
        assert "Vai all'offerta" in msg

    def test_format_only_deal_price(self, agent):
        deal = {"product_name": "Test", "deal_price": 19.99}
        msg = agent._format_deal_post(deal)
        assert "19.99" in msg
        assert "<s>" not in msg  # No strikethrough without original


class TestGrowthEngine:
    def test_cross_post_social(self, agent, db):
        # Give agent a platform credential
        agent._identity.get_platform_credentials.side_effect = lambda p: (
            {"token": "fake"} if p == "twitter" else None
        )

        result = agent._cross_post_social()
        assert "twitter" in result["platforms"]

    def test_cross_post_no_credentials(self, agent, db):
        result = agent._cross_post_social()
        assert result["platforms"] == []

    def test_update_landing_page(self, agent, mock_config):
        result = agent._update_landing_page()
        assert result["status"] == "updated"

        # Verify JSON file written
        landing_path = mock_config.data_dir / "telegram_landing.json"
        assert landing_path.exists()
        data = json.loads(landing_path.read_text())
        assert data["channel"] == "@testdeals"

    def test_analyze_growth(self, agent, db):
        result = agent._analyze_growth()
        assert "weekly_stats" in result
        assert result["weekly_stats"]["subscriber_count"] == 150

    def test_growth_records_actions(self, agent, db):
        agent._identity.get_platform_credentials.side_effect = lambda p: (
            {"token": "x"} if p == "twitter" else None
        )
        agent._cross_post_social()

        rows = db.execute("SELECT * FROM channel_growth_actions")
        assert len(rows) == 1
        assert dict(rows[0])["action_type"] == "social_post"


class TestPlanAndRun:
    def test_plan_with_no_deals_sources(self, agent, db):
        steps = agent.plan()
        assert "source_deals" in steps

    def test_plan_with_approved_deals_posts(self, agent, db):
        _seed_deals(db, count=5, status="approved", score=80)
        steps = agent.plan()
        assert "post_deals" in steps

    def test_plan_growth_every_third_cycle(self, agent, db):
        # Simulate cycle count divisible by 3
        db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
            ("telegram_affiliate", "test", "x"),
        )
        db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
            ("telegram_affiliate", "test", "x"),
        )
        db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
            ("telegram_affiliate", "test", "x"),
        )
        steps = agent.plan()
        assert "grow_audience" in steps

    def test_run_executes_all_steps(self, agent, db):
        _seed_deals(db, count=3, status="approved", score=80)

        result = agent.run()

        assert isinstance(result, dict)
        # run_step was called for each planned step
        assert agent.run_step.call_count >= 1

    def test_run_logs_start_and_complete(self, agent, db):
        agent.run()

        log_calls = [c[0] for c in agent.log_action.call_args_list]
        actions = [c[0] for c in log_calls]
        assert "run_start" in actions
        assert "run_complete" in actions


class TestSaveDeal:
    def test_save_deal_basic(self, agent, db):
        agent._save_deal({
            "product_name": "Test Product",
            "product_url": "https://amazon.it/dp/X",
            "original_price": 100,
            "deal_price": 50,
            "discount_pct": 50,
        }, "Amazon IT")

        rows = db.execute("SELECT * FROM affiliate_deals")
        assert len(rows) == 1
        d = dict(rows[0])
        assert d["product_name"] == "Test Product"
        assert d["discount_pct"] == 50

    def test_save_deal_calculates_missing_discount(self, agent, db):
        agent._save_deal({
            "product_name": "Test",
            "product_url": "https://amazon.it/dp/Y",
            "original_price": 100,
            "deal_price": 70,
            "discount_pct": 0,
        }, "Test")

        rows = db.execute("SELECT discount_pct FROM affiliate_deals")
        assert dict(rows[0])["discount_pct"] == 30.0

    def test_save_deal_empty_url_skipped(self, agent, db):
        agent._save_deal({"product_name": "Test", "product_url": ""}, "Test")

        rows = db.execute("SELECT * FROM affiliate_deals")
        assert len(rows) == 0

    def test_save_deal_truncates_long_name(self, agent, db):
        agent._save_deal({
            "product_name": "x" * 500,
            "product_url": "https://example.com/long",
        }, "Test")

        rows = db.execute("SELECT product_name FROM affiliate_deals")
        assert len(dict(rows[0])["product_name"]) == 200
