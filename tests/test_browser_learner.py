"""Tests for the Browser Learner (adaptive automation)."""

import json
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from monai.config import Config
from monai.db.database import Database
from monai.agents.browser_learner import BrowserLearner


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    return llm


class TestBrowserLearner:
    def test_schema_created(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='browser_actions'"
        )
        assert len(rows) == 1
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='site_playbooks'"
        )
        assert len(rows) == 1

    def test_log_action(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        bl._log_action("example.com", "navigate", None, "https://example.com",
                        True, duration=500)
        rows = db.execute("SELECT * FROM browser_actions")
        assert len(rows) == 1
        assert rows[0]["domain"] == "example.com"
        assert rows[0]["success"] == 1

    def test_log_failure(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        bl._log_action("example.com", "navigate", None, "https://example.com",
                        False, failure_type="captcha", error_message="CAPTCHA detected",
                        duration=1200)
        rows = db.execute("SELECT * FROM browser_actions WHERE success = 0")
        assert len(rows) == 1
        assert rows[0]["failure_type"] == "captcha"

    def test_detect_captcha(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        page_info = {"text": "Please solve the CAPTCHA to continue", "title": "Verify"}
        assert bl._detect_failure(page_info) == "captcha"

    def test_detect_bot_detection(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        page_info = {"text": "Access denied. Suspicious activity detected.", "title": "Blocked"}
        assert bl._detect_failure(page_info) == "bot_detection"

    def test_detect_auth_required(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        page_info = {"text": "Welcome to our platform", "title": "Sign In - Platform"}
        assert bl._detect_failure(page_info) == "auth_required"

    def test_detect_no_failure(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        page_info = {"text": "Welcome to our blog. Read our latest articles.", "title": "Blog Home"}
        assert bl._detect_failure(page_info) is None

    def test_classify_timeout_error(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        assert bl._classify_error(TimeoutError("Connection timeout")) == "timeout"

    def test_classify_dom_error(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        assert bl._classify_error(Exception("selector not found")) == "dom_change"

    def test_classify_unknown_error(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        assert bl._classify_error(Exception("weird error")) == "unknown"

    def test_generate_fallback_selectors(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        fallbacks = bl._generate_fallback_selectors("Submit")
        assert len(fallbacks) > 3
        assert any("Submit" in f for f in fallbacks)

    def test_success_rate_empty(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        rates = bl.get_success_rate()
        assert rates == {}

    def test_success_rate_with_data(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        bl._log_action("test.com", "navigate", None, "https://test.com", True)
        bl._log_action("test.com", "navigate", None, "https://test.com", True)
        bl._log_action("test.com", "navigate", None, "https://test.com", False,
                        failure_type="timeout")
        rates = bl.get_success_rate()
        assert "navigate" in rates
        assert rates["navigate"]["total"] == 3
        assert rates["navigate"]["successes"] == 2

    def test_success_rate_by_domain(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        bl._log_action("a.com", "navigate", None, "https://a.com", True)
        bl._log_action("b.com", "navigate", None, "https://b.com", False, "timeout")

        rates_a = bl.get_success_rate("a.com")
        assert rates_a["navigate"]["successes"] == 1

        rates_b = bl.get_success_rate("b.com")
        assert rates_b["navigate"]["successes"] == 0

    def test_failure_breakdown(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        bl._log_action("test.com", "navigate", None, "url", False, "captcha")
        bl._log_action("test.com", "navigate", None, "url", False, "captcha")
        bl._log_action("test.com", "click", "#btn", None, False, "dom_change")

        breakdown = bl.get_failure_breakdown()
        assert breakdown["captcha"] == 2
        assert breakdown["dom_change"] == 1

    def test_playbook_empty(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        assert bl.get_playbook("unknown.com") is None

    def test_update_playbook_selector(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        bl._update_playbook_selector("test.com", "#old-btn", "button:has-text('Submit')")

        playbook = bl.get_playbook("test.com")
        assert playbook is not None
        selectors = json.loads(playbook["known_selectors"])
        assert selectors["#old-btn"] == "button:has-text('Submit')"

    def test_pre_resolve_uses_playbook(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        bl._update_playbook_selector("test.com", "#email", "#real-email")
        resolved = bl._pre_resolve_selectors(
            {"#email": "a@b.com", "#password": "x"}, domain="test.com"
        )
        assert resolved["#email"] == "#real-email"
        assert resolved["#password"] == "#password"  # no playbook entry

    def test_pre_resolve_no_domain(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        resolved = bl._pre_resolve_selectors(
            {"#email": "a@b.com"}, domain=""
        )
        assert resolved["#email"] == "#email"

    def test_llm_batch_match_parses_json(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        mock_llm.quick.return_value = '{"#email": "#real-email", "#name": null}'
        elements = [{"tag": "input", "id": "real-email"}]
        result = bl._llm_batch_match_selectors(["#email", "#name"], elements)
        assert result["#email"] == "#real-email"
        assert result["#name"] is None

    def test_llm_batch_match_handles_markdown_fences(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        mock_llm.quick.return_value = '```json\n{"#email": "#real-email"}\n```'
        result = bl._llm_batch_match_selectors(["#email"], [])
        assert result["#email"] == "#real-email"

    def test_llm_batch_match_handles_error(self, config, db, mock_llm):
        bl = BrowserLearner(config, db, mock_llm)
        mock_llm.quick.side_effect = Exception("LLM down")
        result = bl._llm_batch_match_selectors(["#email"], [])
        assert result == {}

    def test_pre_resolve_missing_field_returns_none(self, config, db, mock_llm):
        """Fields cached as __MISSING__ should resolve to None."""
        bl = BrowserLearner(config, db, mock_llm)
        bl._update_playbook_selector("stripe.com", ".SearchableSelect", "__MISSING__")
        resolved = bl._pre_resolve_selectors(
            {".SearchableSelect": "US"}, domain="stripe.com"
        )
        assert resolved[".SearchableSelect"] is None

    def test_pre_resolve_codegen_field_returns_none(self, config, db, mock_llm):
        """Fields cached as __CODEGEN__ should resolve to None (routed to codegen)."""
        bl = BrowserLearner(config, db, mock_llm)
        bl._update_playbook_selector("stripe.com", ".SearchableSelect", "__CODEGEN__")
        resolved = bl._pre_resolve_selectors(
            {".SearchableSelect": "US"}, domain="stripe.com"
        )
        assert resolved[".SearchableSelect"] is None

    @pytest.mark.asyncio
    async def test_smart_fill_form_includes_skipped_in_codegen(self, config, db, mock_llm):
        """Skipped (complex UI) fields should be attempted by codegen fallback."""
        bl = BrowserLearner(config, db, mock_llm)
        mock_browser = AsyncMock()
        bl.browser = mock_browser

        # Pre-cache a field as __MISSING__ so it gets skipped by standard fill
        bl._update_playbook_selector("stripe.com", ".country-select", "__MISSING__")

        # Mock _codegen_fill_form to track what fields it receives
        codegen_fields_received = {}

        async def capture_codegen(fields, domain):
            codegen_fields_received.update(fields)
            return {"success": True}

        bl._codegen_fill_form = capture_codegen

        # smart_type for the email field — succeeds
        async def mock_smart_type(selector, value, domain, **kw):
            return {"success": True}

        bl.smart_type = mock_smart_type
        bl._reveal_if_hidden = AsyncMock(return_value={})
        bl._human_delay = AsyncMock()
        bl._discover_form_elements = AsyncMock(return_value=[])

        result = await bl.smart_fill_form(
            {"#email": "test@test.com", ".country-select": "US"},
            domain="stripe.com",
        )

        # The country-select field should have been passed to codegen
        assert ".country-select" in codegen_fields_received
        assert codegen_fields_received[".country-select"] == "US"

    @pytest.mark.asyncio
    async def test_codegen_success_clears_missing_cache(self, config, db, mock_llm):
        """When codegen fills a __MISSING__ field, cache should update to __CODEGEN__."""
        bl = BrowserLearner(config, db, mock_llm)
        mock_browser = AsyncMock()
        bl.browser = mock_browser

        bl._update_playbook_selector("stripe.com", ".country-select", "__MISSING__")

        async def mock_codegen(fields, domain):
            return {"success": True}

        bl._codegen_fill_form = mock_codegen
        bl.smart_type = AsyncMock(return_value={"success": True})
        bl._reveal_if_hidden = AsyncMock(return_value={})
        bl._human_delay = AsyncMock()
        bl._discover_form_elements = AsyncMock(return_value=[])

        await bl.smart_fill_form(
            {"#email": "test@test.com", ".country-select": "US"},
            domain="stripe.com",
        )

        # The __MISSING__ cache should now be __CODEGEN__
        known = bl._get_known_selector("stripe.com", ".country-select")
        assert known == "__CODEGEN__"
