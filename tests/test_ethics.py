"""Tests for monai.agents.ethics."""

from monai.agents.ethics import (
    BLOCKED_ACTIONS,
    CORE_DIRECTIVES,
    REQUIRE_APPROVAL_PATTERNS,
    get_directives_for_context,
    get_full_directives,
    is_action_blocked,
    requires_risk_check,
)


class TestCoreDirectives:
    def test_core_directives_exist(self):
        assert "REAL CONSEQUENCES" in CORE_DIRECTIVES
        assert "CREATOR PROTECTION" in CORE_DIRECTIVES
        assert "LEGAL COMPLIANCE" in CORE_DIRECTIVES
        assert "FINANCIAL DISCIPLINE" in CORE_DIRECTIVES
        assert "QUALITY OVER SPEED" in CORE_DIRECTIVES
        assert "ENGINEERING EXCELLENCE" in CORE_DIRECTIVES

    def test_has_ten_directives(self):
        # Count numbered directives (1. through 10.)
        count = sum(1 for i in range(1, 11) if f"{i}." in CORE_DIRECTIVES)
        assert count == 10


class TestIsActionBlocked:
    def test_blocks_dangerous_commands(self):
        assert is_action_blocked("rm -rf /") is True
        assert is_action_blocked("DROP TABLE users") is True
        assert is_action_blocked("run shutdown now") is True
        assert is_action_blocked("chmod 777 /etc/passwd") is True

    def test_blocks_fork_bomb(self):
        assert is_action_blocked(":(){ :|:& };:") is True

    def test_allows_safe_commands(self):
        assert is_action_blocked("ls -la") is False
        assert is_action_blocked("pip install requests") is False
        assert is_action_blocked("python script.py") is False

    def test_case_insensitive(self):
        assert is_action_blocked("DROP TABLE users") is True
        assert is_action_blocked("SHUTDOWN") is True


class TestRequiresRiskCheck:
    def test_flags_payment_actions(self):
        assert requires_risk_check("process payment for $50") is True
        assert requires_risk_check("purchase domain example.com") is True
        assert requires_risk_check("subscribe to premium plan") is True

    def test_flags_legal_actions(self):
        assert requires_risk_check("sign contract with client") is True
        assert requires_risk_check("accept legal agreement") is True
        assert requires_risk_check("agree to terms of service") is True

    def test_passes_normal_actions(self):
        assert requires_risk_check("browse website") is False
        assert requires_risk_check("write code") is False
        assert requires_risk_check("send email") is False


class TestGetDirectivesForContext:
    def test_financial_context(self):
        text = get_directives_for_context("financial")
        assert "FINANCIAL RULES" in text
        assert "CORE DIRECTIVES" in text
        assert "CLIENT INTERACTION" not in text

    def test_client_context(self):
        text = get_directives_for_context("client")
        assert "CLIENT INTERACTION" in text
        assert "FINANCIAL RULES" not in text

    def test_code_context(self):
        text = get_directives_for_context("code")
        assert "CODE GENERATION RULES" in text
        assert "Every function must have at least one test" in text

    def test_content_context(self):
        text = get_directives_for_context("content")
        assert "CONTENT GENERATION RULES" in text

    def test_general_context_includes_all(self):
        text = get_directives_for_context("general")
        assert "FINANCIAL RULES" in text
        assert "CLIENT INTERACTION" in text
        assert "CODE GENERATION RULES" in text
        assert "CONTENT GENERATION RULES" in text

    def test_unknown_context_returns_core_only(self):
        text = get_directives_for_context("unknown")
        assert "CORE DIRECTIVES" in text
        assert "FINANCIAL RULES" not in text


class TestGetFullDirectives:
    def test_includes_all_sections(self):
        text = get_full_directives()
        assert "CORE DIRECTIVES" in text
        assert "FINANCIAL RULES" in text
        assert "CLIENT INTERACTION" in text
        assert "CODE GENERATION RULES" in text
        assert "CONTENT GENERATION RULES" in text
