"""Integration test for the full orchestrator cycle.

Tests the entire orchestrator run() method with mocked LLM and external services,
verifying that all phases execute in order and data flows correctly.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from monai.agents.orchestrator import Orchestrator
from monai.config import Config, LLMConfig, RiskConfig, TelegramConfig, PrivacyConfig
from monai.db.database import Database


@pytest.fixture
def integration_config(tmp_path):
    """Config for integration tests — all external services disabled."""
    return Config(
        llm=LLMConfig(model="gpt-4o-mini", model_mini="gpt-4o-mini", api_key="test-key"),
        risk=RiskConfig(
            max_strategy_allocation_pct=30.0,
            min_active_strategies=1,
            stop_loss_pct=15.0,
        ),
        telegram=TelegramConfig(enabled=False),
        privacy=PrivacyConfig(proxy_type="none", verify_anonymity=False),
        initial_capital=500.0,
        currency="USD",
        data_dir=tmp_path,
    )


@pytest.fixture
def integration_db(tmp_path):
    """Fresh database for integration tests with pre-seeded identity."""
    db = Database(db_path=tmp_path / "integration.db")
    # Pre-seed identity to avoid DNS lookups during IdentityManager init
    import json
    db.execute(
        "CREATE TABLE IF NOT EXISTS identities ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  type TEXT NOT NULL, platform TEXT NOT NULL,"
        "  identifier TEXT NOT NULL, credentials TEXT,"
        "  status TEXT DEFAULT 'active', metadata TEXT,"
        "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        "  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    db.execute_insert(
        "INSERT INTO identities (type, platform, identifier, metadata) "
        "VALUES ('agent_identity', 'self', 'TestCo Digital', ?)",
        (json.dumps({"name": "TestCo Digital", "tagline": "Test tagline",
                     "description": "Test company", "style": "professional"}),),
    )
    return db


@pytest.fixture
def mock_llm_responses():
    """Standard LLM mock responses for a full cycle."""
    return {
        "plan": {
            "actions": [
                {"action": "discover_opportunities", "priority": 1,
                 "reason": "Need to find revenue", "delegate_to_subagent": False},
            ]
        },
        "discover": {
            "opportunities": [
                {"name": "test_blog", "category": "content",
                 "description": "Test blog", "how_to_start": "Start writing",
                 "estimated_monthly_revenue": 100.0, "startup_cost": 5.0,
                 "risk_level": "low", "time_to_first_revenue_days": 30,
                 "platforms_needed": ["wordpress"], "can_automate": True},
            ]
        },
        "legal": {
            "status": "approved", "risk_level": "low",
            "blockers_count": 0, "recommendations": [],
        },
        "default_json": {
            "result": "mocked", "steps": ["research"],
            # Identity generation expects these fields
            "name": "TestCo Digital", "tagline": "Test tagline",
            "description": "Test company", "style": "professional",
            "industry_focus": ["technology"], "tone": "professional",
            # Research/analysis expects these
            "opportunities": [], "programs": [], "keywords": [],
            "briefs": [], "trends": [], "findings": "",
            "recommended_action": "monitor", "confidence": 0.5,
            "viable": False, "competition_level": "high",
        },
        "default_text": "mocked response",
    }


def _create_mock_llm(config, responses):
    """Create a mock LLM that returns different responses based on prompt content."""
    llm = MagicMock()
    llm.config = config
    llm.caller = "orchestrator"

    def smart_chat_json(messages, **kwargs):
        prompt = str(messages)
        if "plan your next cycle" in prompt.lower():
            return responses["plan"]
        if "brainstorm" in prompt.lower() and "opportunity" in prompt.lower():
            return responses["discover"]
        if "legal" in prompt.lower() or "compliance" in prompt.lower():
            return responses["legal"]
        return responses["default_json"]

    def smart_quick_json(prompt, **kwargs):
        if "plan your next cycle" in prompt.lower():
            return responses["plan"]
        if "brainstorm" in prompt.lower():
            return responses["discover"]
        return responses["default_json"]

    llm.chat_json.side_effect = smart_chat_json
    llm.quick_json.side_effect = smart_quick_json
    llm.chat.return_value = responses["default_text"]
    llm.quick.return_value = responses["default_text"]

    return llm


class TestOrchestratorIntegration:
    """Test the full orchestrator cycle end-to-end."""

    def test_orchestrator_initializes(self, integration_config, integration_db,
                                      mock_llm_responses):
        """Orchestrator should initialize all subsystems without errors."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orchestrator = Orchestrator(integration_config, integration_db, llm)

        assert orchestrator.name == "orchestrator"
        assert orchestrator.commercialista is not None
        assert orchestrator.fact_checker is not None
        assert orchestrator.workflow_engine is not None
        assert orchestrator.risk is not None

    def test_fact_checker_registered_with_workflow_engine(
            self, integration_config, integration_db, mock_llm_responses):
        """FactChecker must be registered with the workflow engine."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orchestrator = Orchestrator(integration_config, integration_db, llm)

        # fact_checker should be in the workflow engine's agent registry
        assert "fact_checker" in orchestrator.workflow_engine._agents
        assert orchestrator.workflow_engine._agents["fact_checker"] is orchestrator.fact_checker

    def test_full_cycle_executes(self, integration_config, integration_db,
                                 mock_llm_responses):
        """A full orchestrator cycle should complete without crashing."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orchestrator = Orchestrator(integration_config, integration_db, llm)

        # Replace telegram with a mock to avoid property patching issues
        mock_telegram = MagicMock()
        mock_telegram.has_token = False
        mock_telegram.is_configured = False
        orchestrator.telegram = mock_telegram

        with patch.object(orchestrator.payment_manager, 'run_sweep_cycle',
                         return_value={"flow": "none", "status": "no_config"}), \
             patch.object(orchestrator.payment_manager, 'health_check',
                         return_value={"status": "ok"}), \
             patch.object(orchestrator.payment_manager, 'get_status',
                         return_value={"active_providers": 0}):

            result = orchestrator.run()

        assert result is not None
        assert "budget" in result
        assert "health" in result
        assert result["budget"]["balance"] > 0

    def test_cycle_tracks_budget(self, integration_config, integration_db,
                                 mock_llm_responses):
        """Budget should be tracked across the cycle."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orchestrator = Orchestrator(integration_config, integration_db, llm)

        budget_before = orchestrator.commercialista.get_budget()
        assert budget_before["initial"] == 500.0
        assert budget_before["balance"] == 500.0

    def test_cycle_phases_execute_in_order(self, integration_config, integration_db,
                                           mock_llm_responses):
        """Verify that cycle phases execute in the expected order."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orchestrator = Orchestrator(integration_config, integration_db, llm)
        logged_actions = []

        mock_telegram = MagicMock()
        mock_telegram.has_token = False
        mock_telegram.is_configured = False
        orchestrator.telegram = mock_telegram

        original_log = orchestrator.log_action

        def tracking_log(action, details="", result=""):
            logged_actions.append(action)
            original_log(action, details, result)

        orchestrator.log_action = tracking_log

        with patch.object(orchestrator.payment_manager, 'run_sweep_cycle',
                         return_value={"flow": "none"}), \
             patch.object(orchestrator.payment_manager, 'health_check',
                         return_value={"status": "ok"}), \
             patch.object(orchestrator.payment_manager, 'get_status',
                         return_value={"active_providers": 0}):

            orchestrator.run()

        # Verify key phases happened in order
        assert "cycle_start" in logged_actions
        assert "budget_check" in logged_actions
        assert "health_check" in logged_actions

        # cycle_start should be before budget_check
        start_idx = logged_actions.index("cycle_start")
        budget_idx = logged_actions.index("budget_check")
        health_idx = logged_actions.index("health_check")
        assert start_idx < budget_idx < health_idx

    def test_strategy_registration(self, integration_config, integration_db,
                                   mock_llm_responses):
        """Strategy agents should be registerable and tracked."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orchestrator = Orchestrator(integration_config, integration_db, llm)

        # Create a mock strategy agent
        mock_agent = MagicMock()
        mock_agent.name = "test_strategy"
        mock_agent.run.return_value = {"status": "ok"}

        orchestrator.register_strategy(mock_agent)

        assert "test_strategy" in orchestrator._strategy_agents
        assert "test_strategy" in orchestrator.workflow_engine._agents

    def test_budget_exhausted_doesnt_crash(self, integration_config, integration_db,
                                           mock_llm_responses):
        """Even with zero budget, the orchestrator should gracefully handle the cycle."""
        # Set initial capital to 0
        integration_config.initial_capital = 0.0
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orchestrator = Orchestrator(integration_config, integration_db, llm)

        mock_telegram = MagicMock()
        mock_telegram.has_token = False
        mock_telegram.is_configured = False
        orchestrator.telegram = mock_telegram

        with patch.object(orchestrator.payment_manager, 'run_sweep_cycle',
                         return_value={"flow": "none"}), \
             patch.object(orchestrator.payment_manager, 'health_check',
                         return_value={"status": "ok"}), \
             patch.object(orchestrator.payment_manager, 'get_status',
                         return_value={"active_providers": 0}):

            result = orchestrator.run()

        # Should still complete (zero-cost operations allowed)
        assert result is not None
        assert result["budget"]["balance"] <= 0


class TestStrategyLifecycle:
    """Test strategy state machine enforcement."""

    def test_valid_transitions(self, db):
        from monai.business.strategy_lifecycle import StrategyLifecycle

        lifecycle = StrategyLifecycle(db)

        # Create a strategy
        strategy_id = db.execute_insert(
            "INSERT INTO strategies (name, category, description, status) "
            "VALUES ('test', 'test', 'test', 'pending')"
        )

        # pending → active
        result = lifecycle.activate(strategy_id, "Starting")
        assert result["new_status"] == "active"

        # active → paused
        result = lifecycle.pause(strategy_id, "Risk limit")
        assert result["new_status"] == "paused"

        # paused → active
        result = lifecycle.resume(strategy_id, "Resuming")
        assert result["new_status"] == "active"

        # active → stopped
        result = lifecycle.stop(strategy_id, "Shutting down")
        assert result["new_status"] == "stopped"

    def test_invalid_transitions(self, db):
        from monai.business.strategy_lifecycle import (
            StrategyLifecycle, InvalidTransitionError,
        )

        lifecycle = StrategyLifecycle(db)

        strategy_id = db.execute_insert(
            "INSERT INTO strategies (name, category, description, status) "
            "VALUES ('test2', 'test', 'test', 'stopped')"
        )

        # stopped → active should fail
        with pytest.raises(InvalidTransitionError):
            lifecycle.activate(strategy_id)

        # stopped → paused should fail
        with pytest.raises(InvalidTransitionError):
            lifecycle.pause(strategy_id)

    def test_is_runnable(self, db):
        from monai.business.strategy_lifecycle import StrategyLifecycle

        lifecycle = StrategyLifecycle(db)

        active_id = db.execute_insert(
            "INSERT INTO strategies (name, category, description, status) "
            "VALUES ('active_s', 'test', 'test', 'active')"
        )
        paused_id = db.execute_insert(
            "INSERT INTO strategies (name, category, description, status) "
            "VALUES ('paused_s', 'test', 'test', 'paused')"
        )

        assert lifecycle.is_runnable(active_id) is True
        assert lifecycle.is_runnable(paused_id) is False


class TestBrandSync:
    """Test that validate_strategies correctly syncs brand_social_accounts."""

    @pytest.fixture(autouse=True)
    def _setup_tables(self, db):
        """Ensure brand_social_accounts and identities tables exist."""
        db.execute(
            "CREATE TABLE IF NOT EXISTS brand_social_accounts ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  brand TEXT NOT NULL,"
            "  platform TEXT NOT NULL,"
            "  brand_voice TEXT DEFAULT '',"
            "  UNIQUE(brand, platform)"
            ")"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS identities ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  type TEXT NOT NULL,"
            "  platform TEXT NOT NULL,"
            "  identifier TEXT NOT NULL,"
            "  credentials TEXT,"
            "  status TEXT DEFAULT 'active',"
            "  metadata TEXT,"
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )

    def test_brands_registered_on_activation(self, db):
        """When validate_strategies activates a strategy, its brands must be created."""
        from monai.business.strategy_lifecycle import StrategyLifecycle

        lifecycle = StrategyLifecycle(db)

        # Create a pending strategy with email + payment assets (digital_products requires both)
        db.execute_insert(
            "INSERT INTO strategies (name, category, description, status) "
            "VALUES ('digital_products', 'products', 'test', 'pending')"
        )
        db.execute_insert(
            "INSERT INTO identities (type, platform, status, identifier) "
            "VALUES ('email', 'email', 'active', 'test@example.com')"
        )
        db.execute_insert(
            "INSERT INTO identities (type, platform, status, identifier) "
            "VALUES ('payment', 'gumroad', 'active', 'gumroad_account')"
        )

        result = lifecycle.validate_strategies()

        assert "digital_products" in result["activated"]
        assert result["brands_registered"] >= 1

        # Check that brand_social_accounts were actually created
        rows = db.execute(
            "SELECT * FROM brand_social_accounts WHERE brand = 'digital_products'"
        )
        assert len(rows) > 0
        platforms = {r["platform"] for r in rows}
        assert "twitter" in platforms
        assert "reddit" in platforms

    def test_brands_removed_on_pause(self, db):
        """When a strategy is paused, its brands must be removed."""
        from monai.business.strategy_lifecycle import StrategyLifecycle

        lifecycle = StrategyLifecycle(db)

        # Create an active strategy that requires payment (which we won't provide)
        db.execute_insert(
            "INSERT INTO strategies (name, category, description, status) "
            "VALUES ('digital_products', 'products', 'test', 'active')"
        )
        # Insert a brand that was registered when it was active
        db.execute_insert(
            "INSERT INTO brand_social_accounts (brand, platform) "
            "VALUES ('digital_products', 'twitter')"
        )
        # Provide email but NOT payment — strategy requires both
        db.execute_insert(
            "INSERT INTO identities (type, platform, status, identifier) "
            "VALUES ('email', 'email', 'active', 'test@example.com')"
        )

        result = lifecycle.validate_strategies()

        paused_names = [p["name"] for p in result["paused"]]
        assert "digital_products" in paused_names
        assert result["brands_removed"] >= 1

        # Brand should be gone
        rows = db.execute(
            "SELECT * FROM brand_social_accounts WHERE brand = 'digital_products'"
        )
        assert len(rows) == 0

    def test_active_brands_not_removed(self, db):
        """Brands for active strategies must not be touched."""
        from monai.business.strategy_lifecycle import StrategyLifecycle

        lifecycle = StrategyLifecycle(db)

        db.execute_insert(
            "INSERT INTO strategies (name, category, description, status) "
            "VALUES ('freelance_writing', 'services', 'test', 'active')"
        )
        db.execute_insert(
            "INSERT INTO brand_social_accounts (brand, platform) "
            "VALUES ('freelance_writing', 'twitter')"
        )
        db.execute_insert(
            "INSERT INTO identities (type, platform, status, identifier) "
            "VALUES ('email', 'email', 'active', 'test@example.com')"
        )

        result = lifecycle.validate_strategies()

        assert "freelance_writing" in result["already_active"]
        assert result["brands_removed"] == 0

        rows = db.execute(
            "SELECT * FROM brand_social_accounts WHERE brand = 'freelance_writing'"
        )
        assert len(rows) == 1

    def test_stale_brands_cleaned_from_previous_session(self, db):
        """Brands from a previous session for now-pending strategies get cleaned."""
        from monai.business.strategy_lifecycle import StrategyLifecycle

        lifecycle = StrategyLifecycle(db)

        # Strategy is pending (no email), but has stale brand from previous session
        db.execute_insert(
            "INSERT INTO strategies (name, category, description, status) "
            "VALUES ('micro_saas', 'products', 'test', 'pending')"
        )
        db.execute_insert(
            "INSERT INTO brand_social_accounts (brand, platform) "
            "VALUES ('micro_saas', 'twitter')"
        )
        db.execute_insert(
            "INSERT INTO brand_social_accounts (brand, platform) "
            "VALUES ('micro_saas', 'reddit')"
        )

        result = lifecycle.validate_strategies()

        assert result["brands_removed"] >= 1
        rows = db.execute(
            "SELECT * FROM brand_social_accounts WHERE brand = 'micro_saas'"
        )
        assert len(rows) == 0


class TestPlatformIntegration:
    """Test the platform integration base class."""

    def test_connection_management(self, config, db):
        from monai.integrations.base import PlatformIntegration, RateLimitConfig

        class TestPlatform(PlatformIntegration):
            platform_name = "test_platform"
            base_url = "https://api.test.com"

            def health_check(self):
                return {"status": "ok"}

        platform = TestPlatform(config, db)

        # Get connection for agent1
        conn1 = platform.get_connection("agent1", api_key="key1")
        assert conn1.agent_name == "agent1"
        assert conn1.platform == "test_platform"

        # Get connection for agent2
        conn2 = platform.get_connection("agent2", api_key="key2")
        assert conn2.agent_name == "agent2"

        # Same agent should return same connection
        conn1_again = platform.get_connection("agent1")
        assert conn1_again is conn1

        # Different agents should have different connections
        assert conn1 is not conn2

    def test_connection_registered_in_db(self, config, db):
        from monai.integrations.base import PlatformIntegration

        class TestPlatform(PlatformIntegration):
            platform_name = "test_db"
            base_url = "https://api.test.com"

            def health_check(self):
                return {"status": "ok"}

        platform = TestPlatform(config, db)
        platform.get_connection("test_agent")

        rows = db.execute(
            "SELECT * FROM platform_connections WHERE platform = 'test_db'"
        )
        assert len(rows) == 1
        assert rows[0]["agent_name"] == "test_agent"

    def test_rate_limit_enforcement(self, config, db):
        from monai.integrations.base import (
            PlatformConnection, RateLimitConfig, RateLimitError,
        )
        import time

        conn = PlatformConnection(
            platform="test",
            agent_name="test",
            base_url="https://api.test.com",
            rate_limit=RateLimitConfig(requests_per_minute=2),
        )

        # Simulate 2 recent requests
        now = time.time()
        conn._request_times = [now - 1, now - 0.5]

        with pytest.raises(RateLimitError):
            conn._check_rate_limit()


class TestPipelineFactCheck:
    """Test that fact_check steps are present in content pipelines."""

    def test_digital_products_pipeline_has_fact_check(self):
        from monai.workflows.pipelines import digital_products_pipeline
        pipeline = digital_products_pipeline()
        step_names = [s.name for s in pipeline.steps]
        assert "fact_check" in step_names

        # fact_check should be before humanize
        fc_idx = step_names.index("fact_check")
        hum_idx = step_names.index("humanize")
        assert fc_idx < hum_idx


class TestOrchestratorAuditIntegration:
    """Test that the orchestrator logs audit events during cycles."""

    def test_orchestrator_has_audit_trail(self, integration_config, integration_db,
                                          mock_llm_responses):
        """Orchestrator should initialize with an AuditTrail."""
        from monai.business.audit import AuditTrail
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orch = Orchestrator(integration_config, integration_db, llm)
        assert isinstance(orch.audit, AuditTrail)

    def test_cycle_logs_audit_events(self, integration_config, integration_db,
                                     mock_llm_responses):
        """A full cycle should produce audit trail entries."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orch = Orchestrator(integration_config, integration_db, llm)

        mock_telegram = MagicMock()
        mock_telegram.has_token = False
        mock_telegram.is_configured = False
        orch.telegram = mock_telegram

        with patch.object(orch.payment_manager, 'run_sweep_cycle',
                         return_value={"flow": "none"}), \
             patch.object(orch.payment_manager, 'health_check',
                         return_value={"status": "ok"}), \
             patch.object(orch.payment_manager, 'get_status',
                         return_value={"active_providers": 0}):
            orch.run()

        # Should have audit entries for cycle start and complete
        entries = orch.audit.get_recent(limit=100, agent_name="orchestrator")
        actions = [e["action"] for e in entries]
        assert "cycle_start" in actions
        assert "cycle_complete" in actions

    def test_budget_exhausted_logs_audit(self, integration_config, integration_db,
                                         mock_llm_responses):
        """Budget exhaustion should be logged to audit trail."""
        integration_config.initial_capital = 0.0
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orch = Orchestrator(integration_config, integration_db, llm)

        mock_telegram = MagicMock()
        mock_telegram.has_token = False
        mock_telegram.is_configured = False
        orch.telegram = mock_telegram

        with patch.object(orch.payment_manager, 'run_sweep_cycle',
                         return_value={"flow": "none"}), \
             patch.object(orch.payment_manager, 'health_check',
                         return_value={"status": "ok"}), \
             patch.object(orch.payment_manager, 'get_status',
                         return_value={"active_providers": 0}):
            orch.run()

        high_risk = orch.audit.get_high_risk_entries()
        actions = [e["action"] for e in high_risk]
        assert "budget_exhausted" in actions

    def test_direct_actions_audited(self, integration_config, integration_db,
                                    mock_llm_responses):
        """Direct actions from the planning phase should be audited."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orch = Orchestrator(integration_config, integration_db, llm)

        mock_telegram = MagicMock()
        mock_telegram.has_token = False
        mock_telegram.is_configured = False
        orch.telegram = mock_telegram

        with patch.object(orch.payment_manager, 'run_sweep_cycle',
                         return_value={"flow": "none"}), \
             patch.object(orch.payment_manager, 'health_check',
                         return_value={"status": "ok"}), \
             patch.object(orch.payment_manager, 'get_status',
                         return_value={"active_providers": 0}):
            orch.run()

        entries = orch.audit.get_recent(action_type="system")
        actions = [e["action"] for e in entries]
        assert "execute_action" in actions

    def test_audit_agent_summary(self, integration_config, integration_db,
                                  mock_llm_responses):
        """Agent summary should work after a cycle."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orch = Orchestrator(integration_config, integration_db, llm)

        mock_telegram = MagicMock()
        mock_telegram.has_token = False
        mock_telegram.is_configured = False
        orch.telegram = mock_telegram

        with patch.object(orch.payment_manager, 'run_sweep_cycle',
                         return_value={"flow": "none"}), \
             patch.object(orch.payment_manager, 'health_check',
                         return_value={"status": "ok"}), \
             patch.object(orch.payment_manager, 'get_status',
                         return_value={"active_providers": 0}):
            orch.run()

        summary = orch.audit.get_agent_summary()
        assert len(summary) > 0
        assert any(s["agent_name"] == "orchestrator" for s in summary)


class TestOrchestratorBackupIntegration:
    """Test that the orchestrator runs scheduled backups."""

    def test_orchestrator_has_backup_manager(self, integration_config, integration_db,
                                              mock_llm_responses):
        """Orchestrator should initialize with a BackupManager."""
        from monai.business.backup import BackupManager
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orch = Orchestrator(integration_config, integration_db, llm)
        assert isinstance(orch.backup_manager, BackupManager)

    def test_cycle_creates_backup(self, integration_config, integration_db,
                                   mock_llm_responses):
        """A full cycle should produce a database backup."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orch = Orchestrator(integration_config, integration_db, llm)

        mock_telegram = MagicMock()
        mock_telegram.has_token = False
        mock_telegram.is_configured = False
        orch.telegram = mock_telegram

        with patch.object(orch.payment_manager, 'run_sweep_cycle',
                         return_value={"flow": "none"}), \
             patch.object(orch.payment_manager, 'health_check',
                         return_value={"status": "ok"}), \
             patch.object(orch.payment_manager, 'get_status',
                         return_value={"active_providers": 0}):
            orch.run()

        backups = orch.backup_manager.list_backups("database")
        assert len(backups) >= 1
        assert backups[0]["size_bytes"] > 0

    def test_backup_audited(self, integration_config, integration_db,
                             mock_llm_responses):
        """Backup operations should be logged to audit trail."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orch = Orchestrator(integration_config, integration_db, llm)

        mock_telegram = MagicMock()
        mock_telegram.has_token = False
        mock_telegram.is_configured = False
        orch.telegram = mock_telegram

        with patch.object(orch.payment_manager, 'run_sweep_cycle',
                         return_value={"flow": "none"}), \
             patch.object(orch.payment_manager, 'health_check',
                         return_value={"status": "ok"}), \
             patch.object(orch.payment_manager, 'get_status',
                         return_value={"active_providers": 0}):
            orch.run()

        entries = orch.audit.get_recent(limit=100, agent_name="orchestrator")
        actions = [e["action"] for e in entries]
        assert "backup_database" in actions

    def test_lifecycle_bug_fixed(self, integration_config, integration_db,
                                  mock_llm_responses):
        """self.lifecycle → self.strategy_lifecycle bug should be fixed."""
        llm = _create_mock_llm(integration_config, mock_llm_responses)
        orch = Orchestrator(integration_config, integration_db, llm)
        # Should have strategy_lifecycle, not lifecycle
        assert hasattr(orch, "strategy_lifecycle")
        assert not hasattr(orch, "lifecycle")
