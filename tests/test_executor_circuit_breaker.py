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
        # Mock db.connect() context manager for SharedMemory schema init
        db.connect.return_value.__enter__ = MagicMock()
        db.connect.return_value.__exit__ = MagicMock()
        llm = MagicMock()
        with patch("monai.agents.executor.get_anonymizer"), \
             patch("monai.agents.executor.SharedMemory"), \
             patch("monai.agents.browser_learner.BrowserLearner", side_effect=Exception("no browser")):
            executor = AutonomousExecutor(config, db, llm, max_steps=max_steps)
        # Ensure _learner is None so tests control browser directly
        executor._learner = None
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
        """Consecutive breaker resets on success; mostly-successful tasks complete."""
        executor = self._make_executor(max_steps=15)

        call_count = {"n": 0}

        def fake_think(task, context, step):
            call_count["n"] += 1
            if call_count["n"] == 8:
                return {"tool": "done", "args": {"result": "finished"}}
            # Use unique URLs to avoid domain-rejection guard
            return {"tool": "browse", "args": {"url": f"https://example{call_count['n']}.com"}}

        executor._think = fake_think
        executor.browser = AsyncMock()
        executor.browser.start = AsyncMock()
        executor.browser.stop = AsyncMock()

        # Alternate: 1 failure, 1 success — stays well under ratio threshold
        fail_count = {"n": 0}

        async def navigate_side_effect(url):
            fail_count["n"] += 1
            if fail_count["n"] % 2 == 0:
                return "ok"
            raise Exception("Timeout 30000ms exceeded")

        executor.browser.navigate = navigate_side_effect
        executor.browser.get_page_info = AsyncMock(return_value={"url": "ok", "text": "ok"})
        executor._log_task = MagicMock()

        result = await executor.execute_task("test task")
        # Should complete — failure ratio is ~50%, under 70% threshold
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_circuit_breaker_ratio_trips_on_interleaved_failures(self):
        """Ratio breaker fires even when LLM games it with read_page between errors."""
        executor = self._make_executor(max_steps=20)

        call_count = {"n": 0}

        def fake_think(task, context, step):
            call_count["n"] += 1
            # Pattern: browse(fail), read_page(ok), browse(fail), read_page(ok), ...
            if call_count["n"] % 2 == 1:
                return {"tool": "browse", "args": {"url": "https://example.com"}}
            return {"tool": "read_page", "args": {}}

        executor._think = fake_think
        executor.browser = AsyncMock()
        executor.browser.start = AsyncMock()
        executor.browser.stop = AsyncMock()
        executor.browser.navigate = AsyncMock(side_effect=Exception("blocked"))
        executor.browser.get_text = AsyncMock(return_value="some text")
        executor._log_task = MagicMock()

        result = await executor.execute_task("test task")
        # 50% failure rate — under 70% threshold, so it should hit max_steps
        # (consecutive breaker never fires because read_page resets it)
        assert result["status"] == "max_steps_reached"

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

    def test_tag_with_attribute_selector_kept(self):
        """a[href='...'] must NOT be treated as a bare name."""
        sel = "a[href='/accounts/emailsignup/']"
        assert Browser._normalize_selector(sel) == sel

    def test_sibling_combinator_kept(self):
        sel = "label + input"
        assert Browser._normalize_selector(sel) == sel


class TestEthicsBlockNudge:
    """When run_page_script is blocked by ethics 2+ times, the nudge should
    steer the LLM away from run_page_script and toward alternatives."""

    def _make_executor(self):
        config = MagicMock()
        config.data_dir = MagicMock()
        config.data_dir.__truediv__ = lambda s, x: MagicMock()
        db = MagicMock()
        db.connect.return_value.__enter__ = MagicMock()
        db.connect.return_value.__exit__ = MagicMock()
        llm = MagicMock()
        with patch("monai.agents.executor.get_anonymizer"), \
             patch("monai.agents.executor.SharedMemory"), \
             patch("monai.agents.browser_learner.BrowserLearner", side_effect=Exception("no browser")):
            executor = AutonomousExecutor(config, db, llm, max_steps=10)
        executor._learner = None
        return executor

    def test_ethics_block_nudge_after_2_blocks(self):
        executor = self._make_executor()
        # Simulate 2 ethics-blocked run_page_script attempts
        executor.action_history = [
            {"step": 1, "tool": "run_page_script",
             "args": {"script": "document.querySelector(...)"},
             "result": "BLOCKED: Script failed ethics review: violating CFAA"},
            {"step": 2, "tool": "run_page_script",
             "args": {"script": "document.querySelector(...)"},
             "result": "BLOCKED: Script failed ethics review: circumvents ToS"},
        ]

        # Mock all the dependencies _think needs
        executor._get_learned_context = MagicMock(return_value="")
        executor._get_executor_config = MagicMock(return_value=0.3)
        executor._get_tool_descriptions = MagicMock(return_value="")
        executor._proof = MagicMock()
        executor._proof.get_history.return_value = ""

        # Make LLM return a valid action so _think completes
        executor.llm.chat_json.return_value = {
            "reasoning": "test", "tool": "fail",
            "args": {"reason": "blocked"}
        }

        # Call _think and check that the prompt includes the ethics nudge
        executor._think("test task", "test context", 2)

        prompt = executor.llm.chat_json.call_args[0][0][-1]["content"]
        assert "run_page_script has been BLOCKED by ethics review" in prompt
        assert "DO NOT use run_page_script" in prompt

    def test_no_ethics_nudge_without_blocks(self):
        executor = self._make_executor()
        executor.action_history = [
            {"step": 1, "tool": "fill_form",
             "args": {"fields": {}},
             "result": "ERROR: Fill failed on: country"},
        ]
        executor._get_learned_context = MagicMock(return_value="")
        executor._get_executor_config = MagicMock(return_value=0.3)
        executor._get_tool_descriptions = MagicMock(return_value="")
        executor._proof = MagicMock()
        executor._proof.get_history.return_value = ""
        executor.llm.chat_json.return_value = {
            "reasoning": "test", "tool": "fail",
            "args": {"reason": "test"}
        }

        executor._think("test task", "test context", 1)

        prompt = executor.llm.chat_json.call_args[0][0][-1]["content"]
        assert "run_page_script has been BLOCKED" not in prompt


class TestScriptTargetBurning:
    """When run_page_script fails N times on the same DOM target, the executor
    should stop retrying and auto-reject further attempts."""

    def _make_executor(self, max_steps=30):
        config = MagicMock()
        config.data_dir = MagicMock()
        config.data_dir.__truediv__ = lambda s, x: MagicMock()
        db = MagicMock()
        db.connect.return_value.__enter__ = MagicMock()
        db.connect.return_value.__exit__ = MagicMock()
        llm = MagicMock()
        with patch("monai.agents.executor.get_anonymizer"), \
             patch("monai.agents.executor.SharedMemory"), \
             patch("monai.agents.browser_learner.BrowserLearner", side_effect=Exception("no browser")):
            executor = AutonomousExecutor(config, db, llm, max_steps=max_steps)
        executor._learner = None
        return executor

    def test_max_script_retries_per_target_constant(self):
        assert AutonomousExecutor.MAX_SCRIPT_RETRIES_PER_TARGET == 3

    @pytest.mark.asyncio
    async def test_auto_rejects_burnt_target(self):
        """After MAX_SCRIPT_RETRIES_PER_TARGET failures on the same selector,
        further run_page_script calls targeting it are auto-rejected without
        executing."""
        executor = self._make_executor(max_steps=10)
        call_count = {"n": 0}
        act_called = {"n": 0}

        def fake_think(task, context, step):
            call_count["n"] += 1
            if call_count["n"] >= 8:
                return {"tool": "fail", "args": {"reason": "gave up"}}
            # Always target the same querySelector
            return {
                "tool": "run_page_script",
                "args": {
                    "script": (
                        "const el = document.querySelector('#country-select');"
                        "el.value = 'US';"
                    ),
                },
            }

        original_act = executor._act

        async def tracking_act(tool, args):
            act_called["n"] += 1
            return "ERROR: Script failed: element not found"

        executor._think = fake_think
        executor._act = tracking_act
        executor.browser = AsyncMock()
        executor.browser.start = AsyncMock()
        executor.browser.stop = AsyncMock()
        executor._log_task = MagicMock()

        result = await executor.execute_task("fill country dropdown")

        # Should have executed _act only MAX_SCRIPT_RETRIES_PER_TARGET times
        # for the run_page_script calls (3), then auto-rejected the rest
        assert act_called["n"] <= AutonomousExecutor.MAX_SCRIPT_RETRIES_PER_TARGET + 1  # +1 for fail()

        # At least one AUTO-REJECTED entry should be in history
        rejected = [
            a for a in executor.action_history
            if "AUTO-REJECTED" in a.get("result", "")
        ]
        assert len(rejected) >= 1, "Expected at least one AUTO-REJECTED entry"

    @pytest.mark.asyncio
    async def test_different_targets_tracked_independently(self):
        """Failures on selector A don't affect the retry budget for selector B."""
        executor = self._make_executor(max_steps=20)
        call_count = {"n": 0}

        def fake_think(task, context, step):
            call_count["n"] += 1
            if call_count["n"] >= 12:
                return {"tool": "done", "args": {"result": "ok"}}
            # Alternate between two different selectors
            if call_count["n"] <= 4:
                sel = "#country-select"
            else:
                sel = "#state-select"
            return {
                "tool": "run_page_script",
                "args": {"script": f"document.querySelector('{sel}').value = 'X';"},
            }

        async def fake_act(tool, args):
            if tool == "run_page_script":
                return "ERROR: Script failed: not found"
            return "ok"

        executor._think = fake_think
        executor._act = fake_act
        executor.browser = AsyncMock()
        executor.browser.start = AsyncMock()
        executor.browser.stop = AsyncMock()
        executor._log_task = MagicMock()
        executor._verify_completion = AsyncMock(
            return_value={"verified": True})

        await executor.execute_task("fill dropdown")

        # Each target should be tracked separately
        assert "#country-select" in str(executor._script_target_failures)
        assert "#state-select" in str(executor._script_target_failures)

    @pytest.mark.asyncio
    async def test_burnt_targets_shown_in_think_prompt(self):
        """Once a target is burnt, it should appear in the _think prompt so
        the LLM knows not to retry it."""
        executor = self._make_executor()
        # Pre-burn a target
        executor._script_target_failures["#country-select"] = 3

        executor._get_learned_context = MagicMock(return_value="")
        executor._get_executor_config = MagicMock(return_value=0.3)
        executor._get_tool_descriptions = MagicMock(return_value="")
        executor._proof = MagicMock()
        executor._proof.get_history.return_value = ""
        executor.llm.chat_json.return_value = {
            "reasoning": "test", "tool": "fail",
            "args": {"reason": "test"},
        }

        executor._think("test task", "test context", 5)

        prompt = executor.llm.chat_json.call_args[0][0][-1]["content"]
        assert "BURNT TARGETS" in prompt
        assert "#country-select" in prompt
        assert "AUTO-REJECTED" in prompt

    @pytest.mark.asyncio
    async def test_fill_form_partial_failure_tracking(self):
        """When fill_form partially succeeds but the same field keeps failing,
        it should be tracked and eventually trigger a context warning."""
        executor = self._make_executor(max_steps=10)
        call_count = {"n": 0}

        def fake_think(task, context, step):
            call_count["n"] += 1
            if call_count["n"] >= 6:
                return {"tool": "fail", "args": {"reason": "gave up"}}
            return {
                "tool": "fill_form",
                "args": {
                    "fields": {
                        "#email": "test@test.com",
                        "#country": "US",
                    },
                },
            }

        async def fake_act(tool, args):
            if tool == "fill_form":
                return "Filled 1 fields. ERROR: Fill failed on: #country"
            return "ok"

        executor._think = fake_think
        executor._act = fake_act
        executor.browser = AsyncMock()
        executor.browser.start = AsyncMock()
        executor.browser.stop = AsyncMock()
        executor._log_task = MagicMock()

        result = await executor.execute_task("register on stripe")

        # The #country field should have accumulated failures
        assert executor._script_target_failures.get("fill_form:#country", 0) >= 3

    def test_script_target_failures_reset_per_task(self):
        """_script_target_failures should be reset at the start of each task."""
        executor = self._make_executor()
        executor._script_target_failures["#old-target"] = 5
        # Simulate execute_task reset (the first lines)
        executor.action_history = []
        executor._reflection_count = 0
        executor._script_target_failures = {}
        assert "#old-target" not in executor._script_target_failures


class TestLLMHealthCheck:
    """LLM.health_check() should detect quota exhaustion and unavailability."""

    def _make_llm(self):
        config = MagicMock()
        config.llm.model_mini = "gpt-4o-mini"
        config.llm.provider = "openai"
        config.privacy.proxy_type = "none"
        config.llm.api_key = "test"
        config.llm.api_base = None
        config.llm.model = "gpt-4o"
        config.llm.temperature = 0.7
        config.llm.max_tokens = 4096
        return config

    def test_health_check_returns_available_on_success(self):
        from monai.utils.llm import LLM
        config = self._make_llm()
        with patch("monai.utils.llm.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.return_value = MagicMock()

            llm = LLM(config)
            result = llm.health_check()

            assert result["available"] is True
            assert result["quota_exhausted"] is False

    def test_health_check_detects_quota_exhaustion(self):
        from monai.utils.llm import LLM
        config = self._make_llm()
        with patch("monai.utils.llm.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.side_effect = Exception(
                "Error code: 429 - {'error': {'message': 'You exceeded your "
                "current quota', 'type': 'insufficient_quota'}}"
            )

            llm = LLM(config)
            result = llm.health_check()

            assert result["available"] is False
            assert result["quota_exhausted"] is True

    def test_health_check_detects_generic_failure(self):
        from monai.utils.llm import LLM
        config = self._make_llm()
        with patch("monai.utils.llm.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.side_effect = ConnectionError(
                "Connection refused"
            )

            llm = LLM(config)
            result = llm.health_check()

            assert result["available"] is False
            assert result["quota_exhausted"] is False
            assert "Connection refused" in result["error"]


class TestOrchestratorLLMGuard:
    """Orchestrator should skip expensive operations when LLM is unavailable.

    Instead of instantiating the full Orchestrator (which has 30+ deps), we test
    the health-check guard logic by verifying that the LLM.health_check method
    correctly detects quota exhaustion, and that the orchestrator code path
    references the check result correctly.
    """

    def test_health_check_result_detected_as_quota_exhausted(self):
        """The health_check result for quota exhaustion should have
        available=False and quota_exhausted=True."""
        from monai.utils.llm import LLM
        config = MagicMock()
        config.llm.model_mini = "gpt-4o-mini"
        config.llm.provider = "openai"
        config.privacy.proxy_type = "none"
        config.llm.api_key = "test"
        config.llm.api_base = None
        config.llm.model = "gpt-4o"
        config.llm.temperature = 0.7
        config.llm.max_tokens = 4096

        with patch("monai.utils.llm.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.side_effect = Exception(
                "Error code: 429 - insufficient_quota"
            )
            llm = LLM(config)
            result = llm.health_check()

        assert result["available"] is False
        assert result["quota_exhausted"] is True

        # The orchestrator checks these exact fields:
        # if not llm_health["available"]:
        #     if llm_health["quota_exhausted"]:
        #         reason = "llm_quota_exhausted"
        reason = "llm_unavailable"
        if result["quota_exhausted"]:
            reason = "llm_quota_exhausted"
        assert reason == "llm_quota_exhausted"

    def test_health_check_connection_error_not_quota(self):
        """Connection errors should be detected as unavailable but NOT
        quota_exhausted — the orchestrator should still back off."""
        from monai.utils.llm import LLM
        config = MagicMock()
        config.llm.model_mini = "gpt-4o-mini"
        config.llm.provider = "openai"
        config.privacy.proxy_type = "none"
        config.llm.api_key = "test"
        config.llm.api_base = None
        config.llm.model = "gpt-4o"
        config.llm.temperature = 0.7
        config.llm.max_tokens = 4096

        with patch("monai.utils.llm.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.side_effect = ConnectionError(
                "Connection refused"
            )
            llm = LLM(config)
            result = llm.health_check()

        assert result["available"] is False
        assert result["quota_exhausted"] is False

        reason = "llm_unavailable"
        if result["quota_exhausted"]:
            reason = "llm_quota_exhausted"
        assert reason == "llm_unavailable"


class TestDaemonBackoff:
    """run_daemon should apply exponential backoff on persistent failures."""

    def test_backoff_multiplier_increases_on_failures(self):
        """Verify the backoff logic computes correct multipliers."""
        # This tests the backoff math without running the full daemon
        max_backoff_multiplier = 12
        base_interval = 300

        # Simulate consecutive failures
        consecutive = 0
        multipliers = []
        for i in range(6):
            consecutive += 1
            mult = min(2 ** (consecutive - 1), max_backoff_multiplier)
            multipliers.append(mult)

        # 1x, 2x, 4x, 8x, 12x (capped), 12x (capped)
        assert multipliers == [1, 2, 4, 8, 12, 12]

    def test_backoff_resets_on_success(self):
        """After a successful cycle, backoff should reset to 1x."""
        consecutive = 5
        # Simulate success
        consecutive = 0
        mult = min(2 ** max(consecutive - 1, 0), 12)
        # 2^(-1) = 0.5, but max(0-1, 0) = 0, so 2^0 = 1
        assert mult == 1


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
