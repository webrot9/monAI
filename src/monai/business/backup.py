"""Automated backup & restore for database and configuration.

Features:
  - Full SQLite database backup (online, using sqlite3 backup API)
  - Config file backup
  - Timestamped backup files with rotation (keep last N)
  - Restore from any backup
  - Integrity verification after backup
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from monai.db.database import Database

logger = logging.getLogger(__name__)

DEFAULT_MAX_BACKUPS = 10


class BackupManager:
    """Manages database and config backups with rotation."""

    def __init__(self, db: Database, backup_dir: Path,
                 max_backups: int = DEFAULT_MAX_BACKUPS):
        """
        Args:
            db: Database instance to back up.
            backup_dir: Directory to store backups.
            max_backups: Maximum number of backups to retain.
        """
        self.db = db
        self.backup_dir = backup_dir
        self.max_backups = max_backups
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def backup_database(self) -> dict[str, Any]:
        """Create a timestamped backup of the SQLite database.

        Uses SQLite's online backup API for a consistent snapshot
        even while the database is in use.

        Returns:
            Backup metadata dict with path, size, timestamp.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"monai_db_{timestamp}.sqlite"

        # Use SQLite backup API for consistency
        with self.db.connect() as source_conn:
            dest = sqlite3.connect(str(backup_path))
            try:
                source_conn.backup(dest)
            finally:
                dest.close()

        size = backup_path.stat().st_size
        logger.info(f"Database backed up to {backup_path} ({size} bytes)")

        # Verify backup integrity
        verified = self._verify_backup(backup_path)

        result = {
            "path": str(backup_path),
            "timestamp": timestamp,
            "size_bytes": size,
            "verified": verified,
            "type": "database",
        }

        # Rotate old backups
        self._rotate_backups("monai_db_*.sqlite")

        return result

    def backup_config(self, config_path: Path) -> dict[str, Any]:
        """Back up a configuration file.

        Args:
            config_path: Path to config file to back up.

        Returns:
            Backup metadata dict.
        """
        if not config_path.exists():
            logger.warning(f"Config file not found: {config_path}")
            return {"error": f"File not found: {config_path}"}

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = config_path.suffix
        backup_path = self.backup_dir / f"config_{timestamp}{suffix}"
        shutil.copy2(config_path, backup_path)

        size = backup_path.stat().st_size
        logger.info(f"Config backed up to {backup_path} ({size} bytes)")

        result = {
            "path": str(backup_path),
            "timestamp": timestamp,
            "size_bytes": size,
            "type": "config",
            "original": str(config_path),
        }

        self._rotate_backups(f"config_*{suffix}")

        return result

    def backup_all(self, config_path: Path | None = None) -> dict[str, Any]:
        """Run all backups: database + optional config.

        Returns:
            Combined backup results.
        """
        results: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "database": self.backup_database(),
        }
        if config_path and config_path.exists():
            results["config"] = self.backup_config(config_path)

        return results

    def restore_database(self, backup_path: Path) -> dict[str, Any]:
        """Restore database from a backup file.

        CAUTION: This replaces the current database entirely.

        Args:
            backup_path: Path to the backup SQLite file.

        Returns:
            Restore result metadata.
        """
        if not backup_path.exists():
            return {"success": False, "error": f"Backup not found: {backup_path}"}

        # Verify backup before restoring
        if not self._verify_backup(backup_path):
            return {"success": False, "error": "Backup integrity check failed"}

        # Copy backup over current database
        db_path = self.db.db_path
        shutil.copy2(backup_path, db_path)

        logger.info(f"Database restored from {backup_path}")
        return {
            "success": True,
            "restored_from": str(backup_path),
            "db_path": str(db_path),
            "size_bytes": db_path.stat().st_size,
        }

    def restore_config(self, backup_path: Path,
                       target_path: Path) -> dict[str, Any]:
        """Restore a config file from backup.

        Args:
            backup_path: Path to the backup config file.
            target_path: Where to restore the config to.

        Returns:
            Restore result metadata.
        """
        if not backup_path.exists():
            return {"success": False, "error": f"Backup not found: {backup_path}"}

        shutil.copy2(backup_path, target_path)
        logger.info(f"Config restored from {backup_path} → {target_path}")
        return {
            "success": True,
            "restored_from": str(backup_path),
            "target": str(target_path),
        }

    # ── Listing & Rotation ─────────────────────────────────────

    def list_backups(self, backup_type: str | None = None) -> list[dict[str, Any]]:
        """List available backups, sorted by newest first.

        Args:
            backup_type: Filter by "database" or "config". None for all.
        """
        backups = []

        if backup_type in (None, "database"):
            for p in sorted(self.backup_dir.glob("monai_db_*.sqlite"), reverse=True):
                backups.append({
                    "path": str(p),
                    "filename": p.name,
                    "type": "database",
                    "size_bytes": p.stat().st_size,
                    "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
                })

        if backup_type in (None, "config"):
            for p in sorted(self.backup_dir.glob("config_*"), reverse=True):
                backups.append({
                    "path": str(p),
                    "filename": p.name,
                    "type": "config",
                    "size_bytes": p.stat().st_size,
                    "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
                })

        return backups

    def get_latest_backup(self, backup_type: str = "database") -> dict[str, Any] | None:
        """Get the most recent backup of a given type."""
        backups = self.list_backups(backup_type)
        return backups[0] if backups else None

    def _rotate_backups(self, pattern: str) -> int:
        """Remove old backups beyond max_backups. Returns count removed."""
        files = sorted(self.backup_dir.glob(pattern), reverse=True)
        removed = 0
        for old_file in files[self.max_backups:]:
            old_file.unlink()
            removed += 1
            logger.debug(f"Rotated old backup: {old_file}")
        return removed

    def _verify_backup(self, backup_path: Path) -> bool:
        """Verify a SQLite backup file's integrity."""
        try:
            conn = sqlite3.connect(str(backup_path))
            cursor = conn.execute("PRAGMA integrity_check")
            result = cursor.fetchone()
            conn.close()
            return result[0] == "ok"
        except Exception as e:
            logger.error(f"Backup verification failed: {e}")
            return False
