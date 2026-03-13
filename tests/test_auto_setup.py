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


class TestSandboxSetup:
    def test_bwrap_already_installed(self):
        setup = InfraSetup()
        with patch("shutil.which", return_value="/usr/bin/bwrap"):
            result = setup._ensure_sandbox()
        assert result["status"] == "ok"

    def test_bwrap_not_found_install_succeeds(self):
        setup = InfraSetup()
        with patch.object(setup, "_install_bubblewrap", return_value=True), \
             patch("shutil.which", return_value=None), \
             patch("monai.utils.sandbox.refresh_isolation_backend", return_value="bubblewrap"):
            result = setup._ensure_sandbox()
        assert result["status"] == "ok"
        assert result["method"] == "auto_installed"

    def test_bwrap_install_fails_degrades(self):
        setup = InfraSetup()
        with patch("shutil.which", return_value=None):
            result = setup._ensure_sandbox()
        assert result["status"] == "degraded"
        assert "warning" in result

    def test_bwrap_install_not_linux(self):
        setup = InfraSetup()
        with patch("shutil.which", return_value=None), \
             patch("platform.system", return_value="Darwin"):
            result = setup._install_bubblewrap()
        assert result is False

    def test_run_all_includes_sandbox(self, tmp_path):
        setup = InfraSetup()
        with patch("monai.infra.auto_setup.MONAI_DIR", tmp_path), \
             patch("monai.infra.auto_setup.MONAI_BIN", tmp_path / "bin"), \
             patch.object(setup, "_ensure_sandbox", return_value={"status": "ok"}), \
             patch.object(setup, "_ensure_tor", return_value={"status": "ok"}), \
             patch.object(setup, "_ensure_llm_access", return_value={"status": "ok"}), \
             patch.object(setup, "_is_crypto_configured", return_value=False):
            (tmp_path / "bin").mkdir(exist_ok=True)
            results = setup.run_all()
        assert "sandbox" in results
        assert results["sandbox"]["status"] == "ok"


class TestBrowserSetup:
    def test_chromium_already_installed(self):
        setup = InfraSetup()
        with patch("monai.infra.auto_setup.InfraSetup._is_chromium_installed", return_value=True), \
             patch.dict("sys.modules", {"playwright": MagicMock(), "playwright.sync_api": MagicMock()}):
            result = setup._ensure_browser()
        assert result["status"] == "ok"

    def test_playwright_not_installed(self):
        setup = InfraSetup()
        import sys
        # Simulate playwright not importable
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            # Force ImportError
            with patch("builtins.__import__", side_effect=ImportError("no playwright")):
                result = setup._ensure_browser()
        assert result["status"] == "degraded"
        assert "playwright" in result["warning"]

    def test_chromium_install_succeeds(self):
        setup = InfraSetup()
        with patch.object(setup, "_is_chromium_installed", return_value=False), \
             patch.object(setup, "_install_chromium", return_value={"status": "ok", "method": "auto_installed"}), \
             patch.dict("sys.modules", {"playwright": MagicMock(), "playwright.sync_api": MagicMock()}):
            result = setup._ensure_browser()
        assert result["status"] == "ok"

    def test_install_chromium_runs_playwright(self):
        setup = InfraSetup()
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as mock_run, \
             patch("platform.system", return_value="Linux"):
            result = setup._install_chromium()
        assert result["status"] == "ok"
        # Should have called playwright install chromium
        calls = [str(c) for c in mock_run.call_args_list]
        assert any("chromium" in c for c in calls)

    def test_install_chromium_failure_degrades(self):
        setup = InfraSetup()
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="error")), \
             patch("platform.system", return_value="Linux"):
            result = setup._install_chromium()
        assert result["status"] == "degraded"


class TestPdfLibsSetup:
    def test_weasyprint_available(self):
        setup = InfraSetup()
        mock_weasyprint = MagicMock()
        with patch.dict("sys.modules", {"weasyprint": mock_weasyprint}):
            result = setup._ensure_pdf_libs()
        assert result["status"] == "ok"

    def test_weasyprint_missing_non_linux(self):
        setup = InfraSetup()
        with patch.dict("sys.modules", {"weasyprint": None}), \
             patch("builtins.__import__", side_effect=ImportError("no weasyprint")), \
             patch("platform.system", return_value="Darwin"):
            result = setup._ensure_pdf_libs()
        assert result["status"] == "degraded"

    def test_run_all_includes_browser_and_pdf(self, tmp_path):
        setup = InfraSetup()
        with patch("monai.infra.auto_setup.MONAI_DIR", tmp_path), \
             patch("monai.infra.auto_setup.MONAI_BIN", tmp_path / "bin"), \
             patch.object(setup, "_ensure_sandbox", return_value={"status": "ok"}), \
             patch.object(setup, "_ensure_tor", return_value={"status": "ok"}), \
             patch.object(setup, "_ensure_llm_access", return_value={"status": "ok"}), \
             patch.object(setup, "_ensure_browser", return_value={"status": "ok"}), \
             patch.object(setup, "_ensure_pdf_libs", return_value={"status": "degraded", "warning": "test"}), \
             patch.object(setup, "_is_crypto_configured", return_value=False):
            (tmp_path / "bin").mkdir(exist_ok=True)
            results = setup.run_all()
        assert "browser" in results
        assert "pdf_libs" in results
        # degraded is acceptable — system still starts
        assert results["ready"] is True


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
