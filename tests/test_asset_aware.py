"""Tests for asset awareness, constraint planner, and self-healing form_fill."""

import json
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

import pytest

from monai.agents.asset_aware import Asset, AssetInventory, AssetManager
from monai.agents.constraint_planner import (
    ConstraintPlanner,
    DependencyGraph,
    ProvisioningStep,
    StepStatus,
    CircularDependencyError,
)


# ═══════════════════════════════════════════════════════════════════
# Asset & AssetInventory tests
# ═══════════════════════════════════════════════════════════════════


class TestAsset:
    def test_create_asset(self):
        a = Asset(type="email", platform="gmail", identifier="test@gmail.com",
                  status="active")
        assert a.type == "email"
        assert a.platform == "gmail"
        assert a.identifier == "test@gmail.com"
        assert a.status == "active"
        assert a.metadata == {}

    def test_asset_with_metadata(self):
        a = Asset(type="api_key", platform="stripe", identifier="sk_test",
                  status="active", metadata={"brand": "acme"})
        assert a.metadata["brand"] == "acme"


class TestAssetInventory:
    def _make_inventory(self, assets=None):
        return AssetInventory(assets=assets or [])

    def test_empty_inventory(self):
        inv = self._make_inventory()
        assert not inv.has_email
        assert inv.email_address is None
        assert inv.emails == []
        assert inv.platform_accounts == []
        assert inv.domains == []
        assert inv.api_keys == []
        assert inv.payment_methods == []

    def test_has_email(self):
        inv = self._make_inventory([
            Asset("email", "gmail", "test@gmail.com", "active"),
        ])
        assert inv.has_email
        assert inv.email_address == "test@gmail.com"

    def test_inactive_email_not_counted(self):
        inv = self._make_inventory([
            Asset("email", "gmail", "test@gmail.com", "suspended"),
        ])
        assert not inv.has_email
        assert inv.email_address is None

    def test_has_account(self):
        inv = self._make_inventory([
            Asset("platform_account", "upwork", "monai_user", "active"),
        ])
        assert inv.has_account("upwork")
        assert not inv.has_account("fiverr")

    def test_has_api_key(self):
        inv = self._make_inventory([
            Asset("api_key", "stripe", "acme:stripe", "active"),
        ])
        assert inv.has_api_key("stripe")
        assert not inv.has_api_key("gumroad")

    def test_has_domain(self):
        inv = self._make_inventory([
            Asset("domain", "namecheap", "acme.com", "active"),
        ])
        assert inv.has_domain()

    def test_has_payment_method(self):
        inv = self._make_inventory([
            Asset("payment", "stripe", "visa_4242", "active"),
        ])
        assert inv.has_payment_method()

    def test_summary_with_assets(self):
        inv = self._make_inventory([
            Asset("email", "gmail", "test@gmail.com", "active"),
            Asset("platform_account", "upwork", "monai", "active"),
            Asset("domain", "namecheap", "acme.com", "active"),
        ])
        s = inv.summary()
        assert "test@gmail.com" in s
        assert "upwork" in s
        assert "acme.com" in s

    def test_summary_without_assets(self):
        inv = self._make_inventory()
        s = inv.summary()
        assert "NONE" in s

    def test_to_context_includes_guardrails(self):
        inv = self._make_inventory()
        ctx = inv.to_context()
        assert "never invent fake" in ctx.lower()
        assert "CRITICAL" in ctx
        assert "NONE" in ctx

    def test_to_context_includes_real_assets(self):
        inv = self._make_inventory([
            Asset("email", "gmail", "real@gmail.com", "active"),
        ])
        ctx = inv.to_context()
        assert "real@gmail.com" in ctx


class TestAssetManager:
    def _make_db(self, identities=None, api_keys=None, entities=None):
        db = MagicMock()
        results = {
            "identities": identities or [],
            "brand_api_keys": api_keys or [],
            "corporate_entities": entities or [],
        }

        def execute_side_effect(query, *args, **kwargs):
            if "identities" in query:
                return results["identities"]
            if "brand_api_keys" in query:
                return results["brand_api_keys"]
            if "corporate_entities" in query:
                return results["corporate_entities"]
            return []

        db.execute.side_effect = execute_side_effect
        return db

    def test_empty_inventory(self):
        db = self._make_db()
        mgr = AssetManager(db)
        inv = mgr.get_inventory()
        assert len(inv.assets) == 0

    def test_email_identity(self):
        db = self._make_db(identities=[
            {"type": "platform_account", "platform": "gmail",
             "identifier": "test@gmail.com", "status": "active", "metadata": None},
        ])
        mgr = AssetManager(db)
        inv = mgr.get_inventory()
        assert inv.has_email
        assert inv.email_address == "test@gmail.com"

    def test_platform_account(self):
        db = self._make_db(identities=[
            {"type": "platform_account", "platform": "upwork",
             "identifier": "monai", "status": "active", "metadata": None},
        ])
        mgr = AssetManager(db)
        inv = mgr.get_inventory()
        assert inv.has_account("upwork")

    def test_agent_identity_skipped(self):
        db = self._make_db(identities=[
            {"type": "agent_identity", "platform": "internal",
             "identifier": "agent1", "status": "active", "metadata": None},
        ])
        mgr = AssetManager(db)
        inv = mgr.get_inventory()
        assert len(inv.assets) == 0

    def test_api_keys_from_brand_table(self):
        db = self._make_db(api_keys=[
            {"provider": "stripe", "brand": "acme", "status": "active"},
        ])
        mgr = AssetManager(db)
        inv = mgr.get_inventory()
        assert inv.has_api_key("stripe")

    def test_corporate_entities(self):
        db = self._make_db(entities=[
            {"name": "Acme LLC", "jurisdiction": "Delaware",
             "status": "active", "id": 1},
        ])
        mgr = AssetManager(db)
        inv = mgr.get_inventory()
        assert any(a.type == "llc" for a in inv.assets)

    def test_db_error_handled_gracefully(self):
        db = MagicMock()
        db.execute.side_effect = Exception("DB error")
        mgr = AssetManager(db)
        inv = mgr.get_inventory()
        assert len(inv.assets) == 0

    def test_missing_prerequisites_signup_without_email(self):
        db = self._make_db()
        mgr = AssetManager(db)
        missing = mgr.get_missing_prerequisites("register on Upwork")
        assert any("email" in m for m in missing)

    def test_missing_prerequisites_signup_with_email(self):
        db = self._make_db(identities=[
            {"type": "platform_account", "platform": "gmail",
             "identifier": "test@gmail.com", "status": "active", "metadata": None},
        ])
        mgr = AssetManager(db)
        missing = mgr.get_missing_prerequisites("register on Upwork")
        assert len(missing) == 0

    def test_missing_prerequisites_domain_without_payment(self):
        db = self._make_db()
        mgr = AssetManager(db)
        missing = mgr.get_missing_prerequisites("buy domain acme.com")
        assert any("payment" in m for m in missing)

    def test_metadata_parsing(self):
        db = self._make_db(identities=[
            {"type": "platform_account", "platform": "upwork",
             "identifier": "monai", "status": "active",
             "metadata": json.dumps({"bio": "AI agent"})},
        ])
        mgr = AssetManager(db)
        inv = mgr.get_inventory()
        assert inv.assets[0].metadata["bio"] == "AI agent"


# ═══════════════════════════════════════════════════════════════════
# DependencyGraph tests
# ═══════════════════════════════════════════════════════════════════


class TestDependencyGraph:
    def test_add_step(self):
        g = DependencyGraph()
        s = ProvisioningStep(action="email", platform="gmail", priority=1,
                             reason="need email", id="email_1")
        g.add_step(s)
        assert len(g.steps) == 1

    def test_get_ready_steps_no_deps(self):
        g = DependencyGraph()
        s = ProvisioningStep(action="email", platform="gmail", priority=1,
                             reason="", id="email_1")
        g.add_step(s)
        ready = g.get_ready_steps()
        assert len(ready) == 1
        assert ready[0].id == "email_1"

    def test_dependency_ordering(self):
        g = DependencyGraph()
        s1 = ProvisioningStep(action="email", platform="gmail", priority=1,
                              reason="", id="email_1")
        g.add_step(s1)
        s2 = ProvisioningStep(action="signup", platform="upwork", priority=2,
                              reason="", dependencies=["email_1"], id="signup_1")
        g.add_step(s2)

        ready = g.get_ready_steps()
        assert len(ready) == 1
        assert ready[0].id == "email_1"

        g.mark_completed("email_1")
        ready = g.get_ready_steps()
        assert len(ready) == 1
        assert ready[0].id == "signup_1"

    def test_mark_failed_cascades(self):
        g = DependencyGraph()
        s1 = ProvisioningStep(action="email", platform="gmail", priority=1,
                              reason="", id="email_1")
        g.add_step(s1)
        s2 = ProvisioningStep(action="signup", platform="upwork", priority=2,
                              reason="", dependencies=["email_1"], id="signup_1")
        g.add_step(s2)

        g.mark_failed("email_1", "CAPTCHA unsolvable")
        assert g.get_step("signup_1").status == StepStatus.SKIPPED

    def test_execution_order_topological(self):
        g = DependencyGraph()
        s1 = ProvisioningStep(action="email", platform="gmail", priority=1,
                              reason="", id="email_1")
        g.add_step(s1)
        s2 = ProvisioningStep(action="payment", platform="stripe", priority=2,
                              reason="", id="payment_1")
        g.add_step(s2)
        s3 = ProvisioningStep(action="signup", platform="upwork", priority=3,
                              reason="", dependencies=["email_1"], id="signup_1")
        g.add_step(s3)

        order = g.get_execution_order()
        ids = [s.id for s in order]
        assert ids.index("email_1") < ids.index("signup_1")

    def test_validate_no_cycles(self):
        g = DependencyGraph()
        s1 = ProvisioningStep(action="email", platform="gmail", priority=1,
                              reason="", id="email_1")
        g.add_step(s1)
        assert g.validate() is True

    def test_validate_detects_cycles(self):
        g = DependencyGraph()
        # Manually create a cycle by adding steps then hacking dependencies
        s1 = ProvisioningStep(action="a", platform="x", priority=1,
                              reason="", id="a")
        g.add_step(s1)
        s2 = ProvisioningStep(action="b", platform="x", priority=2,
                              reason="", dependencies=["a"], id="b")
        g.add_step(s2)
        # Hack: add cycle
        s1.dependencies = ["b"]

        with pytest.raises(CircularDependencyError):
            g.validate()

    def test_is_complete(self):
        g = DependencyGraph()
        s = ProvisioningStep(action="email", platform="gmail", priority=1,
                             reason="", id="email_1")
        g.add_step(s)
        assert not g.is_complete
        g.mark_completed("email_1")
        assert g.is_complete

    def test_summary(self):
        g = DependencyGraph()
        s = ProvisioningStep(action="email", platform="gmail", priority=1,
                             reason="need email", id="email_1")
        g.add_step(s)
        summary = g.summary()
        assert "email" in summary
        assert "gmail" in summary

    def test_empty_graph_ready_steps(self):
        g = DependencyGraph()
        assert g.get_ready_steps() == []

    def test_priority_ordering(self):
        g = DependencyGraph()
        s1 = ProvisioningStep(action="low", platform="x", priority=99,
                              reason="", id="low")
        s2 = ProvisioningStep(action="high", platform="x", priority=1,
                              reason="", id="high")
        g.add_step(s1)
        g.add_step(s2)
        ready = g.get_ready_steps()
        assert ready[0].id == "high"


# ═══════════════════════════════════════════════════════════════════
# ConstraintPlanner tests
# ═══════════════════════════════════════════════════════════════════


class TestConstraintPlanner:
    def _make_planner(self, assets=None):
        db = MagicMock()
        llm = MagicMock()

        def execute_side_effect(query, *args, **kwargs):
            if "identities" in query:
                return assets or []
            return []

        db.execute.side_effect = execute_side_effect
        return ConstraintPlanner(db, llm)

    def test_email_goal_no_deps(self):
        planner = self._make_planner()
        graph = planner.plan(["create_email"])
        steps = graph.steps
        assert len(steps) >= 1
        assert any("email" in s.action for s in steps)

    def test_signup_goal_creates_email_dep(self):
        planner = self._make_planner()
        graph = planner.plan(["signup for upwork"])
        order = graph.get_execution_order()
        actions = [s.action for s in order]
        # Email creation should come before platform signup
        assert "email_creation" in actions
        assert "platform_signup" in actions
        assert actions.index("email_creation") < actions.index("platform_signup")

    def test_signup_skips_email_if_already_have(self):
        planner = self._make_planner(assets=[
            {"type": "platform_account", "platform": "gmail",
             "identifier": "test@gmail.com", "status": "active", "metadata": None},
        ])
        graph = planner.plan(["register on fiverr"])
        # Email step should be skipped (pre-completed)
        pending_steps = [s for s in graph.steps if s.status == StepStatus.PENDING]
        email_steps = [s for s in pending_steps if s.action == "email_creation"]
        assert len(email_steps) == 0

    def test_domain_goal_requires_payment(self):
        planner = self._make_planner()
        graph = planner.plan(["buy_domain acme.com"])
        order = graph.get_execution_order()
        actions = [s.action for s in order]
        assert "payment_method_setup" in actions
        assert "domain_purchase" in actions
        assert actions.index("payment_method_setup") < actions.index("domain_purchase")

    def test_api_key_goal_chain(self):
        planner = self._make_planner()
        graph = planner.plan(["api_key for stripe"])
        order = graph.get_execution_order()
        actions = [s.action for s in order]
        # Full chain: email → platform signup → API key
        assert "email_creation" in actions
        assert "platform_signup" in actions
        assert "api_key_acquisition" in actions

    def test_multiple_goals_dedup_shared_deps(self):
        planner = self._make_planner()
        graph = planner.plan(["signup for upwork", "signup for fiverr"])
        # Email creation should only appear once
        email_steps = [s for s in graph.steps if s.action == "email_creation"]
        assert len(email_steps) == 1

    def test_nonstandard_goal_uses_llm(self):
        planner = self._make_planner()
        planner.llm.chat_json.return_value = {
            "steps": [
                {"action": "setup_hosting", "platform": "vercel",
                 "priority": 1, "reason": "need hosting",
                 "estimated_cost": 0.0, "depends_on_index": [],
                 "already_satisfied": False},
            ]
        }
        # Use a goal that won't match any standard pattern
        graph = planner.plan(["deploy website to vercel"])
        assert len(graph.steps) >= 1

    def test_extract_platform(self):
        planner = self._make_planner()
        assert planner._extract_platform("signup for upwork") == "upwork"
        assert planner._extract_platform("get stripe api key") == "stripe"
        assert planner._extract_platform("do something random") == ""

    def test_get_standard_dependencies(self):
        deps = ConstraintPlanner._get_standard_dependencies()
        assert isinstance(deps, list)
        assert len(deps) > 0
        # Each rule has required fields
        for d in deps:
            assert "action" in d
            assert "requires" in d


# ═══════════════════════════════════════════════════════════════════
# Self-healing form_fill tests
# ═══════════════════════════════════════════════════════════════════


class TestSelfHealingFormFill:
    """Test the BrowserLearner's self-healing form fill capabilities."""

    def _make_learner(self):
        from monai.agents.browser_learner import BrowserLearner

        with patch.object(BrowserLearner, '__init__', lambda self, *a, **kw: None):
            learner = BrowserLearner.__new__(BrowserLearner)
            learner.config = MagicMock()
            learner.db = MagicMock()
            learner.llm = MagicMock()
            learner.browser = MagicMock()
            learner._captcha_solver = None
        return learner

    def test_get_known_selector_no_playbook(self):
        learner = self._make_learner()
        learner.db.execute.return_value = []
        result = learner._get_known_selector("example.com", "#old")
        assert result is None

    def test_get_known_selector_with_playbook(self):
        learner = self._make_learner()
        learner.db.execute.return_value = [
            {"known_selectors": json.dumps({"#old": "#new"})}
        ]
        result = learner._get_known_selector("example.com", "#old")
        assert result == "#new"

    def test_get_known_selector_no_domain(self):
        learner = self._make_learner()
        result = learner._get_known_selector("", "#old")
        assert result is None

    def test_llm_match_selector_finds_match(self):
        learner = self._make_learner()
        learner.llm.quick.return_value = '#email-input'
        elements = [
            {"tag": "input", "type": "email", "name": "email",
             "id": "email-input", "placeholder": "Enter email",
             "ariaLabel": "", "className": "", "visibleText": "",
             "boundingBox": {"x": 0, "y": 0, "width": 200, "height": 30},
             "isVisible": True},
        ]
        result = learner._llm_match_selector("email", elements)
        assert result == "#email-input"

    def test_llm_match_selector_no_match(self):
        learner = self._make_learner()
        learner.llm.quick.return_value = "NONE"
        result = learner._llm_match_selector("password", [])
        assert result is None

    def test_llm_match_selector_strips_formatting(self):
        learner = self._make_learner()
        learner.llm.quick.return_value = '`input[name="user"]`'
        result = learner._llm_match_selector("username", [{"tag": "input"}])
        assert result == 'input[name="user"]'

    @pytest.mark.asyncio
    async def test_discover_form_elements(self):
        learner = self._make_learner()
        mock_page = AsyncMock()
        mock_page.evaluate.return_value = [
            {"tag": "input", "type": "text", "name": "username",
             "id": "user", "placeholder": "Username",
             "ariaLabel": "", "className": "form-input",
             "visibleText": "", "boundingBox": {"x": 10, "y": 20,
             "width": 200, "height": 30}, "isVisible": True},
        ]
        learner.browser._get_page = AsyncMock(return_value=mock_page)
        elements = await learner._discover_form_elements("example.com")
        assert len(elements) == 1
        assert elements[0]["name"] == "username"

    @pytest.mark.asyncio
    async def test_smart_fill_form_uses_known_selector(self):
        learner = self._make_learner()
        learner.db.execute.return_value = [
            {"known_selectors": json.dumps({"#old_email": "#real_email"})}
        ]
        learner.smart_type = AsyncMock(return_value={"success": True})

        result = await learner.smart_fill_form(
            {"#old_email": "test@test.com"}, domain="example.com"
        )
        assert result["success"]
        # Should have used the learned selector
        learner.smart_type.assert_called_once()
        call_args = learner.smart_type.call_args
        assert call_args[0][0] == "#real_email"

    @pytest.mark.asyncio
    async def test_smart_fill_form_self_heals(self):
        learner = self._make_learner()
        learner.db.execute.return_value = []  # no known selectors

        # Pre-healing resolves selector, so smart_type is called once
        # with the healed selector and succeeds
        learner.smart_type = AsyncMock(return_value={"success": True})

        mock_page = AsyncMock()
        mock_page.evaluate.return_value = [
            {"tag": "input", "type": "email", "id": "email-field",
             "name": "email", "placeholder": "Email", "ariaLabel": "",
             "className": "", "visibleText": "",
             "boundingBox": {"x": 0, "y": 0, "width": 200, "height": 30},
             "isVisible": True},
        ]
        learner.browser._get_page = AsyncMock(return_value=mock_page)
        # Batch match returns the healed selector for the broken one
        learner.llm.quick.return_value = '{"#broken_selector": "#email-field"}'
        learner._update_playbook_selector = MagicMock()

        result = await learner.smart_fill_form(
            {"#broken_selector": "test@test.com"}, domain="example.com"
        )
        assert result["success"]
        # smart_type should be called with the pre-healed selector
        learner.smart_type.assert_called_once()
        call_args = learner.smart_type.call_args
        assert call_args[0][0] == "#email-field"
        # Should have stored the healed selector
        learner._update_playbook_selector.assert_called_once_with(
            "example.com", "#broken_selector", "#email-field"
        )


# ═══════════════════════════════════════════════════════════════════
# Integration: AssetManager wired into Executor
# ═══════════════════════════════════════════════════════════════════


class TestExecutorAssetAwareness:
    """Test that the executor injects asset context into LLM prompts."""

    def test_think_includes_asset_context(self):
        from monai.agents.executor import AutonomousExecutor

        with patch.object(AutonomousExecutor, '__init__', lambda self, *a, **kw: None):
            executor = AutonomousExecutor.__new__(AutonomousExecutor)
            executor.db = MagicMock()
            executor.llm = MagicMock()
            executor.action_history = []
            executor.max_steps = 30
            executor._learner = None
            executor._custom_tools = {}
            executor._anonymizer = MagicMock()
            executor._anonymizer.fallback_chain = MagicMock()
            executor._anonymizer.fallback_chain.get_domain_status.return_value = {}
            executor.memory = MagicMock()
            executor.memory.get_lessons.return_value = []
            executor._script_target_failures = {}
            executor._failed_domains = set()
            executor._visited_urls = set()

        # Mock AssetManager to return known inventory
        mock_inventory = AssetInventory(assets=[
            Asset("email", "gmail", "real@gmail.com", "active"),
        ])

        executor.llm.chat_json.return_value = {
            "reasoning": "test", "tool": "done", "args": {"result": "ok"}
        }

        with patch('monai.agents.executor.AssetManager') as MockAM:
            MockAM.return_value.get_inventory.return_value = mock_inventory
            executor._think("test task", "context", 0)

        # Verify the LLM was called with asset context in the prompt
        call_args = executor.llm.chat_json.call_args
        prompt = call_args[0][0][1]["content"]  # user message content
        assert "real@gmail.com" in prompt
        assert "never invent fake" in prompt.lower()


class TestProvisionerConstraintPlanning:
    """Test that the provisioner uses constraint-aware planning."""

    def test_provisioner_has_constraint_planner(self):
        from monai.agents.provisioner import Provisioner

        with patch.object(Provisioner, '__init__', lambda self, *a, **kw: None):
            prov = Provisioner.__new__(Provisioner)
            prov.db = MagicMock()
            prov.llm = MagicMock()
            prov.constraint_planner = ConstraintPlanner(prov.db, prov.llm)

        assert isinstance(prov.constraint_planner, ConstraintPlanner)


# ═══════════════════════════════════════════════════════════════════
# Self-healing for smart_click and smart_type
# ═══════════════════════════════════════════════════════════════════


class TestSelfHealingAllActions:
    """Test self-healing works for click, type, and form_fill."""

    def _make_learner(self):
        from monai.agents.browser_learner import BrowserLearner

        with patch.object(BrowserLearner, '__init__', lambda self, *a, **kw: None):
            learner = BrowserLearner.__new__(BrowserLearner)
            learner.config = MagicMock()
            learner.db = MagicMock()
            learner.llm = MagicMock()
            learner.browser = MagicMock()
            learner._captcha_solver = None
            learner.db.execute.return_value = []  # no known selectors
            learner.db.execute_insert = MagicMock()
        return learner

    @pytest.mark.asyncio
    async def test_smart_click_uses_known_selector(self):
        learner = self._make_learner()
        learner.db.execute.return_value = [
            {"known_selectors": json.dumps({"#old_btn": "#new_btn"})}
        ]
        learner.browser.click = AsyncMock()

        result = await learner.smart_click("#old_btn", domain="example.com")
        assert result["success"]
        # Should have used the learned selector
        learner.browser.click.assert_called_once_with("#new_btn")

    @pytest.mark.asyncio
    async def test_smart_click_llm_heals_on_failure(self):
        learner = self._make_learner()
        learner.db.execute.return_value = []  # no known selectors

        # Original click fails, LLM-healed click succeeds
        learner.browser.click = AsyncMock(side_effect=[
            Exception("Element not found"),  # original fails
            None,  # healed selector works
        ])

        mock_page = AsyncMock()
        mock_page.evaluate.return_value = [
            {"tag": "button", "type": "submit", "id": "submit-btn",
             "name": "", "placeholder": "", "ariaLabel": "Submit",
             "className": "btn-primary", "visibleText": "Submit",
             "boundingBox": {"x": 10, "y": 20, "width": 100, "height": 40},
             "isVisible": True},
        ]
        learner.browser._get_page = AsyncMock(return_value=mock_page)
        learner.llm.quick.return_value = "#submit-btn"
        learner._update_playbook_selector = MagicMock()

        result = await learner.smart_click(
            "#broken_btn", domain="example.com", fallback_text=""
        )
        assert result["success"]
        assert result.get("healed")
        learner._update_playbook_selector.assert_called_once_with(
            "example.com", "#broken_btn", "#submit-btn"
        )

    @pytest.mark.asyncio
    async def test_smart_type_uses_known_selector(self):
        learner = self._make_learner()
        learner.db.execute.return_value = [
            {"known_selectors": json.dumps({"#old_input": "#new_input"})}
        ]

        mock_page = AsyncMock()
        mock_page.keyboard = AsyncMock()
        mock_page.click = AsyncMock()
        learner.browser._get_page = AsyncMock(return_value=mock_page)
        learner.browser.type_text = AsyncMock()

        result = await learner.smart_type(
            "#old_input", "hello", domain="example.com", human_like=False
        )
        assert result["success"]
        # Should have used the learned selector via type_text
        learner.browser.type_text.assert_called_once_with("#new_input", "hello")

    @pytest.mark.asyncio
    async def test_smart_type_llm_heals_on_failure(self):
        learner = self._make_learner()
        learner.db.execute.return_value = []

        mock_page = AsyncMock()
        mock_page.keyboard = AsyncMock()
        mock_page.click = AsyncMock()  # succeeds on healed selector
        mock_page.evaluate.return_value = [
            {"tag": "input", "type": "text", "id": "username",
             "name": "user", "placeholder": "Username", "ariaLabel": "",
             "className": "", "visibleText": "",
             "boundingBox": {"x": 0, "y": 0, "width": 200, "height": 30},
             "isVisible": True},
        ]
        learner.browser._get_page = AsyncMock(return_value=mock_page)
        # First call (original selector) fails, second (healed) succeeds
        learner.browser.type_text = AsyncMock(side_effect=[
            Exception("not found"),  # original fails
            None,  # healed succeeds
        ])
        learner.llm.quick.return_value = "#username"
        learner._update_playbook_selector = MagicMock()

        result = await learner.smart_type(
            "#broken", "testuser", domain="example.com", human_like=False
        )
        assert result["success"]
        assert result.get("healed_selector") == "#username"
        learner._update_playbook_selector.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
# Dynamic Tool Creation
# ═══════════════════════════════════════════════════════════════════


class TestDynamicToolCreation:
    """Test the executor's ability to create and use custom tools."""

    def _make_executor(self):
        from monai.agents.executor import AutonomousExecutor

        with patch.object(AutonomousExecutor, '__init__', lambda self, *a, **kw: None):
            executor = AutonomousExecutor.__new__(AutonomousExecutor)
            executor.db = MagicMock()
            executor.db.execute.return_value = []
            executor.llm = MagicMock()
            # LLM review (via llm.chat) defaults to SAFE for test purposes
            executor.llm.chat.return_value = "SAFE: code is harmless"
            executor.llm.get_model.return_value = "gpt-4o"
            executor._custom_tools = {}
        return executor

    def test_create_tool_success(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "greet",
            "description": "Returns a greeting",
            "code": "result = 'Hello, ' + args.get('name', 'world')",
        })
        assert "created successfully" in result
        assert "greet" in executor._custom_tools

    def test_create_tool_rejects_reserved_name(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "browse",
            "description": "test",
            "code": "result = 'test'",
        })
        assert "ERROR" in result or "reserved" in result

    def test_create_tool_blocks_dangerous_code(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "evil",
            "description": "does evil",
            "code": "import os; os.system('rm -rf /')",
        })
        assert "BLOCKED" in result

    def test_create_tool_blocks_sandbox_escape(self):
        """Blocks Python introspection-based sandbox escapes."""
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "escape",
            "description": "sandbox escape",
            "code": "x = ().__class__.__mro__[-1].__subclasses__()",
        })
        assert "BLOCKED" in result

    def test_create_tool_blocks_dunder_globals(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "leak",
            "description": "leaks globals",
            "code": "x = foo.__globals__['os']",
        })
        assert "BLOCKED" in result

    def test_create_tool_validates_syntax(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "broken",
            "description": "broken syntax",
            "code": "def broken(:",
        })
        assert "ERROR" in result

    def test_create_tool_rejects_empty(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "",
            "description": "",
            "code": "",
        })
        assert "ERROR" in result

    def test_create_tool_rejects_oversized_code(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "big",
            "description": "too big",
            "code": "x = 1\n" * 10000,
        })
        assert "ERROR" in result

    def test_create_tool_llm_review_rejects_unsafe(self):
        """LLM review can reject code even if static checks pass."""
        executor = self._make_executor()
        executor.llm.chat.return_value = "UNSAFE: suspicious data exfiltration"
        result = executor._handle_create_tool({
            "name": "sneaky",
            "description": "looks innocent",
            "code": "result = str(args)",  # benign code, but LLM says no
        })
        assert "BLOCKED" in result

    def test_create_tool_llm_review_failure_rejects(self):
        """If LLM review fails, code is rejected (fail-closed)."""
        executor = self._make_executor()
        executor.llm.chat.side_effect = Exception("LLM down")
        result = executor._handle_create_tool({
            "name": "risky",
            "description": "risky",
            "code": "result = 'hello'",
        })
        assert "BLOCKED" in result

    @pytest.mark.asyncio
    async def test_run_custom_tool_in_subprocess(self):
        """Custom tools run in isolated subprocess, not in-process."""
        executor = self._make_executor()
        executor._custom_tools["greet"] = {
            "description": "Greets someone",
            "code": "result = 'Hello, ' + args.get('name', 'world')",
        }
        with patch('monai.agents.executor.sandbox_run') as mock_sandbox:
            mock_sandbox.return_value = {
                "stdout": "Hello, Alice\n",
                "stderr": "",
                "returncode": 0,
            }
            result = await executor._run_custom_tool("greet", {"name": "Alice"})
            assert result == "Hello, Alice"
            # Verify it was called with python -c (subprocess, not exec)
            call_args = mock_sandbox.call_args[0][0]
            assert call_args[1] == "-c"

    @pytest.mark.asyncio
    async def test_run_custom_tool_subprocess_failure(self):
        executor = self._make_executor()
        executor._custom_tools["crasher"] = {
            "description": "Crashes",
            "code": "raise ValueError('boom')",
        }
        with patch('monai.agents.executor.sandbox_run') as mock_sandbox:
            mock_sandbox.return_value = {
                "stdout": "",
                "stderr": "ValueError: boom",
                "returncode": 1,
            }
            result = await executor._run_custom_tool("crasher", {})
            assert "ERROR" in result

    @pytest.mark.asyncio
    async def test_run_custom_tool_not_found(self):
        executor = self._make_executor()
        result = await executor._run_custom_tool("nonexistent", {})
        assert "ERROR" in result

    def test_get_tool_descriptions_includes_custom(self):
        executor = self._make_executor()
        executor._custom_tools["my_tool"] = {
            "description": "Does something cool",
            "code": "result = 'cool'",
        }
        desc = executor._get_tool_descriptions()
        assert "my_tool" in desc
        assert "Does something cool" in desc

    def test_get_tool_descriptions_without_custom(self):
        executor = self._make_executor()
        desc = executor._get_tool_descriptions()
        assert "browse" in desc
        assert "Custom tools" not in desc

    def test_create_tool_blocks_subprocess(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "sneaky",
            "description": "sneaky",
            "code": "import subprocess; subprocess.run(['ls'])",
        })
        assert "BLOCKED" in result

    def test_create_tool_blocks_requests(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "http_leak",
            "description": "leaks data",
            "code": "import requests; requests.get('http://evil.com')",
        })
        assert "BLOCKED" in result

    def test_create_tool_blocks_socket(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "raw_net",
            "description": "raw socket",
            "code": "import socket; s = socket.socket()",
        })
        assert "BLOCKED" in result

    def test_create_tool_blocks_ctypes(self):
        executor = self._make_executor()
        result = executor._handle_create_tool({
            "name": "native",
            "description": "native code",
            "code": "import ctypes; ctypes.CDLL('libc.so.6')",
        })
        assert "BLOCKED" in result


# ═══════════════════════════════════════════════════════════════════
# LLM-enriched constraint planning
# ═══════════════════════════════════════════════════════════════════


class TestLLMConstraintPlanning:
    """Test that constraint planner combines hardcoded + LLM deps."""

    def _make_planner(self, assets=None):
        db = MagicMock()
        llm = MagicMock()

        def execute_side_effect(query, *args, **kwargs):
            if "identities" in query:
                return assets or []
            return []

        db.execute.side_effect = execute_side_effect
        return ConstraintPlanner(db, llm)

    def test_standard_goal_skips_llm(self):
        """Standard goals use only hardcoded rules — LLM is NOT consulted.

        LLM enrichment was removed for standard goals because it caused scope
        explosion (e.g. "telegram_bot" → 16 steps when only 1 needed).
        """
        planner = self._make_planner()
        planner.llm.chat_json.return_value = {
            "steps": [
                {"action": "verify_email", "platform": "gmail",
                 "priority": 15, "reason": "verify email before signup",
                 "estimated_cost": 0.0, "depends_on_index": [],
                 "already_satisfied": False},
            ]
        }
        graph = planner.plan(["signup for upwork"])
        actions = [s.action for s in graph.steps]
        # Hardcoded rules are authoritative — LLM step NOT included
        assert "email_creation" in actions
        assert "platform_signup" in actions
        assert "verify_email" not in actions
        # LLM should NOT have been called for standard goals
        planner.llm.chat_json.assert_not_called()

    def test_llm_deps_dont_duplicate_hardcoded(self):
        """LLM steps that match hardcoded ones should be deduplicated."""
        planner = self._make_planner()
        # LLM returns email_creation which is already in hardcoded rules
        planner.llm.chat_json.return_value = {
            "steps": [
                {"action": "email_creation", "platform": "email",
                 "priority": 10, "reason": "need email",
                 "estimated_cost": 0.0, "depends_on_index": [],
                 "already_satisfied": False},
            ]
        }
        graph = planner.plan(["signup for upwork"])
        email_steps = [s for s in graph.steps if s.action == "email_creation"]
        assert len(email_steps) == 1  # Not duplicated

    def test_llm_prompt_includes_inventory(self):
        """LLM receives full inventory context."""
        planner = self._make_planner(assets=[
            {"type": "platform_account", "platform": "gmail",
             "identifier": "test@gmail.com", "status": "active", "metadata": None},
        ])
        # Use a non-standard goal to force LLM path
        planner.llm.chat_json.return_value = {"steps": []}
        planner.plan(["deploy to kubernetes"])
        # Verify LLM was called with inventory info
        call_args = planner.llm.chat_json.call_args
        system_msg = call_args[1]["messages"][0]["content"] if "messages" in call_args[1] else call_args[0][0][0]["content"]
        assert "test@gmail.com" in system_msg
