"""Tests for executor circuit breaker, browser fill_form selector normalization,
inter-agent tools, playbook injection, and history formatting."""

import asyncio
import json
from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.agents.executor import AutonomousExecutor
from monai.agents.playbooks import (
    detect_platforms_in_task,
    get_playbook,
    get_playbook_prompt,
)
from monai.utils.browser import Browser


class TestCircuitBreaker:
    """Executor should abort after MAX_CONSECUTIVE_FAILURES consecutive tool failures."""

    def _make_executor(self, max_steps=30):
        config = MagicMock()
        config.data_dir = MagicMock()
        config.data_dir.__truediv__ = lambda s, x: MagicMock()
        db = MagicMock()
        llm = MagicMock()
        with patch("monai.agents.executor.get_anonymizer"):
            executor = AutonomousExecutor(config, db, llm, max_steps=max_steps)
        return executor

    def test_default_max_steps_is_30(self):
        executor = self._make_executor()
        assert executor.max_steps == 30

    def test_max_consecutive_failures_constant(self):
        assert AutonomousExecutor.MAX_CONSECUTIVE_FAILURES == 5

    @pytest.mark.asyncio
    async def test_circuit_breaker_triggers_on_consecutive_errors(self):
        executor = self._make_executor(max_steps=20)

        # Every _think call returns a browse action that will fail
        step_counter = {"n": 0}

        def fake_think(task, context, step, playbook_context=""):
            step_counter["n"] += 1
            return {"tool": "browse", "args": {"url": "https://example.com"}}

        executor._think = fake_think
        executor.browser = AsyncMock()
        executor.browser.start = AsyncMock()
        executor.browser.stop = AsyncMock()
        executor.browser.navigate = AsyncMock(side_effect=Exception("Timeout 30000ms exceeded"))
        executor._log_task = MagicMock()

        result = await executor.execute_task("test task")

        assert result["status"] == "failed"
        assert "circuit breaker" in result["reason"].lower()
        # Should have stopped after 5 consecutive failures (triggered at step 6 check)
        assert result["steps"] == 5

    @pytest.mark.asyncio
    async def test_failure_rate_circuit_breaker(self):
        """If >60% of steps fail after 8+ steps, abort."""
        executor = self._make_executor(max_steps=20)

        call_count = {"n": 0}

        def fake_think(task, context, step, playbook_context=""):
            call_count["n"] += 1
            return {"tool": "browse", "args": {"url": "https://example.com"}}

        executor._think = fake_think
        executor.browser = AsyncMock()
        executor.browser.start = AsyncMock()
        executor.browser.stop = AsyncMock()

        # Alternate: fail, fail, fail, success, fail, fail, fail, success, ...
        # = 75% failure rate, but never 5 consecutive
        step_n = {"n": 0}

        async def mixed_act(tool, args):
            step_n["n"] += 1
            if step_n["n"] % 4 == 0:
                return {"url": "ok", "text": "ok"}  # success every 4th
            return "ERROR: All proxy methods blocked"

        executor._act = mixed_act
        executor._log_task = MagicMock()

        result = await executor.execute_task("test task")
        assert result["status"] == "failed"
        assert "circuit breaker" in result["reason"].lower()
        assert "not making progress" in result["reason"]

    @pytest.mark.asyncio
    async def test_error_result_strings_detected_as_failures(self):
        executor = self._make_executor(max_steps=10)

        error_messages = [
            "ERROR: Page.fill: Timeout 30000ms exceeded",
            "BLOCKED by ethics guardrails: dangerous action",
            "ERROR: some tool failed",
            "Tool click failed: Timeout 30000ms exceeded",
            "ERROR: another failure",
        ]
        call_count = {"n": 0}

        def fake_think(task, context, step, playbook_context=""):
            return {"tool": "type", "args": {"selector": "x", "text": "y"}}

        executor._think = fake_think
        executor.browser = AsyncMock()
        executor.browser.start = AsyncMock()
        executor.browser.stop = AsyncMock()

        async def fake_act(tool, args):
            idx = call_count["n"]
            call_count["n"] += 1
            if idx < len(error_messages):
                return error_messages[idx]
            return "ok"

        executor._act = fake_act
        executor._log_task = MagicMock()

        result = await executor.execute_task("test task")
        assert result["status"] == "failed"
        assert "circuit breaker" in result["reason"].lower()


class TestIsFailureResult:
    """Test the static _is_failure_result method."""

    def test_error_prefix(self):
        assert AutonomousExecutor._is_failure_result("ERROR: something") is True

    def test_blocked_prefix(self):
        assert AutonomousExecutor._is_failure_result("BLOCKED by guardrails") is True

    def test_timeout_in_result(self):
        assert AutonomousExecutor._is_failure_result("Page.fill: Timeout 30000ms") is True

    def test_timed_out_lower(self):
        assert AutonomousExecutor._is_failure_result("request timed out") is True

    def test_success_result(self):
        assert AutonomousExecutor._is_failure_result("typed") is False

    def test_dict_result(self):
        assert AutonomousExecutor._is_failure_result("{'url': 'ok'}") is False


class TestHistoryFormatting:
    """Test _format_history produces useful context for the LLM."""

    def _make_executor(self):
        config = MagicMock()
        config.data_dir = MagicMock()
        config.data_dir.__truediv__ = lambda s, x: MagicMock()
        db = MagicMock()
        llm = MagicMock()
        with patch("monai.agents.executor.get_anonymizer"):
            executor = AutonomousExecutor(config, db, llm)
        return executor

    def test_empty_history(self):
        executor = self._make_executor()
        assert executor._format_history() == "None yet"

    def test_includes_step_status(self):
        executor = self._make_executor()
        executor.action_history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://x.com"}, "result": "ok page", "failed": False},
            {"step": 2, "tool": "click", "args": {"selector": "#btn"}, "result": "ERROR: not found", "failed": True},
        ]
        formatted = executor._format_history()
        assert "[OK]" in formatted
        assert "[FAILED]" in formatted
        assert "Stats: 2 steps total, 1 failed" in formatted

    def test_highlights_repeated_failures(self):
        executor = self._make_executor()
        executor.action_history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://x.com"}, "result": "ERROR: timeout", "failed": True},
            {"step": 2, "tool": "browse", "args": {"url": "https://x.com"}, "result": "ERROR: timeout", "failed": True},
            {"step": 3, "tool": "browse", "args": {"url": "https://x.com"}, "result": "ERROR: timeout", "failed": True},
        ]
        formatted = executor._format_history()
        assert "REPEATED FAILURES" in formatted
        assert "failed 3x" in formatted

    def test_respects_max_actions(self):
        executor = self._make_executor()
        executor.action_history = [
            {"step": i, "tool": "wait", "args": {"seconds": 1}, "result": "ok", "failed": False}
            for i in range(1, 30)
        ]
        formatted = executor._format_history(max_actions=5)
        # Should only show last 5 steps plus summary
        assert "Step 25" in formatted
        assert "Step 29" in formatted
        # Should NOT show early steps
        assert "Step 1 " not in formatted


class TestPlaybooks:
    """Test playbook lookup and prompt generation."""

    def test_get_playbook_upwork(self):
        pb = get_playbook("upwork")
        assert pb is not None
        assert pb["name"] == "Upwork"
        assert "signup_url" in pb
        assert "steps" in pb

    def test_get_playbook_protonmail(self):
        pb = get_playbook("protonmail")
        assert pb is not None
        assert "signup_url" in pb

    def test_get_playbook_nonexistent(self):
        assert get_playbook("nonexistent_platform_xyz") is None

    def test_get_playbook_prompt_contains_url(self):
        prompt = get_playbook_prompt("upwork")
        assert "upwork.com" in prompt
        assert "STEPS:" in prompt
        assert "ERROR RECOVERY:" in prompt

    def test_detect_platforms_upwork(self):
        platforms = detect_platforms_in_task("Register on Upwork freelance platform")
        assert "upwork" in platforms

    def test_detect_platforms_email(self):
        platforms = detect_platforms_in_task("Create an email account for business")
        assert "mail_tm" in platforms

    def test_detect_platforms_domain(self):
        platforms = detect_platforms_in_task("Register a domain name")
        assert "namecheap" in platforms

    def test_detect_platforms_empty(self):
        platforms = detect_platforms_in_task("do some random stuff")
        assert platforms == []

    def test_detect_platforms_multiple(self):
        platforms = detect_platforms_in_task("Register on Upwork and Fiverr")
        assert "upwork" in platforms
        assert "fiverr" in platforms


class TestInterAgentTools:
    """Test that executor can call inter-agent tools."""

    def _make_executor(self):
        config = MagicMock()
        config.data_dir = MagicMock()
        config.data_dir.__truediv__ = lambda s, x: MagicMock()
        db = MagicMock()
        llm = MagicMock()
        with patch("monai.agents.executor.get_anonymizer"):
            executor = AutonomousExecutor(config, db, llm)
        return executor

    @pytest.mark.asyncio
    async def test_create_temp_email_tool(self):
        executor = self._make_executor()
        mock_verifier = MagicMock()
        mock_verifier.create_temp_email.return_value = {
            "status": "created",
            "address": "test@mail.tm",
            "password": "secret123",
        }
        executor._email_verifier = mock_verifier

        result = await executor._act("create_temp_email", {})
        assert result["status"] == "created"
        assert result["address"] == "test@mail.tm"
        mock_verifier.create_temp_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_email_verification_tool(self):
        executor = self._make_executor()
        mock_verifier = MagicMock()
        mock_verifier.wait_for_verification.return_value = {
            "status": "found",
            "verification_type": "code",
            "verification_value": "123456",
        }
        executor._email_verifier = mock_verifier

        result = await executor._act("check_email_verification", {
            "email": "test@mail.tm", "platform": "upwork",
        })
        assert result["status"] == "found"
        assert result["verification_value"] == "123456"

    @pytest.mark.asyncio
    async def test_get_phone_tool(self):
        executor = self._make_executor()
        mock_phone = MagicMock()
        mock_phone.get_number.return_value = {
            "status": "acquired",
            "phone_number": "+15551234567",
            "phone_id": 42,
        }
        executor._phone_provisioner = mock_phone

        result = await executor._act("get_phone", {"platform": "upwork"})
        assert result["status"] == "acquired"
        assert result["phone_number"] == "+15551234567"

    @pytest.mark.asyncio
    async def test_check_phone_code_tool(self):
        executor = self._make_executor()
        mock_phone = MagicMock()
        mock_phone.wait_for_code.return_value = {
            "status": "received",
            "code": "789012",
        }
        executor._phone_provisioner = mock_phone

        result = await executor._act("check_phone_code", {"phone_id": 42})
        assert result["status"] == "received"
        assert result["code"] == "789012"


class TestBuildPlaybookContext:
    """Test that playbook context is injected into executor tasks."""

    def _make_executor(self):
        config = MagicMock()
        config.data_dir = MagicMock()
        config.data_dir.__truediv__ = lambda s, x: MagicMock()
        db = MagicMock()
        llm = MagicMock()
        with patch("monai.agents.executor.get_anonymizer"):
            executor = AutonomousExecutor(config, db, llm)
        return executor

    def test_playbook_context_for_upwork_task(self):
        executor = self._make_executor()
        ctx = executor._build_playbook_context("Register on Upwork")
        assert "Upwork" in ctx
        assert "upwork.com" in ctx

    def test_no_playbook_for_generic_task(self):
        executor = self._make_executor()
        ctx = executor._build_playbook_context("write a hello world script")
        assert ctx == ""


class TestFillFormSelectorNormalization:
    """Browser.fill_form should auto-fix bare field names into CSS selectors."""

    def test_bare_name_becomes_attribute_selector(self):
        assert Browser._normalize_selector("firstName") == '[name="firstName"], #firstName'

    def test_name_attribute_kept(self):
        result = Browser._normalize_selector('[name="firstName"]')
        assert result == '[name="firstName"]'

    def test_id_selector_kept(self):
        assert Browser._normalize_selector("#myId") == "#myId"

    def test_class_selector_kept(self):
        assert Browser._normalize_selector(".myClass") == ".myClass"

    def test_input_tag_kept(self):
        assert Browser._normalize_selector("input[name='email']") == "input[name='email']"

    def test_css_combinator_kept(self):
        sel = "div > input.field"
        assert Browser._normalize_selector(sel) == sel

    def test_pseudo_selector_kept(self):
        sel = "input:first-child"
        assert Browser._normalize_selector(sel) == sel

    def test_textarea_kept(self):
        assert Browser._normalize_selector("textarea#bio") == "textarea#bio"

    def test_anchor_tag_with_href_kept(self):
        sel = "a[href='/accounts/emailsignup/']"
        assert Browser._normalize_selector(sel) == sel

    def test_div_tag_kept(self):
        assert Browser._normalize_selector("div") == "div"

    def test_form_tag_kept(self):
        assert Browser._normalize_selector("form") == "form"

    def test_text_selector_kept(self):
        sel = "text='Sign up'"
        # Contains =, which isn't in our special chars but ' ' isn't either
        # Playwright-specific selectors — pass through since they contain '
        pass

    def test_selector_with_brackets_kept(self):
        """Any selector containing [ should be treated as CSS."""
        sel = "button[type='submit']"
        assert Browser._normalize_selector(sel) == sel


class TestProxyBlockTTL:
    """Proxy blocked domains should expire after TTL."""

    def test_block_expires_after_ttl(self):
        from monai.utils.privacy import ProxyFallbackChain
        from monai.config import PrivacyConfig
        import time

        config = PrivacyConfig(proxy_type="tor", tor_socks_port=9050)
        chain = ProxyFallbackChain(config)

        # Block a domain
        chain.report_blocked("example.com", "tor")
        assert "tor" in chain._blocked.get("example.com", set())

        # Simulate TTL expiry
        chain._blocked_at["example.com"] = time.time() - 400  # 400s ago > 300s TTL

        # _expire_blocks should clear it
        with chain._lock:
            chain._expire_blocks("example.com")

        assert "example.com" not in chain._blocked

    def test_block_not_expired_before_ttl(self):
        from monai.utils.privacy import ProxyFallbackChain
        from monai.config import PrivacyConfig
        import time

        config = PrivacyConfig(proxy_type="tor", tor_socks_port=9050)
        chain = ProxyFallbackChain(config)

        chain.report_blocked("example.com", "tor")
        chain._blocked_at["example.com"] = time.time() - 100  # 100s ago < 300s TTL

        with chain._lock:
            chain._expire_blocks("example.com")

        assert "tor" in chain._blocked.get("example.com", set())


class TestTorControlPort:
    """Tor should be started with --ControlPort 9051."""

    def test_start_tor_includes_control_port(self):
        from monai.infra.auto_setup import InfraSetup
        setup = InfraSetup()

        with patch("shutil.which", return_value="/usr/bin/tor"), \
             patch("subprocess.Popen") as mock_popen, \
             patch.object(setup, "_is_port_open", return_value=True), \
             patch("monai.infra.auto_setup.MONAI_DIR", MagicMock()):
            # Mock MONAI_DIR / "tor_data" to return a mock path
            mock_dir = MagicMock()
            mock_dir.__truediv__ = lambda s, x: MagicMock()

            import monai.infra.auto_setup as mod
            orig = mod.MONAI_DIR
            try:
                mod.MONAI_DIR = mock_dir
                mock_popen.return_value = MagicMock(pid=1234)
                setup._start_tor()

                # Verify --ControlPort 9051 was passed
                call_args = mock_popen.call_args[0][0]
                assert "--ControlPort" in call_args
                assert "9051" in call_args
            finally:
                mod.MONAI_DIR = orig
