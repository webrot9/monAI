"""Tests for monai.business.backup — automated backup & restore."""

from __future__ import annotations

from pathlib import Path

import pytest

from monai.business.backup import BackupManager
from monai.business.finance import GeneralLedger


@pytest.fixture
def backup_dir(tmp_dir):
    d = tmp_dir / "backups"
    d.mkdir()
    return d


@pytest.fixture
def manager(db, backup_dir):
    return BackupManager(db, backup_dir, max_backups=3)


@pytest.fixture
def ledger(db):
    return GeneralLedger(db)


class TestDatabaseBackup:
    def test_backup_creates_file(self, manager, backup_dir):
        """Backup creates a .sqlite file."""
        result = manager.backup_database()
        path = Path(result["path"])
        assert path.exists()
        assert path.suffix == ".sqlite"
        assert result["size_bytes"] > 0
        assert result["verified"] is True
        assert result["type"] == "database"

    def test_backup_contains_data(self, manager, ledger, db):
        """Backup contains actual database data."""
        ledger.record_revenue(
            amount=999.0, revenue_account="4000", cash_account="1010",
            description="Backup test",
        )
        result = manager.backup_database()

        # Verify data exists in backup
        import sqlite3
        conn = sqlite3.connect(result["path"])
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM gl_journal_entries WHERE description = 'Backup test'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1

    def test_backup_rotation(self, manager, backup_dir):
        """Old backups beyond max_backups are removed."""
        # Create 5 backup files manually (same-second timestamps collide)
        for i in range(5):
            path = backup_dir / f"monai_db_20260101_00000{i}.sqlite"
            path.write_bytes(b"")
        # Trigger rotation
        manager._rotate_backups("monai_db_*.sqlite")
        remaining = list(backup_dir.glob("monai_db_*.sqlite"))
        assert len(remaining) == 3

    def test_backup_verified(self, manager):
        """Backup passes integrity check."""
        result = manager.backup_database()
        assert result["verified"] is True


class TestDatabaseRestore:
    def test_restore_database(self, manager, ledger, db):
        """Restore replaces current database with backup."""
        # Create data and backup
        ledger.record_revenue(
            amount=500.0, revenue_account="4000", cash_account="1010",
            description="Before backup",
        )
        result = manager.backup_database()

        # Add more data after backup
        ledger.record_revenue(
            amount=200.0, revenue_account="4000", cash_account="1010",
            description="After backup",
        )

        # Restore
        restore_result = manager.restore_database(Path(result["path"]))
        assert restore_result["success"] is True

    def test_restore_nonexistent_fails(self, manager):
        """Restore from missing file returns error."""
        result = manager.restore_database(Path("/nonexistent/backup.sqlite"))
        assert result["success"] is False
        assert "not found" in result["error"]


class TestConfigBackup:
    def test_backup_config_file(self, manager, tmp_dir):
        """Config file backup creates copy."""
        config_file = tmp_dir / "config.json"
        config_file.write_text('{"key": "value"}')

        result = manager.backup_config(config_file)
        backup_path = Path(result["path"])
        assert backup_path.exists()
        assert backup_path.read_text() == '{"key": "value"}'
        assert result["type"] == "config"

    def test_backup_missing_config(self, manager):
        """Missing config file returns error."""
        result = manager.backup_config(Path("/nonexistent/config.json"))
        assert "error" in result

    def test_restore_config(self, manager, tmp_dir):
        """Config restore copies backup to target."""
        config_file = tmp_dir / "config.json"
        config_file.write_text('{"original": true}')

        result = manager.backup_config(config_file)

        # Modify original
        config_file.write_text('{"modified": true}')

        # Restore
        restore = manager.restore_config(
            Path(result["path"]), config_file
        )
        assert restore["success"] is True
        assert config_file.read_text() == '{"original": true}'

    def test_config_rotation(self, manager, backup_dir):
        """Config backups rotated beyond max_backups."""
        for i in range(5):
            path = backup_dir / f"config_20260101_00000{i}.json"
            path.write_text("{}")
        manager._rotate_backups("config_*.json")
        remaining = list(backup_dir.glob("config_*.json"))
        assert len(remaining) == 3


class TestBackupAll:
    def test_backup_all(self, manager, tmp_dir):
        """Backup all runs both database and config backup."""
        config_file = tmp_dir / "config.json"
        config_file.write_text("{}")

        result = manager.backup_all(config_file)
        assert "database" in result
        assert "config" in result
        assert result["database"]["verified"] is True

    def test_backup_all_without_config(self, manager):
        """Backup all works without config path."""
        result = manager.backup_all()
        assert "database" in result
        assert "config" not in result


class TestBackupListing:
    def test_list_empty(self, manager):
        """No backups returns empty list."""
        assert manager.list_backups() == []

    def test_list_by_type(self, manager, tmp_dir):
        """List filters by backup type."""
        manager.backup_database()
        config_file = tmp_dir / "test.json"
        config_file.write_text("{}")
        manager.backup_config(config_file)

        db_backups = manager.list_backups("database")
        cfg_backups = manager.list_backups("config")
        all_backups = manager.list_backups()

        assert len(db_backups) == 1
        assert len(cfg_backups) == 1
        assert len(all_backups) == 2

    def test_get_latest(self, manager):
        """Get latest returns most recent backup."""
        manager.backup_database()
        manager.backup_database()
        latest = manager.get_latest_backup()
        assert latest is not None
        assert latest["type"] == "database"

    def test_get_latest_none(self, manager):
        """No backups returns None."""
        assert manager.get_latest_backup() is None

    def test_backup_metadata(self, manager):
        """Backup listing includes full metadata."""
        manager.backup_database()
        backups = manager.list_backups("database")
        b = backups[0]
        assert "path" in b
        assert "filename" in b
        assert "size_bytes" in b
        assert "modified" in b
        assert b["type"] == "database"
