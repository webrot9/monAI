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
