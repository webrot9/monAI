"""Shared fixtures for monAI tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from monai.config import Config, LLMConfig, RiskConfig, CommsConfig, TelegramConfig
from monai.db.database import Database


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temporary directory for test data."""
    return tmp_path


@pytest.fixture
def config(tmp_dir):
    """Config with test defaults — no real API keys."""
    return Config(
        llm=LLMConfig(model="gpt-4o-mini", model_mini="gpt-4o-mini", api_key="test-key"),
        risk=RiskConfig(),
        comms=CommsConfig(
            smtp_host="",
            from_name="TestAgent",
            from_email="test@example.com",
        ),
        telegram=TelegramConfig(
            bot_token="",
            creator_chat_id="",
            creator_username="TestCreator",
            enabled=False,
        ),
        initial_capital=500.0,
        currency="EUR",
        data_dir=tmp_dir,
    )


@pytest.fixture
def db(tmp_dir):
    """Fresh in-file SQLite database for each test."""
    db_path = tmp_dir / "test.db"
    return Database(db_path=db_path)


@pytest.fixture
def mock_llm(config):
    """Mock LLM that never calls OpenAI."""
    from monai.utils.llm import LLM

    llm = MagicMock(spec=LLM)
    llm.config = config
    llm.caller = "test"

    # Default returns
    llm.quick.return_value = "mocked response"
    llm.quick_json.return_value = {"result": "mocked"}
    llm.chat.return_value = "mocked chat response"
    llm.chat_json.return_value = {"result": "mocked"}

    return llm
