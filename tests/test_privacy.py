"""Tests for monai.utils.privacy — anonymization layer."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from monai.config import Config, PrivacyConfig
from monai.utils.privacy import (
    USER_AGENTS,
    VIEWPORTS,
    TIMEZONES,
    LOCALES,
    AnonymityError,
    NetworkAnonymizer,
    TorController,
    get_anonymizer,
    reset_anonymizer,
)


@pytest.fixture(autouse=True)
def reset_global():
    """Reset the global anonymizer singleton between tests."""
    reset_anonymizer()
    yield
    reset_anonymizer()


@pytest.fixture
def tor_config():
    cfg = Config()
    cfg.privacy = PrivacyConfig(proxy_type="tor", tor_socks_port=9050)
    return cfg


@pytest.fixture
def socks5_config():
    cfg = Config()
    cfg.privacy = PrivacyConfig(proxy_type="socks5", socks5_proxy="socks5://proxy.example.com:1080")
    return cfg


@pytest.fixture
def http_config():
    cfg = Config()
    cfg.privacy = PrivacyConfig(proxy_type="http", http_proxy="http://proxy.example.com:8080")
    return cfg


@pytest.fixture
def no_proxy_config():
    cfg = Config()
    cfg.privacy = PrivacyConfig(proxy_type="none")
    return cfg


class TestProxyUrls:
    def test_tor_proxy_url(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        assert anon.get_proxy_url() == "socks5://127.0.0.1:9050"

    def test_socks5_proxy_url(self, socks5_config):
        anon = NetworkAnonymizer(socks5_config)
        assert anon.get_proxy_url() == "socks5://proxy.example.com:1080"

    def test_http_proxy_url(self, http_config):
        anon = NetworkAnonymizer(http_config)
        assert anon.get_proxy_url() == "http://proxy.example.com:8080"

    def test_no_proxy_url(self, no_proxy_config):
        anon = NetworkAnonymizer(no_proxy_config)
        assert anon.get_proxy_url() is None


class TestProxyDict:
    def test_tor_proxy_dict(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        d = anon.get_proxy_dict()
        assert "all://" in d
        assert "socks5" in d["all://"]

    def test_no_proxy_empty_dict(self, no_proxy_config):
        anon = NetworkAnonymizer(no_proxy_config)
        assert anon.get_proxy_dict() == {}


class TestBrowserProxy:
    def test_tor_browser_proxy(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        proxy = anon.get_browser_proxy()
        assert proxy is not None
        assert "server" in proxy
        assert "socks5" in proxy["server"]

    def test_no_proxy_returns_none(self, no_proxy_config):
        anon = NetworkAnonymizer(no_proxy_config)
        assert anon.get_browser_proxy() is None


class TestUserAgent:
    def test_returns_string(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        ua = anon.get_user_agent()
        assert isinstance(ua, str)
        assert len(ua) > 20

    def test_rotation_produces_variety(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        agents = {anon.get_user_agent() for _ in range(100)}
        assert len(agents) > 1  # Should get multiple different user agents

    def test_no_rotation_returns_first(self, tor_config):
        tor_config.privacy.rotate_user_agent = False
        anon = NetworkAnonymizer(tor_config)
        agents = {anon.get_user_agent() for _ in range(10)}
        assert len(agents) == 1
        assert agents.pop() == USER_AGENTS[0]


class TestBrowserFingerprint:
    def test_returns_all_fields(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        fp = anon.get_browser_fingerprint()
        assert "user_agent" in fp
        assert "viewport" in fp
        assert "timezone_id" in fp
        assert "locale" in fp
        assert "color_scheme" in fp
        assert "device_scale_factor" in fp

    def test_viewport_has_dimensions(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        fp = anon.get_browser_fingerprint()
        assert "width" in fp["viewport"]
        assert "height" in fp["viewport"]
        assert fp["viewport"]["width"] > 0
        assert fp["viewport"]["height"] > 0

    def test_timezone_is_valid(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        fp = anon.get_browser_fingerprint()
        assert fp["timezone_id"] in TIMEZONES

    def test_locale_is_valid(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        fp = anon.get_browser_fingerprint()
        assert fp["locale"] in LOCALES

    def test_fingerprints_vary(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        fps = [anon.get_browser_fingerprint() for _ in range(50)]
        timezones = {fp["timezone_id"] for fp in fps}
        assert len(timezones) > 1  # Should get variety


class TestCircuitManagement:
    def test_maybe_rotate_counts_requests(self, tor_config):
        tor_config.privacy.max_requests_per_circuit = 5
        anon = NetworkAnonymizer(tor_config)
        # Mock the tor controller so it doesn't actually connect
        anon._tor_controller = MagicMock()
        anon._tor_controller.new_circuit.return_value = True

        for _ in range(4):
            anon.maybe_rotate()
        anon._tor_controller.new_circuit.assert_not_called()

        anon.maybe_rotate()  # 5th request — should trigger rotation
        anon._tor_controller.new_circuit.assert_called_once()

    def test_maybe_rotate_resets_counter(self, tor_config):
        tor_config.privacy.max_requests_per_circuit = 3
        anon = NetworkAnonymizer(tor_config)
        anon._tor_controller = MagicMock()
        anon._tor_controller.new_circuit.return_value = True

        for _ in range(6):
            anon.maybe_rotate()
        # Should have rotated twice (at 3 and 6)
        assert anon._tor_controller.new_circuit.call_count == 2


class TestHttpClient:
    def test_creates_client_with_proxy(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        client = anon.create_http_client()
        # httpx Client should have been created (we can't easily check proxy config
        # but we can verify it's an httpx.Client)
        import httpx
        assert isinstance(client, httpx.Client)
        client.close()

    def test_creates_client_without_proxy(self, no_proxy_config):
        anon = NetworkAnonymizer(no_proxy_config)
        client = anon.create_http_client()
        import httpx
        assert isinstance(client, httpx.Client)
        client.close()

    def test_creates_async_client(self, tor_config):
        anon = NetworkAnonymizer(tor_config)
        client = anon.create_async_http_client()
        import httpx
        assert isinstance(client, httpx.AsyncClient)


class TestAnonymityVerification:
    def test_no_proxy_warns(self, no_proxy_config):
        anon = NetworkAnonymizer(no_proxy_config)
        result = anon.verify_anonymity()
        assert result["proxy_active"] is False

    def test_startup_check_no_proxy_blocked(self, no_proxy_config):
        """proxy_type=none must raise unless MONAI_ALLOW_NO_PROXY is set."""
        anon = NetworkAnonymizer(no_proxy_config)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MONAI_ALLOW_NO_PROXY", None)
            with pytest.raises(AnonymityError, match="proxy_type=none is dangerous"):
                anon.startup_check()

    def test_startup_check_no_proxy_allowed(self, no_proxy_config):
        """proxy_type=none allowed with explicit MONAI_ALLOW_NO_PROXY=1."""
        anon = NetworkAnonymizer(no_proxy_config)
        with patch.dict(os.environ, {"MONAI_ALLOW_NO_PROXY": "1"}):
            result = anon.startup_check()
            assert result["anonymous"] is False
            assert "explicit override" in result["reason"]


class TestMetadataStripping:
    def test_strip_disabled(self, tor_config):
        tor_config.privacy.strip_metadata = False
        anon = NetworkAnonymizer(tor_config)
        # Should not raise even on non-existent file when disabled
        anon.strip_file_metadata(Path("/nonexistent.jpg"))

    def test_strip_image_metadata(self, tor_config, tmp_path):
        anon = NetworkAnonymizer(tor_config)
        # Create a small test image with Pillow
        try:
            from PIL import Image
            img = Image.new("RGB", (10, 10), color="red")
            # Add some EXIF-like data
            img_path = tmp_path / "test.jpg"
            img.save(img_path)
            anon.strip_image_metadata(img_path)
            assert img_path.exists()
            # Verify the cleaned image is readable
            cleaned = Image.open(img_path)
            assert cleaned.size == (10, 10)
        except ImportError:
            pytest.skip("Pillow not installed")

    def test_strip_unsupported_file_type(self, tor_config, tmp_path):
        anon = NetworkAnonymizer(tor_config)
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("hello")
        # Should not crash on unsupported types
        anon.strip_file_metadata(txt_file)
        assert txt_file.read_text() == "hello"


class TestGlobalSingleton:
    def test_get_anonymizer_returns_same_instance(self, tor_config):
        a1 = get_anonymizer(tor_config)
        a2 = get_anonymizer()
        assert a1 is a2

    def test_reset_clears_singleton(self, tor_config):
        a1 = get_anonymizer(tor_config)
        reset_anonymizer()
        a2 = get_anonymizer(tor_config)
        assert a1 is not a2


class TestUserAgentList:
    def test_has_multiple_agents(self):
        assert len(USER_AGENTS) >= 5

    def test_includes_different_browsers(self):
        joined = " ".join(USER_AGENTS)
        assert "Chrome" in joined
        assert "Firefox" in joined
        assert "Safari" in joined

    def test_includes_different_platforms(self):
        joined = " ".join(USER_AGENTS)
        assert "Windows" in joined
        assert "Macintosh" in joined
        assert "Linux" in joined


class TestViewports:
    def test_has_common_resolutions(self):
        widths = {v["width"] for v in VIEWPORTS}
        assert 1920 in widths
        assert 1366 in widths


class TestPrivacyConfig:
    def test_defaults_to_tor(self):
        cfg = PrivacyConfig()
        assert cfg.proxy_type == "tor"
        assert cfg.tor_socks_port == 9050
        assert cfg.rotate_user_agent is True
        assert cfg.strip_metadata is True
        assert cfg.dns_over_proxy is True
        assert cfg.verify_anonymity is True

    def test_config_in_main_config(self):
        cfg = Config()
        assert hasattr(cfg, "privacy")
        assert isinstance(cfg.privacy, PrivacyConfig)
        assert cfg.privacy.proxy_type == "tor"
