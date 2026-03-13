"""Tests for executor circuit breaker and browser fill_form selector normalization."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.agents.executor import AutonomousExecutor
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

        def fake_think(task, context, step):
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
    async def test_circuit_breaker_resets_on_success(self):
        executor = self._make_executor(max_steps=15)

        call_count = {"n": 0}

        def fake_think(task, context, step):
            call_count["n"] += 1
            if call_count["n"] == 10:
                return {"tool": "done", "args": {"result": "finished"}}
            return {"tool": "browse", "args": {"url": "https://example.com"}}

        executor._think = fake_think
        executor.browser = AsyncMock()
        executor.browser.start = AsyncMock()
        executor.browser.stop = AsyncMock()

        # Alternate: 4 failures, 1 success, 4 failures, 1 success, etc.
        fail_count = {"n": 0}

        async def navigate_side_effect(url):
            fail_count["n"] += 1
            if fail_count["n"] % 5 == 0:
                return "ok"
            raise Exception("Timeout 30000ms exceeded")

        executor.browser.navigate = navigate_side_effect
        executor.browser.get_page_info = AsyncMock(return_value={"url": "ok", "text": "ok"})
        executor._log_task = MagicMock()

        result = await executor.execute_task("test task")
        # Should eventually complete because failures reset before hitting 5
        assert result["status"] == "completed"

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

        def fake_think(task, context, step):
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
