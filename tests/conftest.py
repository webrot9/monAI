"""Shared fixtures for monAI tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from monai.config import Config, LLMConfig, RiskConfig, CommsConfig, TelegramConfig
from monai.db.database import Database


@pytest.fixture(autouse=True)
def _no_network_in_tests(request):
    """Prevent real network calls during tests.

    Patches:
    1. NameValidator.generate_and_validate — does DNS lookups, HTTP to Google
       DNS API, GitHub/Twitter profile checks, Google web searches.
    2. ProxyFallbackChain._get_free_proxy — scrapes free proxy lists and
       waits up to 30s for results.

    Tests marked with @pytest.mark.real_validator skip the validator patch.
    """
    from monai.agents.name_validator import NameValidator, FullValidation
    from monai.utils.privacy import ProxyFallbackChain

    def fake_generate_and_validate(self, **kwargs):
        identity = {
            "name": "TestCo Digital",
            "tagline": "AI-powered digital services",
            "description": "Test company for automated testing",
            "preferred_username": "testco_digital",
            "business_type": "digital_services",
        }
        validation = FullValidation(
            name="TestCo Digital",
            checks=[],
            overall_viable=True,
            blockers=[],
            warnings=[],
        )
        return identity, validation

    def _fast_get_free_proxy(self, wait: bool = False):
        """Skip network scraping. If a mock pool was set, use it; else return None."""
        if self._free_proxy_pool is not None:
            return self._free_proxy_pool.get_proxy(wait=wait)
        return None

    patches = [patch.object(ProxyFallbackChain, "_get_free_proxy", _fast_get_free_proxy)]
    if "real_validator" not in request.keywords:
        patches.append(patch.object(NameValidator, "generate_and_validate", fake_generate_and_validate))

    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


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
