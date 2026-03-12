"""Tests for infrastructure auto-setup module."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from monai.infra.auto_setup import InfraSetup, MONAI_DIR


class TestInfraSetup:
    def test_tor_already_running(self):
        setup = InfraSetup()
        with patch.object(setup, "_is_port_open", return_value=True):
            result = setup._ensure_tor()
        assert result["status"] == "already_running"

    def test_tor_not_found_no_install(self):
        setup = InfraSetup()
        with patch.object(setup, "_is_port_open", return_value=False), \
             patch("shutil.which", return_value=None), \
             patch.object(setup, "_install_tor", return_value=False):
            result = setup._ensure_tor()
        assert result["status"] == "failed"

    def test_monero_already_running(self):
        setup = InfraSetup()
        with patch.object(setup, "_is_port_open", return_value=True):
            result = setup._ensure_monero_wallet_rpc()
        assert result["status"] == "already_running"

    def test_llm_openai_env(self):
        setup = InfraSetup()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = setup._ensure_llm_access()
        assert result["status"] == "ok"
        assert result["provider"] == "openai"

    def test_llm_anthropic_env(self):
        setup = InfraSetup()
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
            # Clear OPENAI_API_KEY if set
            env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
            with patch.dict("os.environ", env, clear=True):
                result = setup._ensure_llm_access()
        assert result["status"] == "ok"
        assert result["provider"] == "anthropic"

    def test_llm_ollama_detected(self):
        setup = InfraSetup()

        def port_check(port, host="127.0.0.1"):
            return port == 11434

        with patch.dict("os.environ", {}, clear=True), \
             patch.object(setup, "_is_port_open", side_effect=port_check), \
             patch.object(setup, "_configure_ollama_backend"):
            # Need to ensure config file doesn't have api_key
            with patch("builtins.open", side_effect=FileNotFoundError):
                with patch.object(Path, "exists", return_value=False):
                    result = setup._ensure_llm_access()
        assert result["status"] == "ok"
        assert result["provider"] == "ollama"

    def test_crypto_not_configured_skips_monero(self, tmp_path):
        import monai.infra.auto_setup as mod
        orig = mod.MONAI_DIR
        try:
            mod.MONAI_DIR = tmp_path
            setup = InfraSetup()
            config_file = tmp_path / "config.json"
            config_file.write_text(json.dumps({"privacy": {"proxy_type": "tor"}}))
            assert setup._is_crypto_configured() is False
        finally:
            mod.MONAI_DIR = orig

    def test_crypto_configured_with_xmr_address(self, tmp_path):
        import monai.infra.auto_setup as mod
        orig = mod.MONAI_DIR
        try:
            mod.MONAI_DIR = tmp_path
            setup = InfraSetup()
            config_file = tmp_path / "config.json"
            config_file.write_text(json.dumps({
                "creator_wallet": {"xmr_address": "4" + "A" * 94}
            }))
            assert setup._is_crypto_configured() is True
        finally:
            mod.MONAI_DIR = orig

    def test_ensure_config_creates_default(self, tmp_path):
        setup = InfraSetup()
        config_file = tmp_path / "config.json"
        with patch("monai.infra.auto_setup.MONAI_DIR", tmp_path):
            result = setup._ensure_config()
        assert result["status"] == "ok"
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert data["privacy"]["proxy_type"] == "tor"

    def test_is_port_open_closed(self):
        setup = InfraSetup()
        # Port 1 should never be open
        assert setup._is_port_open(1) is False


class TestLocalModelPricing:
    def test_ollama_model_free(self):
        from monai.utils.llm import _get_model_pricing
        pricing = _get_model_pricing("llama3.1:8b")
        assert pricing["input"] == 0.0
        assert pricing["output"] == 0.0

    def test_openai_model_has_cost(self):
        from monai.utils.llm import _get_model_pricing
        pricing = _get_model_pricing("gpt-4o")
        assert pricing["input"] > 0

    def test_unknown_model_defaults_to_mini(self):
        from monai.utils.llm import _get_model_pricing
        pricing = _get_model_pricing("some-unknown-model")
        assert pricing["input"] == 0.15
