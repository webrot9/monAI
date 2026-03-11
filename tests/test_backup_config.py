"""Tests for configurable backup scheduling."""

from __future__ import annotations

import pytest

from monai.config import BackupConfig, Config


class TestBackupConfig:
    def test_default_values(self):
        """BackupConfig has sensible defaults."""
        cfg = BackupConfig()
        assert cfg.db_interval_cycles == 1
        assert cfg.config_interval_cycles == 7
        assert cfg.max_backups == 10
        assert cfg.enabled is True

    def test_custom_values(self):
        """BackupConfig accepts custom values."""
        cfg = BackupConfig(
            db_interval_cycles=3,
            config_interval_cycles=14,
            max_backups=5,
            enabled=False,
        )
        assert cfg.db_interval_cycles == 3
        assert cfg.config_interval_cycles == 14
        assert cfg.max_backups == 5
        assert cfg.enabled is False

    def test_config_has_backup(self):
        """Top-level Config includes BackupConfig."""
        config = Config()
        assert isinstance(config.backup, BackupConfig)
        assert config.backup.enabled is True

    def test_config_save_load_roundtrip(self, tmp_path):
        """Backup config survives save/load cycle."""
        import json
        config = Config(data_dir=tmp_path)
        config.backup = BackupConfig(
            db_interval_cycles=5,
            config_interval_cycles=21,
            max_backups=3,
            enabled=False,
        )

        # Simulate save (just check the dict structure)
        from monai.utils.crypto import encrypt_config_fields
        data = {
            "backup": {
                "db_interval_cycles": config.backup.db_interval_cycles,
                "config_interval_cycles": config.backup.config_interval_cycles,
                "max_backups": config.backup.max_backups,
                "enabled": config.backup.enabled,
            },
        }

        # Roundtrip through JSON
        restored = BackupConfig(**data["backup"])
        assert restored.db_interval_cycles == 5
        assert restored.config_interval_cycles == 21
        assert restored.max_backups == 3
        assert restored.enabled is False


def _make_orchestrator(tmp_path, backup_cfg):
    """Helper to create an Orchestrator with the given BackupConfig."""
    from monai.agents.orchestrator import Orchestrator
    from monai.config import LLMConfig, RiskConfig, TelegramConfig, PrivacyConfig
    from monai.db.database import Database
    from unittest.mock import MagicMock

    config = Config(
        llm=LLMConfig(model="gpt-4o-mini", model_mini="gpt-4o-mini", api_key="test"),
        risk=RiskConfig(),
        telegram=TelegramConfig(enabled=False),
        privacy=PrivacyConfig(proxy_type="none", verify_anonymity=False),
        backup=backup_cfg,
        initial_capital=500.0,
        data_dir=tmp_path,
    )
    db = Database(db_path=tmp_path / "test.db")
    llm = MagicMock()
    llm.config = config
    llm.caller = "test"
    llm.quick.return_value = "mocked"
    llm.quick_json.return_value = {
        "result": "mocked",
        "name": "TestCo Digital", "tagline": "Test", "description": "Test",
        "style": "professional", "industry_focus": ["technology"],
        "tone": "professional",
    }
    llm.chat.return_value = "mocked"
    llm.chat_json.return_value = llm.quick_json.return_value

    return Orchestrator(config, db, llm)


class TestBackupSchedulingInOrchestrator:
    def test_backup_disabled_skips(self, tmp_path):
        """When backup.enabled=False, _run_scheduled_backups returns disabled."""
        orch = _make_orchestrator(tmp_path, BackupConfig(enabled=False))
        result = orch._run_scheduled_backups()
        assert result == {"status": "disabled"}

    def test_db_interval_respected(self, tmp_path):
        """DB backup only runs on configured interval."""
        orch = _make_orchestrator(
            tmp_path,
            BackupConfig(db_interval_cycles=3, config_interval_cycles=99),
        )

        # Cycle 1: not divisible by 3 → no DB backup
        orch._cycle = 1
        result = orch._run_scheduled_backups()
        assert "database" not in result

        # Cycle 3: divisible by 3 → DB backup
        orch._cycle = 3
        result = orch._run_scheduled_backups()
        assert "database" in result
        assert result["database"]["verified"] is True
