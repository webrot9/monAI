"""Tests for the Humanizer agent."""

import json
from unittest.mock import MagicMock

import pytest

from monai.config import Config
from monai.db.database import Database
from monai.agents.humanizer import Humanizer


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.quick.return_value = "test"
    llm.chat_json.return_value = {"human_score": 9, "remaining_issues": [], "needs_rewrite": False}
    llm.chat.return_value = "Rewritten content that sounds human."
    return llm


class TestHumanizer:
    def test_schema_created(self, config, db, mock_llm):
        h = Humanizer(config, db, mock_llm)
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='style_profiles'"
        )
        assert len(rows) == 1
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_quality'"
        )
        assert len(rows) == 1

    def test_ai_tells_list_exists(self, config, db, mock_llm):
        h = Humanizer(config, db, mock_llm)
        assert len(h.AI_TELLS) > 10

    def test_analyze_ai_tells_detects_patterns(self, config, db, mock_llm):
        h = Humanizer(config, db, mock_llm)
        content = "Certainly, it's important to note that we should leverage this opportunity."
        analysis = h._analyze_ai_tells(content)
        assert analysis["ai_tell_count"] > 0
        assert "certainly" in analysis["ai_tells_found"]
        assert "leverage" in analysis["ai_tells_found"]

    def test_analyze_ai_tells_clean_content(self, config, db, mock_llm):
        h = Humanizer(config, db, mock_llm)
        content = "The cat sat on the mat. It was a nice day."
        analysis = h._analyze_ai_tells(content)
        assert analysis["ai_tell_count"] == 0

    def test_analyze_sentence_variance(self, config, db, mock_llm):
        h = Humanizer(config, db, mock_llm)
        # Uniform sentence length (AI-like)
        uniform = "This is a test. That is a test. Here is a test. More is a test."
        analysis = h._analyze_ai_tells(uniform)
        assert analysis["low_variance"] is True

        # Varied sentence length (human-like)
        varied = "Short. This is a much longer sentence with more words in it. Yes. Another medium one here."
        analysis2 = h._analyze_ai_tells(varied)
        # Variance should be higher
        assert analysis2["sentence_length_variance"] > analysis["sentence_length_variance"]

    def test_get_or_create_default_profile(self, config, db, mock_llm):
        h = Humanizer(config, db, mock_llm)
        profile = h._get_or_create_profile("default")
        assert profile["name"] == "default"
        assert "voice_description" in profile

        # Second call returns same profile
        profile2 = h._get_or_create_profile("default")
        assert profile2["name"] == "default"

    def test_create_custom_profile(self, config, db, mock_llm):
        mock_llm.chat_json.return_value = {
            "characteristic_phrases": ["yo", "check this out"],
            "tone": "casual", "sentence_patterns": [], "vocabulary_preferences": []
        }
        h = Humanizer(config, db, mock_llm)
        profile = h.create_profile(
            "casual_blog", "Casual and fun blog voice",
            sample_text="Hey check this out, it's pretty cool stuff.",
            formality="casual", traits=["fun", "energetic"]
        )
        assert profile["name"] == "casual_blog"
        assert profile["formality_level"] == "casual"

    def test_humanize_calls_rewrite(self, config, db, mock_llm):
        h = Humanizer(config, db, mock_llm)
        result = h.humanize("Certainly, it's important to note this is comprehensive.")
        assert result == "Rewritten content that sounds human."
        assert mock_llm.chat.called

    def test_record_quality(self, config, db, mock_llm):
        h = Humanizer(config, db, mock_llm)
        h._record_quality(
            "original content", "final content", "default",
            {"ai_tells_found": ["certainly"], "ai_tell_count": 1}
        )
        rows = db.execute("SELECT * FROM content_quality")
        assert len(rows) == 1
        assert rows[0]["style_profile"] == "default"

    def test_quality_stats_empty(self, config, db, mock_llm):
        h = Humanizer(config, db, mock_llm)
        stats = h.get_quality_stats()
        assert stats["total"] == 0

    def test_quality_stats_with_data(self, config, db, mock_llm):
        h = Humanizer(config, db, mock_llm)
        h._record_quality("a", "b", "default", {"ai_tells_found": [], "ai_tell_count": 0})
        h._record_quality("c", "d", "default", {"ai_tells_found": ["x"], "ai_tell_count": 1})
        stats = h.get_quality_stats()
        assert stats["total"] == 2
