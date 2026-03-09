"""Tests for monai.config."""

import json
import os
from pathlib import Path

import pytest

from monai.config import Config, LLMConfig, RiskConfig, CommsConfig


class TestLLMConfig:
    def test_defaults(self):
        cfg = LLMConfig()
        assert cfg.model == "gpt-4o"
        assert cfg.model_mini == "gpt-4o-mini"
        assert cfg.max_tokens == 4096
        assert cfg.temperature == 0.7

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        cfg = LLMConfig()
        assert cfg.api_key == "sk-test-123"

    def test_explicit_api_key_overrides_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        cfg = LLMConfig(api_key="sk-explicit")
        assert cfg.api_key == "sk-explicit"


class TestRiskConfig:
    def test_defaults(self):
        cfg = RiskConfig()
        assert cfg.max_strategy_allocation_pct == 30.0
        assert cfg.min_active_strategies == 3
        assert cfg.stop_loss_pct == 15.0
        assert cfg.min_roi_threshold == 1.0
        assert cfg.review_period_days == 30


class TestConfig:
    def test_defaults(self):
        cfg = Config()
        assert cfg.initial_capital == 500.0
        assert cfg.currency == "EUR"
        assert isinstance(cfg.llm, LLMConfig)
        assert isinstance(cfg.risk, RiskConfig)
        assert isinstance(cfg.comms, CommsConfig)

    def test_save_and_load(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_dir = tmp_path

        monkeypatch.setattr("monai.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("monai.config.CONFIG_DIR", config_dir)

        cfg = Config(
            llm=LLMConfig(model="gpt-4.1", api_key="test"),
            initial_capital=1000.0,
            currency="USD",
        )
        cfg.save()

        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert data["initial_capital"] == 1000.0
        assert data["currency"] == "USD"
        assert data["llm"]["model"] == "gpt-4.1"

        loaded = Config.load()
        assert loaded.initial_capital == 1000.0
        assert loaded.currency == "USD"
        assert loaded.llm.model == "gpt-4.1"

    def test_load_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("monai.config.CONFIG_FILE", tmp_path / "nonexistent.json")
        cfg = Config.load()
        assert cfg.initial_capital == 500.0  # defaults
