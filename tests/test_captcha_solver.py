"""Tests for the CAPTCHA Solver."""

from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

import pytest

from monai.config import Config, CaptchaConfig
from monai.db.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def config():
    cfg = Config()
    cfg.captcha = CaptchaConfig(
        provider="twocaptcha",
        twocaptcha_api_key="test-2captcha-key",
        anticaptcha_api_key="test-anticaptcha-key",
    )
    return cfg


@pytest.fixture
def solver(config, db):
    with patch("monai.agents.captcha_solver.get_anonymizer") as mock_anon:
        mock_anon.return_value.create_http_client.return_value = MagicMock()
        from monai.agents.captcha_solver import CaptchaSolver
        return CaptchaSolver(config, db)


class TestCaptchaSolverSchema:
    def test_creates_table(self, solver, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='captcha_solves'"
        )
        assert len(rows) == 1

    def test_record_stores_solve(self, solver, db):
        solver._record("twocaptcha", "recaptcha_v2", "example.com", True, 0.003, 5000, "abc123")
        rows = db.execute("SELECT * FROM captcha_solves")
        assert len(rows) == 1
        assert rows[0]["provider"] == "twocaptcha"
        assert rows[0]["captcha_type"] == "recaptcha_v2"
        assert rows[0]["domain"] == "example.com"
        assert rows[0]["success"] == 1
        assert rows[0]["cost_usd"] == 0.003
        assert rows[0]["task_id"] == "abc123"

    def test_record_failure(self, solver, db):
        solver._record("anticaptcha", "hcaptcha", "blocked.com", False, 0, 2000)
        rows = db.execute("SELECT * FROM captcha_solves WHERE success = 0")
        assert len(rows) == 1
        assert rows[0]["success"] == 0


class TestCaptchaSolverConfig:
    def test_provider_from_config(self, solver):
        assert solver._provider == "twocaptcha"

    def test_api_key_twocaptcha(self, solver):
        assert solver._get_api_key("twocaptcha") == "test-2captcha-key"

    def test_api_key_anticaptcha(self, solver):
        assert solver._get_api_key("anticaptcha") == "test-anticaptcha-key"

    def test_api_key_fallback(self, config, db):
        config.captcha = CaptchaConfig(
            provider="twocaptcha",
            twocaptcha_api_key="",
            api_key="shared-key",
        )
        with patch("monai.agents.captcha_solver.get_anonymizer") as mock_anon:
            mock_anon.return_value.create_http_client.return_value = MagicMock()
            from monai.agents.captcha_solver import CaptchaSolver
            s = CaptchaSolver(config, db)
        assert s._get_api_key("twocaptcha") == "shared-key"

    def test_no_api_key_returns_empty(self, config, db):
        config.captcha = CaptchaConfig(provider="twocaptcha")
        with patch("monai.agents.captcha_solver.get_anonymizer") as mock_anon:
            mock_anon.return_value.create_http_client.return_value = MagicMock()
            from monai.agents.captcha_solver import CaptchaSolver
            s = CaptchaSolver(config, db)
        assert s._get_api_key("twocaptcha") == ""


class TestCaptchaSolverStats:
    def test_stats_empty(self, solver):
        assert solver.get_stats() == {}
        assert solver.get_total_cost() == 0

    def test_stats_aggregation(self, solver, db):
        solver._record("twocaptcha", "recaptcha_v2", "a.com", True, 0.003, 5000)
        solver._record("twocaptcha", "recaptcha_v2", "b.com", True, 0.003, 4000)
        solver._record("twocaptcha", "recaptcha_v2", "c.com", False, 0, 3000)
        solver._record("twocaptcha", "hcaptcha", "d.com", True, 0.003, 6000)

        stats = solver.get_stats()
        assert "recaptcha_v2" in stats
        assert stats["recaptcha_v2"]["total"] == 3
        assert stats["recaptcha_v2"]["solved"] == 2
        assert "hcaptcha" in stats
        assert stats["hcaptcha"]["total"] == 1

    def test_total_cost(self, solver):
        solver._record("twocaptcha", "recaptcha_v2", "a.com", True, 0.003, 5000)
        solver._record("twocaptcha", "hcaptcha", "b.com", True, 0.003, 4000)
        assert solver.get_total_cost() == pytest.approx(0.006)


class TestCaptchaSolverSolve:
    @pytest.mark.asyncio
    async def test_solve_no_api_key_returns_error(self, config, db):
        config.captcha = CaptchaConfig(provider="twocaptcha")
        with patch("monai.agents.captcha_solver.get_anonymizer") as mock_anon:
            mock_anon.return_value.create_http_client.return_value = MagicMock()
            from monai.agents.captcha_solver import CaptchaSolver
            s = CaptchaSolver(config, db)
        result = await s.solve("recaptcha_v2", "https://example.com", sitekey="abc")
        assert result["status"] == "error"
        assert "No API key" in result["error"]

    @pytest.mark.asyncio
    async def test_solve_unknown_provider(self, config, db):
        config.captcha = CaptchaConfig(provider="unknown", api_key="key")
        with patch("monai.agents.captcha_solver.get_anonymizer") as mock_anon:
            mock_anon.return_value.create_http_client.return_value = MagicMock()
            from monai.agents.captcha_solver import CaptchaSolver
            s = CaptchaSolver(config, db)
        result = await s.solve("recaptcha_v2", "https://example.com", sitekey="abc")
        assert result["status"] == "error"
        assert "Unknown provider" in result["error"]

    @pytest.mark.asyncio
    async def test_solve_twocaptcha_success(self, solver):
        """Test full 2captcha flow: submit → poll → solved."""
        mock_http = MagicMock()
        # Submit response
        submit_resp = MagicMock()
        submit_resp.json.return_value = {"status": 1, "request": "TASK123"}
        submit_resp.raise_for_status = MagicMock()
        # Poll response (solved immediately)
        poll_resp = MagicMock()
        poll_resp.json.return_value = {"status": 1, "request": "SOLVED_TOKEN_ABC"}
        poll_resp.raise_for_status = MagicMock()

        mock_http.post.return_value = submit_resp
        mock_http.get.return_value = poll_resp
        solver._CaptchaSolver__http = mock_http

        result = await solver.solve("recaptcha_v2", "https://test.com",
                                    sitekey="sitekey123", domain="test.com")
        assert result["status"] == "solved"
        assert result["token"] == "SOLVED_TOKEN_ABC"
        assert result["cost_usd"] == 0.003
        assert result["solve_time_ms"] >= 0

        # Verify it was recorded in DB
        rows = solver.db.execute("SELECT * FROM captcha_solves WHERE success = 1")
        assert len(rows) == 1
        assert rows[0]["task_id"] == "TASK123"

    @pytest.mark.asyncio
    async def test_solve_twocaptcha_submit_error(self, solver):
        """2captcha returns error on submit."""
        mock_http = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {"status": 0, "request": "ERROR_WRONG_USER_KEY"}
        resp.raise_for_status = MagicMock()
        mock_http.post.return_value = resp
        solver._CaptchaSolver__http = mock_http

        result = await solver.solve("recaptcha_v2", "https://test.com",
                                    sitekey="bad", domain="test.com")
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_solve_anticaptcha_success(self, config, db):
        """Test anti-captcha flow."""
        config.captcha = CaptchaConfig(
            provider="anticaptcha",
            anticaptcha_api_key="ac-key",
        )
        mock_http = MagicMock()
        # Create task response
        create_resp = MagicMock()
        create_resp.json.return_value = {"errorId": 0, "taskId": 99}
        create_resp.raise_for_status = MagicMock()
        # Get result response
        result_resp = MagicMock()
        result_resp.json.return_value = {
            "errorId": 0, "status": "ready",
            "solution": {"gRecaptchaResponse": "TOKEN_FROM_AC"},
        }
        result_resp.raise_for_status = MagicMock()
        mock_http.post.side_effect = [create_resp, result_resp]

        with patch("monai.agents.captcha_solver.get_anonymizer") as mock_anon:
            mock_anon.return_value.create_http_client.return_value = mock_http
            from monai.agents.captcha_solver import CaptchaSolver
            s = CaptchaSolver(config, db)
            s._CaptchaSolver__http = mock_http

        result = await s.solve("hcaptcha", "https://test.com",
                               sitekey="hc-key", domain="test.com")
        assert result["status"] == "solved"
        assert result["token"] == "TOKEN_FROM_AC"

    @pytest.mark.asyncio
    async def test_solve_failover_on_exception(self, solver):
        """When primary provider throws, falls back to secondary."""
        mock_http = MagicMock()

        call_count = 0

        def side_effect_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call (2captcha submit) — fails
                raise ConnectionError("2captcha down")
            # Second call (anticaptcha createTask)
            resp = MagicMock()
            resp.json.return_value = {"errorId": 0, "taskId": 42}
            resp.raise_for_status = MagicMock()
            if call_count == 3:
                resp.json.return_value = {
                    "errorId": 0, "status": "ready",
                    "solution": {"token": "FALLBACK_TOKEN"},
                }
            return resp

        mock_http.post.side_effect = side_effect_post
        solver._CaptchaSolver__http = mock_http

        result = await solver.solve("turnstile", "https://cf.com",
                                    sitekey="ts-key", domain="cf.com")
        assert result["status"] == "solved"
        assert result["token"] == "FALLBACK_TOKEN"


class TestCaptchaSolverDetection:
    @pytest.mark.asyncio
    async def test_detect_recaptcha_v2(self, solver):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value={
            "type": "recaptcha_v2", "sitekey": "6Le..."
        })
        result = await solver._detect_captcha_type(page)
        assert result["type"] == "recaptcha_v2"
        assert result["sitekey"] == "6Le..."

    @pytest.mark.asyncio
    async def test_detect_none(self, solver):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=None)
        result = await solver._detect_captcha_type(page)
        assert result is None

    @pytest.mark.asyncio
    async def test_solve_from_page_no_captcha(self, solver):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=None)
        result = await solver.solve_from_page(page, "https://clean.com")
        assert result["status"] == "error"
        assert "No CAPTCHA detected" in result["error"]
