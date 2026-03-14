"""Tests for ProofOfCompletion — the anti-hallucination verification layer."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monai.agents.proof import ProofOfCompletion


class TestProofOfCompletion:
    """ProofOfCompletion should catch executor hallucinations."""

    def _make_proof(self):
        config = MagicMock()
        db = MagicMock()
        db.connect.return_value.__enter__ = MagicMock()
        db.connect.return_value.__exit__ = MagicMock()
        llm = MagicMock()
        memory = MagicMock()
        memory.record_lesson = MagicMock()
        return ProofOfCompletion(config, db, llm, memory)

    # ── Action Trail Checks ──────────────────────────────────────

    def test_empty_history_rejected(self):
        proof = self._make_proof()
        result = proof.verify(
            task="Create an email account",
            claimed_result="Email created: test@mail.com",
            action_history=[],
        )
        assert not result["verified"]
        assert "no actions" in result["reason"].lower()

    def test_only_passive_actions_rejected(self):
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "read_page", "args": {}, "result": "some text"},
            {"step": 2, "tool": "screenshot", "args": {"name": "page"}, "result": "saved"},
            {"step": 3, "tool": "wait", "args": {"seconds": 2}, "result": "waited"},
        ]
        result = proof.verify(
            task="Register account on Upwork",
            claimed_result="Account created successfully",
            action_history=history,
        )
        assert not result["verified"]
        assert "productive actions" in result["reason"].lower()

    def test_all_productive_actions_failed_rejected(self):
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://upwork.com"},
             "result": "ERROR: Navigation failed (bot_detection)"},
            {"step": 2, "tool": "browse", "args": {"url": "https://upwork.com"},
             "result": "BLOCKED by proxy"},
        ]
        result = proof.verify(
            task="Register account on Upwork",
            claimed_result="Account created on Upwork",
            action_history=history,
        )
        assert not result["verified"]
        assert "all" in result["reason"].lower() and "failed" in result["reason"].lower()

    def test_productive_actions_accepted(self):
        """Executor with real browse→fill→submit sequence should pass."""
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://site.com/register"},
             "result": "Page loaded: Registration"},
            {"step": 2, "tool": "fill_form", "args": {"fields": {"email": "real@mail.com"}},
             "result": "Filled 1 fields"},
            {"step": 3, "tool": "submit", "args": {"selector": "form"},
             "result": "Page: Thank you for registering"},
        ]
        # Mock asset manager to return inventory with the account
        with patch.object(proof._asset_mgr, "get_inventory") as mock_inv:
            inv = MagicMock()
            inv.assets = []  # No asset check needed for generic task
            mock_inv.return_value = inv
            result = proof.verify(
                task="Fill out the contact form on site.com",
                claimed_result="Form submitted successfully",
                action_history=history,
            )
        assert result["verified"]

    def test_simple_read_task_allows_fewer_actions(self):
        """Simple read/check tasks should accept 1 productive action."""
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "http_get", "args": {"url": "https://api.example.com/status"},
             "result": '{"status": "ok"}'},
        ]
        result = proof.verify(
            task="Check the status of the API endpoint",
            claimed_result="API is running, status: ok",
            action_history=history,
        )
        assert result["verified"]

    # ── Asset Verification Checks ────────────────────────────────

    def test_email_task_rejected_when_no_email_in_db(self):
        """Task about creating email should fail if no email in DB."""
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://mail.tm"},
             "result": "Page loaded"},
            {"step": 2, "tool": "fill_form", "args": {"fields": {"email": "new@mail.tm"}},
             "result": "Filled 1 fields"},
            {"step": 3, "tool": "submit", "args": {"selector": "form"},
             "result": "Page updated"},
        ]
        with patch.object(proof._asset_mgr, "get_inventory") as mock_inv:
            inv = MagicMock()
            inv.assets = []  # No email in DB!
            mock_inv.return_value = inv
            result = proof.verify(
                task="Create a new email account",
                claimed_result="Email created: new@mail.tm",
                action_history=history,
            )
        assert not result["verified"]
        assert "email" in result["reason"].lower()
        assert "no email" in result["reason"].lower() or "not found" in result["reason"].lower()

    def test_email_task_accepted_when_email_in_db(self):
        """Task about creating email should pass if email exists in DB."""
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://mail.tm"},
             "result": "Page loaded"},
            {"step": 2, "tool": "fill_form",
             "args": {"fields": {"email": "real@mail.tm", "password": "secret123"}},
             "result": "Filled 2 fields"},
            {"step": 3, "tool": "submit", "args": {"selector": "form"},
             "result": "Welcome to mail.tm"},
        ]
        with patch.object(proof._asset_mgr, "get_inventory") as mock_inv:
            from monai.agents.asset_aware import Asset
            inv = MagicMock()
            inv.assets = [
                Asset(type="email", platform="mail.tm",
                      identifier="real@mail.tm", status="active"),
            ]
            mock_inv.return_value = inv
            result = proof.verify(
                task="Create a new email account on mail.tm",
                claimed_result="Email created: real@mail.tm",
                action_history=history,
            )
        assert result["verified"]

    def test_account_task_rejected_when_no_account_in_db(self):
        """Task about platform registration should fail if no account in DB."""
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://upwork.com/signup"},
             "result": "Page loaded: Sign Up"},
            {"step": 2, "tool": "fill_form", "args": {"fields": {"name": "John"}},
             "result": "Filled 1 fields"},
            {"step": 3, "tool": "click", "args": {"selector": "#submit"},
             "result": "Clicked"},
        ]
        with patch.object(proof._asset_mgr, "get_inventory") as mock_inv:
            inv = MagicMock()
            inv.assets = []
            mock_inv.return_value = inv
            result = proof.verify(
                task="Create account on Upwork",
                claimed_result="Account created on Upwork",
                action_history=history,
            )
        assert not result["verified"]
        assert "platform_account" in result["reason"].lower()

    # ── Hallucination Pattern Detection ──────────────────────────

    def test_claimed_email_not_in_history_rejected(self):
        """If executor claims email X but X never appeared in actions, reject."""
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://mail.tm"},
             "result": "Page loaded"},
            {"step": 2, "tool": "click", "args": {"selector": "#register"},
             "result": "Form appeared"},
            {"step": 3, "tool": "submit", "args": {"selector": "form"},
             "result": "Page updated"},
        ]
        result = proof.verify(
            task="Do something useful",
            claimed_result="Created email: hallucinated@nowhere.com",
            action_history=history,
        )
        assert not result["verified"]
        assert "hallucinated@nowhere.com" in result["reason"]

    def test_claimed_email_in_history_accepted(self):
        """If executor claims email X and X was typed in form, accept."""
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://mail.tm"},
             "result": "Page loaded"},
            {"step": 2, "tool": "fill_form",
             "args": {"fields": {"email": "real@mail.tm"}},
             "result": "Filled 1 fields"},
            {"step": 3, "tool": "submit", "args": {"selector": "form"},
             "result": "Account created for real@mail.tm"},
        ]
        result = proof.verify(
            task="Do something useful",
            claimed_result="Created email: real@mail.tm",
            action_history=history,
        )
        assert result["verified"]

    def test_placeholder_emails_rejected(self):
        """Claims containing example.com or test@test should be rejected."""
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://site.com"},
             "result": "Page loaded"},
            {"step": 2, "tool": "click", "args": {"selector": "a"},
             "result": "clicked"},
        ]
        result = proof.verify(
            task="Do something useful",
            claimed_result="Created account with user@example.com",
            action_history=history,
        )
        assert not result["verified"]
        assert "placeholder" in result["reason"].lower() or "example.com" in result["reason"]

    # ── Confirmation Page Detection ──────────────────────────────

    def test_confirmation_page_positive_signals(self):
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://site.com"},
             "result": "loaded"},
            {"step": 2, "tool": "fill_form", "args": {"fields": {"name": "x"}},
             "result": "filled"},
            {"step": 3, "tool": "submit", "args": {"selector": "form"},
             "result": "submitted"},
        ]
        # Use a task that doesn't trigger asset_created checks
        result = proof.verify(
            task="Fill out the contact form on site.com",
            claimed_result="Form submitted successfully",
            action_history=history,
            page_text="Thank you for your submission! We'll be in touch.",
            page_url="https://site.com/thank-you",
        )
        # Confirmation page check should pass (advisory)
        page_checks = [c for c in result["checks"] if c["check"] == "confirmation_page"]
        assert page_checks
        assert page_checks[0]["passed"]

    def test_error_page_flagged(self):
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://site.com"},
             "result": "loaded"},
            {"step": 2, "tool": "fill_form", "args": {"fields": {"name": "x"}},
             "result": "filled"},
            {"step": 3, "tool": "submit", "args": {"selector": "form"},
             "result": "submitted"},
        ]
        # Use a task that doesn't trigger asset_created checks
        result = proof.verify(
            task="Fill out the contact form on site.com",
            claimed_result="Form submitted successfully",
            action_history=history,
            page_text="Error: Email address is invalid. Please try again.",
            page_url="https://site.com/contact",
        )
        page_checks = [c for c in result["checks"] if c["check"] == "confirmation_page"]
        assert page_checks
        assert not page_checks[0]["passed"]

    # ── Learning Integration ─────────────────────────────────────

    def test_verification_failure_records_lesson(self):
        """Verification failures should be stored as lessons."""
        proof = self._make_proof()
        history = []  # Empty history = instant rejection
        proof.verify(
            task="Create email account",
            claimed_result="Email created!",
            action_history=history,
        )
        # Should have called record_lesson
        proof.memory.record_lesson.assert_called_once()
        call_args = proof.memory.record_lesson.call_args
        assert call_args.kwargs["category"] == "hallucination"
        assert call_args.kwargs["severity"] == "high"

    def test_successful_verification_does_not_record_lesson(self):
        """Successful verifications should NOT store failure lessons."""
        proof = self._make_proof()
        history = [
            {"step": 1, "tool": "http_get",
             "args": {"url": "https://api.example.com/status"},
             "result": '{"status": "ok"}'},
        ]
        proof.verify(
            task="Check API status",
            claimed_result="API is running",
            action_history=history,
        )
        proof.memory.record_lesson.assert_not_called()


class TestExecutorProofIntegration:
    """Test that the executor properly integrates proof-of-completion."""

    def _make_executor(self):
        from monai.agents.executor import AutonomousExecutor
        config = MagicMock()
        config.data_dir = MagicMock()
        config.data_dir.__truediv__ = lambda s, x: MagicMock()
        db = MagicMock()
        db.connect.return_value.__enter__ = MagicMock()
        db.connect.return_value.__exit__ = MagicMock()
        llm = MagicMock()
        with patch("monai.agents.executor.get_anonymizer"), \
             patch("monai.agents.executor.SharedMemory"), \
             patch("monai.agents.executor.ProofOfCompletion") as mock_proof_cls, \
             patch("monai.agents.browser_learner.BrowserLearner",
                   side_effect=Exception("no browser")):
            executor = AutonomousExecutor(config, db, llm, max_steps=10)
        executor._learner = None
        return executor, mock_proof_cls

    @pytest.mark.asyncio
    async def test_done_triggers_verification(self):
        """Calling done() should trigger proof-of-completion verification."""
        executor, _ = self._make_executor()

        call_count = {"n": 0}

        def fake_think(task, context, step):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"tool": "browse", "args": {"url": "https://site.com"}}
            return {"tool": "done", "args": {"result": "Task completed"}}

        executor._think = fake_think
        executor.browser = AsyncMock()
        executor.browser.navigate = AsyncMock(return_value="ok")
        executor.browser.get_page_info = AsyncMock(return_value={"text": "ok"})
        executor.browser._get_page = AsyncMock(
            return_value=MagicMock(url="https://site.com"))
        executor.browser.get_text = AsyncMock(return_value="page text")
        executor._log_task = MagicMock()

        # Mock verification to pass
        executor._proof.verify = MagicMock(return_value={
            "verified": True, "reason": "ok", "checks": [],
        })

        result = await executor.execute_task("test task")
        assert result["status"] == "completed"
        assert "proof" in result
        executor._proof.verify.assert_called_once()

    @pytest.mark.asyncio
    async def test_done_rejected_when_verification_fails(self):
        """If verification fails, done() should be rejected and executor continues."""
        executor, _ = self._make_executor()

        call_count = {"n": 0}

        def fake_think(task, context, step):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return {"tool": "browse", "args": {"url": "https://site.com"}}
            return {"tool": "done", "args": {"result": "Task completed"}}

        executor._think = fake_think
        executor.browser = AsyncMock()
        executor.browser.navigate = AsyncMock(return_value="ok")
        executor.browser.get_page_info = AsyncMock(return_value={"text": "ok"})
        executor.browser._get_page = AsyncMock(
            return_value=MagicMock(url="https://site.com"))
        executor.browser.get_text = AsyncMock(return_value="page text")
        executor._log_task = MagicMock()

        # Mock verification to always fail
        executor._proof.verify = MagicMock(return_value={
            "verified": False,
            "reason": "No email found in DB",
            "checks": [{"check": "asset", "passed": False, "reason": "No email"}],
        })

        result = await executor.execute_task("Create email account")

        # After 3 verification failures, should report failed
        assert result["status"] == "failed"
        assert "verification" in result["reason"].lower()
        assert executor._proof.verify.call_count >= 3
