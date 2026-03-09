"""Tests for monai.db.database."""

import sqlite3

import pytest

from monai.db.database import Database


class TestDatabase:
    def test_creates_tables(self, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {r["name"] for r in rows}
        expected = {"strategies", "contacts", "projects", "transactions",
                    "messages", "performance", "agent_log"}
        assert expected.issubset(tables)

    def test_execute_insert_returns_rowid(self, db):
        rowid = db.execute_insert(
            "INSERT INTO strategies (name, category) VALUES (?, ?)",
            ("test_strategy", "freelance"),
        )
        assert rowid >= 1

    def test_execute_returns_rows(self, db):
        db.execute_insert(
            "INSERT INTO strategies (name, category) VALUES (?, ?)",
            ("test", "freelance"),
        )
        rows = db.execute("SELECT * FROM strategies WHERE name = ?", ("test",))
        assert len(rows) == 1
        assert rows[0]["name"] == "test"
        assert rows[0]["category"] == "freelance"
        assert rows[0]["status"] == "active"

    def test_execute_many(self, db):
        data = [
            ("s1", "freelance"),
            ("s2", "trading"),
            ("s3", "digital_products"),
        ]
        db.execute_many(
            "INSERT INTO strategies (name, category) VALUES (?, ?)", data
        )
        rows = db.execute("SELECT COUNT(*) as c FROM strategies")
        assert rows[0]["c"] == 3

    def test_rollback_on_error(self, db):
        db.execute_insert(
            "INSERT INTO strategies (name, category) VALUES (?, ?)",
            ("unique_name", "test"),
        )
        # UNIQUE constraint violation
        with pytest.raises(sqlite3.IntegrityError):
            db.execute_insert(
                "INSERT INTO strategies (name, category) VALUES (?, ?)",
                ("unique_name", "test"),
            )
        # Original row should still be there
        rows = db.execute("SELECT * FROM strategies WHERE name = 'unique_name'")
        assert len(rows) == 1

    def test_wal_mode(self, db):
        rows = db.execute("PRAGMA journal_mode")
        assert rows[0]["journal_mode"] == "wal"

    def test_foreign_keys_enabled(self, db):
        rows = db.execute("PRAGMA foreign_keys")
        assert rows[0]["foreign_keys"] == 1

    def test_context_manager(self, db):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO strategies (name, category) VALUES (?, ?)",
                ("ctx_test", "test"),
            )
        rows = db.execute("SELECT * FROM strategies WHERE name = 'ctx_test'")
        assert len(rows) == 1
