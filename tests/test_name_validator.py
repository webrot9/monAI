"""Tests for monai.agents.name_validator — real-world business name validation."""

import json
from unittest.mock import MagicMock, patch

import pytest

from monai.agents.name_validator import (
    FullValidation,
    NameValidator,
    ValidationResult,
    PLATFORM_USERNAME_URLS,
)


class TestValidationResult:
    """Test the ValidationResult dataclass."""

    def test_available_domain(self):
        r = ValidationResult(
            check_type="domain",
            target="example123xyz.com",
            available=True,
            details="NXDOMAIN",
        )
        assert r.available is True
        assert r.check_type == "domain"

    def test_taken_domain(self):
        r = ValidationResult(
            check_type="domain",
            target="google.com",
            available=False,
            details="resolves",
        )
        assert r.available is False

    def test_unknown_result(self):
        r = ValidationResult(
            check_type="domain",
            target="test.com",
            available=None,
            details="error",
        )
        assert r.available is None


class TestFullValidation:
    """Test the FullValidation aggregate."""

    def test_viable_when_no_blockers(self):
        v = FullValidation(name="TestCorp", overall_viable=True)
        assert v.overall_viable is True
        assert v.blockers == []

    def test_not_viable_with_blockers(self):
        v = FullValidation(
            name="TestCorp",
            blockers=["Domain taken"],
            overall_viable=False,
        )
        assert v.overall_viable is False
        assert len(v.blockers) == 1

    def test_to_dict(self):
        v = FullValidation(
            name="TestCorp",
            overall_viable=True,
            warnings=["Medium trademark risk"],
            checks=[
                ValidationResult("domain", "testcorp.com", True, "available"),
            ],
        )
        d = v.to_dict()
        assert d["name"] == "TestCorp"
        assert d["viable"] is True
        assert len(d["checks"]) == 1
        assert d["checks"][0]["type"] == "domain"


class TestNameValidator:
    @pytest.fixture
    def validator(self, config, db, mock_llm):
        v = NameValidator(config, db, mock_llm)
        yield v
        v.close()

    # ── Schema ────────────────────────────────────────────────

    def test_schema_created(self, validator, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='name_validations'"
        )
        assert len(rows) == 1

    # ── Domain DNS Check ──────────────────────────────────────

    @patch("monai.agents.name_validator.socket.getaddrinfo")
    def test_check_domain_resolves(self, mock_dns, validator):
        """Domain that resolves = taken."""
        mock_dns.return_value = [("AF_INET", None, None, None, ("1.2.3.4", 80))]
        result = validator.check_domain("google.com")
        assert result.available is False
        assert "resolves" in result.details

    @patch("monai.agents.name_validator.socket.getaddrinfo")
    def test_check_domain_nxdomain(self, mock_dns, validator):
        """Domain that doesn't resolve = likely available."""
        import socket
        mock_dns.side_effect = socket.gaierror("NXDOMAIN")
        result = validator.check_domain("xyznotexist123456.com")
        assert result.available is True
        assert "does not resolve" in result.details

    def test_check_domain_invalid_format(self, validator):
        result = validator.check_domain("not a domain!!!")
        assert result.available is None
        assert "Invalid domain format" in result.details

    def test_check_domain_stores_result(self, validator, db):
        """Validation results are stored in DB for audit trail."""
        with patch("monai.agents.name_validator.socket.getaddrinfo") as mock:
            import socket
            mock.side_effect = socket.gaierror("NXDOMAIN")
            validator.check_domain("testaudit.com")

        rows = db.execute("SELECT * FROM name_validations WHERE name = 'testaudit.com'")
        assert len(rows) == 1
        assert rows[0]["status"] == "available"

    # ── Domain WHOIS Check ────────────────────────────────────

    def test_check_domain_whois_nxdomain(self, validator):
        """WHOIS check via DNS-over-HTTPS — NXDOMAIN response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"Status": 3, "Answer": None}  # NXDOMAIN
        validator._http = MagicMock()
        validator._http.get.return_value = mock_resp

        result = validator.check_domain_whois("available123.com")
        assert result.available is True
        assert "NXDOMAIN" in result.details

    def test_check_domain_whois_has_records(self, validator):
        """WHOIS check — domain has DNS records = taken."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"Status": 0, "Answer": [{"data": "1.2.3.4"}]}
        validator._http = MagicMock()
        validator._http.get.return_value = mock_resp

        result = validator.check_domain_whois("google.com")
        assert result.available is False
        assert "taken" in result.details

    def test_check_domain_whois_fallback_on_api_error(self, validator):
        """Falls back to DNS check if WHOIS API fails."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        validator._http = MagicMock()
        validator._http.get.return_value = mock_resp

        with patch("monai.agents.name_validator.socket.getaddrinfo") as mock_dns:
            import socket
            mock_dns.side_effect = socket.gaierror("NXDOMAIN")
            result = validator.check_domain_whois("fallback.com")
            assert result.available is True  # Fell back to DNS check

    # ── Username Checks ───────────────────────────────────────

    def test_check_username_available(self, validator):
        """404 on profile URL = username available."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        validator._http = MagicMock()
        validator._http.get.return_value = mock_resp

        result = validator.check_username("uniqueuser123xyz", "github")
        assert result.available is True
        assert "available" in result.details

    def test_check_username_taken(self, validator):
        """200 on profile URL = username taken."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        validator._http = MagicMock()
        validator._http.get.return_value = mock_resp

        result = validator.check_username("octocat", "github")
        assert result.available is False
        assert "taken" in result.details

    def test_check_username_unknown_platform(self, validator):
        """Unknown platform returns None available."""
        result = validator.check_username("test", "unknownplatform")
        assert result.available is None
        assert "No URL pattern" in result.details

    def test_check_username_http_error(self, validator):
        """HTTP errors result in unknown availability."""
        validator._http = MagicMock()
        validator._http.get.side_effect = Exception("Connection refused")

        result = validator.check_username("test", "github")
        assert result.available is None
        assert "Check failed" in result.details

    # ── LLC Name Check ────────────────────────────────────────

    def test_check_llc_name_available(self, validator):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Some unrelated search results about Wyoming"
        validator._http = MagicMock()
        validator._http.get.return_value = mock_resp

        result = validator.check_llc_name("NexifyWonder")
        assert result.available is True
        assert "appears available" in result.details

    def test_check_llc_name_exists(self, validator):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '"nexifywonder" LLC is an active company registered in Wyoming'
        validator._http = MagicMock()
        validator._http.get.return_value = mock_resp

        result = validator.check_llc_name("NexifyWonder")
        assert result.available is False
        assert "already exists" in result.details

    # ── Trademark Check ───────────────────────────────────────

    def test_check_trademark_low_risk(self, validator, mock_llm):
        mock_llm.quick_json.return_value = {
            "risk_level": "low",
            "conflicts": [],
            "reasoning": "Unique name, no known conflicts",
        }
        result = validator.check_trademark("NexifyZeta")
        assert result.available is True
        assert "Low trademark risk" in result.details

    def test_check_trademark_high_risk(self, validator, mock_llm):
        mock_llm.quick_json.return_value = {
            "risk_level": "high",
            "conflicts": ["Apple Inc."],
            "reasoning": "Too similar to Apple",
        }
        result = validator.check_trademark("Appel")
        assert result.available is False
        assert "HIGH trademark risk" in result.details
        assert "Apple Inc." in result.details

    def test_check_trademark_medium_risk(self, validator, mock_llm):
        mock_llm.quick_json.return_value = {
            "risk_level": "medium",
            "conflicts": [],
            "reasoning": "Some similarity to existing brands",
        }
        result = validator.check_trademark("TestBrand")
        assert result.available is True  # Proceed with caution
        assert "Medium trademark risk" in result.details

    # ── Full Validation Pipeline ──────────────────────────────

    def test_validate_business_identity_all_clear(self, validator, mock_llm):
        """All checks pass — identity is viable."""
        # Mock domain WHOIS
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"Status": 3}  # NXDOMAIN
        validator._http = MagicMock()
        validator._http.get.return_value = mock_resp

        # Mock LLC check (Google returns unrelated results)
        mock_resp_llc = MagicMock()
        mock_resp_llc.status_code = 200
        mock_resp_llc.text = "Unrelated Wyoming business search results"

        # Set up returns in order: WHOIS, LLC search
        validator._http.get.side_effect = [mock_resp, mock_resp_llc]

        mock_llm.quick_json.return_value = {
            "risk_level": "low",
            "conflicts": [],
            "reasoning": "Unique",
        }

        v = validator.validate_business_identity(
            name="NexifyZeta",
            domain="nexifyzeta.com",
            jurisdiction="US-WY",
        )
        assert v.overall_viable is True
        assert len(v.blockers) == 0
        assert len(v.checks) >= 3  # domain + llc + trademark

    def test_validate_business_identity_domain_taken(self, validator, mock_llm):
        """Domain taken = blocker."""
        # Mock domain WHOIS — taken
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"Status": 0, "Answer": [{"data": "1.2.3.4"}]}
        validator._http = MagicMock()

        # LLC check — available
        mock_resp_llc = MagicMock()
        mock_resp_llc.status_code = 200
        mock_resp_llc.text = "Unrelated results"

        validator._http.get.side_effect = [mock_resp, mock_resp_llc]

        mock_llm.quick_json.return_value = {
            "risk_level": "low", "conflicts": [], "reasoning": "Fine",
        }

        v = validator.validate_business_identity(
            name="TestCorp", domain="testcorp.com",
        )
        assert v.overall_viable is False
        assert any("taken" in b.lower() for b in v.blockers)

    def test_validate_business_identity_trademark_blocker(self, validator, mock_llm):
        """High trademark risk = blocker."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Unrelated"
        validator._http = MagicMock()
        validator._http.get.return_value = mock_resp

        mock_llm.quick_json.return_value = {
            "risk_level": "high",
            "conflicts": ["Google"],
            "reasoning": "Identical to Google",
        }

        v = validator.validate_business_identity(name="Googol")
        assert v.overall_viable is False
        assert any("trademark" in b.lower() for b in v.blockers)

    # ── Generate and Validate Loop ────────────────────────────

    @pytest.mark.real_validator
    def test_generate_and_validate_first_try_viable(self, validator, mock_llm):
        """First generated name passes all checks."""
        mock_llm.quick_json.side_effect = [
            # 1st call: generate identity
            {"name": "ZetaNova", "tagline": "Innovation", "description": "Digital",
             "preferred_username": "zetanova", "business_type": "services"},
            # 2nd call: trademark check
            {"risk_level": "low", "conflicts": [], "reasoning": "Unique"},
        ]

        # Mock DNS for domain check — NXDOMAIN
        with patch("monai.agents.name_validator.socket.getaddrinfo") as mock_dns:
            import socket
            mock_dns.side_effect = socket.gaierror("NXDOMAIN")

            # Mock HTTP for LLC check
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "Unrelated"
            mock_resp.json.return_value = {"Status": 3}
            validator._http = MagicMock()
            validator._http.get.return_value = mock_resp

            identity, validation = validator.generate_and_validate()

        assert identity["name"] == "ZetaNova"
        assert validation.overall_viable is True

    @pytest.mark.real_validator
    def test_generate_and_validate_retries_on_failure(self, validator, mock_llm):
        """Retries with feedback when first name has blockers."""
        mock_llm.quick_json.side_effect = [
            # 1st attempt: generate
            {"name": "BadName", "tagline": "X", "description": "X",
             "preferred_username": "badname", "business_type": "X"},
            # 1st attempt: trademark — HIGH risk
            {"risk_level": "high", "conflicts": ["Apple"], "reasoning": "Too similar"},
            # 2nd attempt: generate (with feedback about BadName failing)
            {"name": "ZetaGood", "tagline": "Y", "description": "Y",
             "preferred_username": "zetagood", "business_type": "Y"},
            # 2nd attempt: trademark — low risk
            {"risk_level": "low", "conflicts": [], "reasoning": "Fine"},
        ]

        with patch("monai.agents.name_validator.socket.getaddrinfo") as mock_dns:
            import socket
            mock_dns.side_effect = socket.gaierror("NXDOMAIN")

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "Unrelated"
            mock_resp.json.return_value = {"Status": 3}
            validator._http = MagicMock()
            validator._http.get.return_value = mock_resp

            identity, validation = validator.generate_and_validate(max_attempts=3)

        assert identity["name"] == "ZetaGood"
        assert validation.overall_viable is True
        # The second generate call should contain feedback
        second_call_prompt = mock_llm.quick_json.call_args_list[2][0][0]
        assert "PREVIOUS ATTEMPTS" in second_call_prompt
        assert "BadName" in second_call_prompt

    @pytest.mark.real_validator
    def test_generate_and_validate_returns_best_effort(self, validator, mock_llm):
        """After max attempts, returns the best (least blockers) attempt."""
        mock_llm.quick_json.side_effect = [
            # Every attempt generates same bad name pattern
            {"name": "Bad1", "tagline": "X", "description": "X",
             "preferred_username": "bad1", "business_type": "X"},
            {"risk_level": "high", "conflicts": ["X"], "reasoning": "Bad"},
            {"name": "Bad2", "tagline": "X", "description": "X",
             "preferred_username": "bad2", "business_type": "X"},
            {"risk_level": "high", "conflicts": ["Y"], "reasoning": "Bad"},
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Unrelated"
        mock_resp.json.return_value = {"Status": 3}
        validator._http = MagicMock()
        validator._http.get.return_value = mock_resp

        with patch("monai.agents.name_validator.socket.getaddrinfo") as mock_dns:
            import socket
            mock_dns.side_effect = socket.gaierror("NXDOMAIN")
            identity, validation = validator.generate_and_validate(max_attempts=2)

        # Should return something (best effort), even though not viable
        assert identity["name"] in ("Bad1", "Bad2")

    # ── Validation History ────────────────────────────────────

    def test_get_validation_history(self, validator):
        with patch("monai.agents.name_validator.socket.getaddrinfo") as mock_dns:
            import socket
            mock_dns.side_effect = socket.gaierror("NXDOMAIN")
            validator.check_domain("test1.com")
            validator.check_domain("test2.com")

        history = validator.get_validation_history()
        assert len(history) == 2

    def test_get_validation_history_by_name(self, validator):
        with patch("monai.agents.name_validator.socket.getaddrinfo") as mock_dns:
            import socket
            mock_dns.side_effect = socket.gaierror("NXDOMAIN")
            validator.check_domain("alpha.com")
            validator.check_domain("beta.com")

        history = validator.get_validation_history(name="alpha")
        assert len(history) == 1
        assert history[0]["name"] == "alpha.com"

    # ── Platform URL Patterns ─────────────────────────────────

    def test_platform_urls_contain_required_platforms(self):
        assert "github" in PLATFORM_USERNAME_URLS
        assert "gumroad" in PLATFORM_USERNAME_URLS
        assert "ko-fi" in PLATFORM_USERNAME_URLS

    def test_platform_urls_use_username_placeholder(self):
        for platform, url in PLATFORM_USERNAME_URLS.items():
            assert "{username}" in url, f"Missing {{username}} in {platform} URL"


class TestActionableReflection:
    """Test that reflection produces REAL behavioral changes."""

    @pytest.fixture
    def memory(self, db):
        from monai.agents.memory import SharedMemory
        return SharedMemory(db)

    @pytest.fixture
    def executor(self, config, db, mock_llm):
        from monai.agents.executor import AutonomousExecutor
        # Ensure agent_config table exists
        from monai.agents.self_improve import SELF_IMPROVE_SCHEMA
        with db.connect() as conn:
            conn.executescript(SELF_IMPROVE_SCHEMA)
        return AutonomousExecutor(config, db, mock_llm, headless=True)

    def test_post_task_learn_stores_lesson_in_memory(self, executor, db, mock_llm):
        """Post-task learning stores lessons in SharedMemory."""
        mock_llm.quick.return_value = (
            "LESSON: Browser selectors are fragile on dynamic sites\n"
            "RULE: Always try text-based selectors before CSS selectors\n"
            "ACTION: Use wait_for before click on dynamic pages"
        )

        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://test.com"}, "result": "OK"},
            {"step": 2, "tool": "click", "args": {"selector": "#btn"}, "result": "ERROR: element not found"},
            {"step": 3, "tool": "click", "args": {"selector": ".submit"}, "result": "ERROR: timeout"},
            {"step": 4, "tool": "screenshot", "args": {}, "result": "saved"},
            {"step": 5, "tool": "click", "args": {"selector": "button"}, "result": "ERROR: no match"},
            {"step": 6, "tool": "fail", "args": {"reason": "stuck"}, "result": "Task failed"},
        ]

        executor._post_task_learn("Register on platform X", history)

        # Verify lesson was stored
        lessons = db.execute("SELECT * FROM lessons WHERE agent_name = 'executor'")
        assert len(lessons) >= 1
        assert "selector" in lessons[0]["lesson"].lower() or "browser" in lessons[0]["lesson"].lower()

    def test_post_task_learn_writes_to_agent_config(self, executor, db, mock_llm):
        """Post-task learning writes concrete rules to agent_config."""
        mock_llm.quick.return_value = (
            "LESSON: Test sites block Tor exit nodes\n"
            "RULE: Rotate proxy before navigating\n"
            "ACTION: Use residential proxy for registration pages"
        )

        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://blocked.com"}, "result": "ERROR: 403"},
            {"step": 2, "tool": "browse", "args": {"url": "https://blocked.com"}, "result": "ERROR: 403"},
            {"step": 3, "tool": "browse", "args": {"url": "https://other.com"}, "result": "ERROR: timeout"},
            {"step": 4, "tool": "fail", "args": {}, "result": "failed"},
        ]

        executor._post_task_learn("Navigate to blocked.com", history)

        # Verify rule was written to agent_config
        rows = db.execute(
            "SELECT config_value FROM agent_config "
            "WHERE agent_name = 'executor' AND config_key = 'custom_rules'"
        )
        assert len(rows) == 1
        import json
        value = json.loads(rows[0]["config_value"])
        assert "residential proxy" in value.lower()

    def test_post_task_learn_stores_success_playbooks(self, executor, db, mock_llm):
        """Successful tasks get stored as playbooks for reuse."""
        from monai.agents.memory import SharedMemory
        SharedMemory(db)  # Ensure knowledge table exists

        history = [
            {"step": 1, "tool": "browse", "args": {"url": "https://gumroad.com"}, "result": "loaded"},
            {"step": 2, "tool": "click", "args": {"selector": "#signup"}, "result": "clicked"},
            {"step": 3, "tool": "fill_form", "args": {"fields": {"email": "x"}}, "result": "filled"},
            {"step": 4, "tool": "submit", "args": {"selector": "form"}, "result": "submitted"},
            {"step": 5, "tool": "done", "args": {"result": "registered"}, "result": "done"},
        ]

        executor._post_task_learn("Register on Gumroad", history)

        # Should store as playbook knowledge
        rows = db.execute(
            "SELECT * FROM knowledge WHERE category = 'playbook'"
        )
        assert len(rows) >= 1
        import json
        content = json.loads(rows[0]["content"])
        assert "browse" in content["tool_sequence"]

    def test_executor_reads_custom_rules(self, executor, db, mock_llm):
        """Executor's _think() injects custom_rules from agent_config."""
        import json
        # Pre-populate custom rules
        db.execute(
            "INSERT INTO agent_config (agent_name, config_key, config_value) "
            "VALUES ('executor', 'custom_rules', ?)",
            (json.dumps("- Always try API before browser\n- Skip captcha-heavy sites"),),
        )

        mock_llm.chat_json.return_value = {
            "reasoning": "test", "tool": "done", "args": {"result": "ok"},
        }

        executor._think("Test task", "", 0)

        # Verify the custom rules were included in the prompt
        call_args = mock_llm.chat_json.call_args
        prompt = call_args[0][0][1]["content"]  # user message
        assert "DEPLOYED RULES" in prompt
        assert "API before browser" in prompt

    def test_executor_reads_temperature_config(self, executor, db, mock_llm):
        """Executor uses temperature from agent_config if set."""
        import json
        db.execute(
            "INSERT INTO agent_config (agent_name, config_key, config_value) "
            "VALUES ('executor', 'temperature', ?)",
            (json.dumps(0.7),),
        )

        mock_llm.chat_json.return_value = {
            "reasoning": "test", "tool": "done", "args": {"result": "ok"},
        }

        executor._think("Test task", "", 0)

        # Verify temperature was passed
        call_kwargs = mock_llm.chat_json.call_args
        assert call_kwargs[1]["temperature"] == 0.7

    def test_custom_rules_cap_at_10(self, executor, db, mock_llm):
        """Custom rules are capped at 10 to prevent bloat."""
        import json

        # Pre-populate with 9 rules
        existing = "\n".join(f"- Rule {i}" for i in range(9))
        db.execute(
            "INSERT INTO agent_config (agent_name, config_key, config_value) "
            "VALUES ('executor', 'custom_rules', ?)",
            (json.dumps(existing),),
        )

        # Apply two more — should cap at 10
        executor._apply_learned_action("New rule 10", set(), 0.5)
        executor._apply_learned_action("New rule 11", set(), 0.5)

        rows = db.execute(
            "SELECT config_value FROM agent_config "
            "WHERE agent_name = 'executor' AND config_key = 'custom_rules'"
        )
        rules = json.loads(rows[0]["config_value"])
        rule_count = len([r for r in rules.split("\n") if r.strip()])
        assert rule_count <= 10

    def test_base_agent_reads_agent_config(self, config, db, mock_llm):
        """BaseAgent.get_agent_config() reads values set by SelfImprover."""
        from monai.agents.self_improve import SELF_IMPROVE_SCHEMA
        with db.connect() as conn:
            conn.executescript(SELF_IMPROVE_SCHEMA)

        import json
        db.execute(
            "INSERT INTO agent_config (agent_name, config_key, config_value) "
            "VALUES ('test_agent', 'strategy_approach', ?)",
            (json.dumps("prefer_api_over_browser"),),
        )

        # Create a minimal agent that reads config
        from monai.agents.base import BaseAgent

        class TestAgent(BaseAgent):
            name = "test_agent"
            description = "test"
            def run(self, **kwargs): return {}
            def plan(self): return []

        agent = TestAgent(config, db, mock_llm)
        val = agent.get_agent_config("strategy_approach")
        assert val == "prefer_api_over_browser"

    def test_base_agent_config_in_system_prompt(self, config, db, mock_llm):
        """Deployed improvements appear in the system prompt."""
        from monai.agents.self_improve import SELF_IMPROVE_SCHEMA
        with db.connect() as conn:
            conn.executescript(SELF_IMPROVE_SCHEMA)

        import json
        db.execute(
            "INSERT INTO agent_config (agent_name, config_key, config_value) "
            "VALUES ('test_agent', 'strategy_registration', ?)",
            (json.dumps("Always verify email before proceeding"),),
        )

        from monai.agents.base import BaseAgent

        class TestAgent(BaseAgent):
            name = "test_agent"
            description = "test"
            def run(self, **kwargs): return {}
            def plan(self): return []

        agent = TestAgent(config, db, mock_llm)
        prompt = agent._build_system_prompt()
        assert "DEPLOYED IMPROVEMENTS" in prompt
        assert "verify email" in prompt

    def test_post_task_no_learn_on_success(self, executor, db, mock_llm):
        """Tasks with <30% failure rate don't trigger deep analysis."""
        history = [
            {"step": 1, "tool": "browse", "args": {}, "result": "loaded"},
            {"step": 2, "tool": "click", "args": {}, "result": "clicked"},
            {"step": 3, "tool": "done", "args": {}, "result": "done"},
        ]

        executor._post_task_learn("Easy task", history)
        # Should NOT call LLM for lesson extraction
        mock_llm.quick.assert_not_called()

    def test_domain_block_stored_in_executor_config(self, executor, db, mock_llm):
        """Learned actions are stored as executor custom rules in agent_config.

        Domain blocks are NOT stored in the knowledge table — they are
        transient (proxy/Tor circuit rotation changes reachability) and
        permanently blocking domains caused the executor to preemptively
        fail without even trying. Instead, rules go to agent_config.
        """
        executor._apply_learned_action(
            "Skip domain blocked.example.com — always returns 403",
            {"blocked.example.com"},
            0.8,
        )

        rows = db.execute(
            "SELECT config_value FROM agent_config "
            "WHERE agent_name = 'executor' AND config_key = 'custom_rules'"
        )
        assert len(rows) >= 1
        assert "blocked.example.com" in rows[0]["config_value"]


class TestIdentityManagerValidation:
    """Test that IdentityManager uses NameValidator."""

    def test_generate_identity_uses_validator(self, config, db, mock_llm):
        """_generate_identity with validate=True uses NameValidator."""
        from monai.agents.identity import IdentityManager

        # Mock the validator's generate_and_validate
        with patch("monai.agents.name_validator.NameValidator") as MockValidator:
            mock_v = MagicMock()
            mock_v.generate_and_validate.return_value = (
                {"name": "ValidatedCorp", "tagline": "T", "description": "D",
                 "preferred_username": "validcorp", "business_type": "B"},
                FullValidation(name="ValidatedCorp", overall_viable=True),
            )
            MockValidator.return_value = mock_v

            mgr = IdentityManager(config, db, mock_llm)
            # _ensure_base_identity also calls _generate_identity, so reset mock
            mock_v.generate_and_validate.reset_mock()

            identity = mgr._generate_identity(validate=True)

            assert identity["name"] == "ValidatedCorp"
            mock_v.generate_and_validate.assert_called_once()

    def test_generate_identity_fallback_on_validation_error(self, config, db, mock_llm):
        """Falls back to unvalidated generation if validator fails."""
        from monai.agents.identity import IdentityManager

        with patch("monai.agents.name_validator.NameValidator") as MockValidator:
            MockValidator.return_value.generate_and_validate.side_effect = Exception("Network error")

            mock_llm.quick_json.return_value = {
                "name": "FallbackCorp", "tagline": "T", "description": "D",
                "preferred_username": "fallback", "business_type": "B",
            }

            mgr = IdentityManager(config, db, mock_llm)
            identity = mgr._generate_identity(validate=True)

            assert identity["name"] == "FallbackCorp"

    def test_generate_identity_no_validation(self, config, db, mock_llm):
        """validate=False skips NameValidator entirely."""
        from monai.agents.identity import IdentityManager

        mock_llm.quick_json.return_value = {
            "name": "QuickCorp", "tagline": "T", "description": "D",
            "preferred_username": "quick", "business_type": "B",
        }

        mgr = IdentityManager(config, db, mock_llm)
        identity = mgr._generate_identity(validate=False)

        assert identity["name"] == "QuickCorp"
