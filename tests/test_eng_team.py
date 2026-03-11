"""Tests for the Engineering Team (TechLead + Engineer agents)."""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from monai.config import Config
from monai.db.database import Database
from monai.agents.eng_team import EngineeringTeam, ENG_TEAM_SCHEMA
from monai.agents.eng_team.tech_lead import TechLead
from monai.agents.eng_team.engineer import Engineer


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.quick.return_value = "test response"
    llm.chat_json.return_value = {"bugs": []}
    llm.chat.return_value = "test"
    return llm


class TestEngineeringTeam:
    def test_schema_created(self, config, db, mock_llm):
        team = EngineeringTeam(config, db, mock_llm)
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bugs'"
        )
        assert len(rows) == 1

    def test_has_tech_lead(self, config, db, mock_llm):
        team = EngineeringTeam(config, db, mock_llm)
        assert team.tech_lead is not None
        assert team.tech_lead.name == "tech_lead"

    def test_has_engineers(self, config, db, mock_llm):
        team = EngineeringTeam(config, db, mock_llm, num_engineers=3)
        assert len(team.engineers) == 3
        assert team.engineers[0].name == "engineer_1"
        assert team.engineers[2].name == "engineer_3"

    def test_report_bug(self, config, db, mock_llm):
        team = EngineeringTeam(config, db, mock_llm)
        bug_id = team.report_bug(
            "Test bug", "Something broke", severity="high",
            source="test", source_agent="test_agent"
        )
        assert bug_id > 0

        rows = db.execute("SELECT * FROM bugs WHERE id = ?", (bug_id,))
        assert len(rows) == 1
        assert rows[0]["title"] == "Test bug"
        assert rows[0]["severity"] == "high"
        assert rows[0]["status"] == "open"

    def test_backlog_stats(self, config, db, mock_llm):
        team = EngineeringTeam(config, db, mock_llm)
        team.report_bug("Bug 1", "desc", severity="high")
        team.report_bug("Bug 2", "desc", severity="low")

        stats = team._get_backlog_stats()
        assert stats.get("open", 0) == 2

    def test_run_returns_results(self, config, db, mock_llm):
        mock_llm.chat_json.return_value = {"bugs": []}
        team = EngineeringTeam(config, db, mock_llm)
        result = team.run()
        assert "bugs_found" in result
        assert "bugs_assigned" in result
        assert "fixes_submitted" in result
        assert "fixes_reviewed" in result
        assert "backlog" in result


class TestTechLead:
    def test_name_and_description(self, config, db, mock_llm):
        lead = TechLead(config, db, mock_llm)
        assert lead.name == "tech_lead"
        assert "Engineering" in lead.description or "lead" in lead.description.lower()

    def test_scan_no_errors(self, config, db, mock_llm):
        # Initialize bugs table
        with db.connect() as conn:
            conn.executescript(ENG_TEAM_SCHEMA)
        lead = TechLead(config, db, mock_llm)
        bugs = lead.scan_for_bugs()
        assert bugs == []

    def test_scan_finds_errors(self, config, db, mock_llm):
        with db.connect() as conn:
            conn.executescript(ENG_TEAM_SCHEMA)
        lead = TechLead(config, db, mock_llm)

        # Insert a recent error
        db.execute_insert(
            "INSERT INTO agent_log (agent_name, action, details) VALUES (?, ?, ?)",
            ("test_agent", "error_occurred", "Something failed"),
        )

        mock_llm.chat_json.return_value = {
            "bugs": [
                {"title": "Test failure", "description": "Agent test_agent failed",
                 "severity": "high", "source_agent": "test_agent",
                 "root_cause_guess": "unknown"}
            ]
        }

        bugs = lead.scan_for_bugs()
        assert len(bugs) == 1
        assert bugs[0]["title"] == "Test failure"

    def test_assign_bugs(self, config, db, mock_llm):
        with db.connect() as conn:
            conn.executescript(ENG_TEAM_SCHEMA)
        lead = TechLead(config, db, mock_llm)

        # Create open bugs
        db.execute_insert(
            "INSERT INTO bugs (title, description, severity, source) VALUES (?, ?, ?, ?)",
            ("Bug 1", "desc 1", "high", "test"),
        )
        db.execute_insert(
            "INSERT INTO bugs (title, description, severity, source) VALUES (?, ?, ?, ?)",
            ("Bug 2", "desc 2", "medium", "test"),
        )

        engineers = [
            Engineer(config, db, mock_llm, name="eng_1"),
            Engineer(config, db, mock_llm, name="eng_2"),
        ]

        assignments = lead.assign_bugs(engineers)
        assert len(assignments) == 2
        assert assignments[0]["assigned_to"] == "eng_1"
        assert assignments[1]["assigned_to"] == "eng_2"

        # Verify status updated
        rows = db.execute("SELECT status FROM bugs WHERE id = 1")
        assert rows[0]["status"] == "assigned"

    def test_review_fixes(self, config, db, mock_llm):
        with db.connect() as conn:
            conn.executescript(ENG_TEAM_SCHEMA)
        lead = TechLead(config, db, mock_llm)

        db.execute_insert(
            "INSERT INTO bugs (title, description, severity, source, status, "
            "fix_description, fix_files, test_results) VALUES (?, ?, ?, ?, 'fix_ready', ?, ?, ?)",
            ("Bug 1", "desc", "medium", "test", "Fixed the issue",
             '["src/monai/agents/base.py"]',
             '{"status": "success", "passed": 5, "failed": 0}'),
        )

        mock_llm.chat_json.return_value = {
            "approved": True, "notes": "LGTM", "concerns": []
        }

        reviews = lead.review_fixes()
        assert len(reviews) == 1
        assert reviews[0]["approved"] is True


class TestEngineer:
    def test_name_configurable(self, config, db, mock_llm):
        eng = Engineer(config, db, mock_llm, name="engineer_42")
        assert eng.name == "engineer_42"

    def test_no_assigned_bugs(self, config, db, mock_llm):
        with db.connect() as conn:
            conn.executescript(ENG_TEAM_SCHEMA)
        eng = Engineer(config, db, mock_llm, name="eng_1")
        fixes = eng.work_on_assigned_bugs()
        assert fixes == []

    def test_works_on_assigned_bug(self, config, db, mock_llm):
        with db.connect() as conn:
            conn.executescript(ENG_TEAM_SCHEMA)
        eng = Engineer(config, db, mock_llm, name="eng_1")

        db.execute_insert(
            "INSERT INTO bugs (title, description, severity, source, assigned_to, status) "
            "VALUES (?, ?, ?, ?, ?, 'assigned')",
            ("Bug 1", "desc", "medium", "test", "eng_1"),
        )

        # Mock the analysis
        mock_llm.chat_json.return_value = {
            "root_cause": "test", "fix_approach": "patch it",
            "files_to_change": ["file.py"], "test_plan": "test it"
        }

        # Mock the coder (it's a lazy-loaded property, so set _coder directly)
        mock_coder = MagicMock()
        mock_coder.generate_module.return_value = {"status": "success"}
        eng._coder = mock_coder
        fixes = eng.work_on_assigned_bugs()

        assert len(fixes) == 1
        assert fixes[0]["title"] == "Bug 1"
        assert fixes[0]["fix_submitted"] is True
