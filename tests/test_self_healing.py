"""Tests for self-healing strategy management.

Covers:
- Strategy auto-pauses after N consecutive proxy failures
- Strategy auto-retries with exponential backoff
- Success resets failure counter and backoff
- Proxy failure detection in strategy error messages
- Free proxy pool integration into fallback chain
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from monai.config import PrivacyConfig
from monai.utils.privacy import (
    PROXY_DATACENTER,
    PROXY_FREE,
    PROXY_RESIDENTIAL,
    PROXY_TOR,
    AllProxiesBlockedError,
    ProxyFallbackChain,
)


# ── Free Proxy Tier in Fallback Chain ─────────────────────────


class TestFreeProxyFallback:
    """Free proxies appear as last tier before aborting."""

    @pytest.fixture
    def chain_with_free(self):
        """Chain with Tor + free proxies (no residential/datacenter)."""
        config = PrivacyConfig(
            proxy_type="tor",
            tor_socks_port=9050,
            residential_proxy="",
            datacenter_proxy="",
            fallback_enabled=True,
        )
        chain = ProxyFallbackChain(config)
        # Mock the free proxy pool to return a proxy
        mock_pool = MagicMock()
        mock_pool.get_proxy.return_value = "http://1.2.3.4:8080"
        chain._free_proxy_pool = mock_pool
        return chain

    def test_tor_blocked_falls_to_free_proxy(self, chain_with_free):
        chain_with_free.report_blocked("example.com", PROXY_TOR)
        ptype, url = chain_with_free.get_proxy_for_domain("example.com")
        assert ptype == PROXY_FREE
        assert url == "http://1.2.3.4:8080"

    def test_all_blocked_including_free_raises(self, chain_with_free):
        chain_with_free.report_blocked("example.com", PROXY_TOR)
        chain_with_free.report_blocked("example.com", PROXY_FREE)
        with pytest.raises(AllProxiesBlockedError):
            chain_with_free.get_proxy_for_domain("example.com")

    def test_free_proxy_pool_not_configured_skipped(self):
        """When free proxy pool returns None, it's treated as unconfigured."""
        config = PrivacyConfig(
            proxy_type="tor",
            tor_socks_port=9050,
            residential_proxy="",
            datacenter_proxy="",
            fallback_enabled=True,
        )
        chain = ProxyFallbackChain(config)
        mock_pool = MagicMock()
        mock_pool.get_proxy.return_value = None  # No proxies available
        chain._free_proxy_pool = mock_pool
        chain.report_blocked("example.com", PROXY_TOR)
        with pytest.raises(AllProxiesBlockedError):
            chain.get_proxy_for_domain("example.com")

    def test_full_chain_with_free_proxy(self):
        """Free proxy comes after datacenter in the full chain."""
        config = PrivacyConfig(
            proxy_type="tor",
            tor_socks_port=9050,
            residential_proxy="socks5://res:1080",
            datacenter_proxy="http://dc:8080",
            fallback_enabled=True,
        )
        chain = ProxyFallbackChain(config)
        mock_pool = MagicMock()
        mock_pool.get_proxy.return_value = "http://free:3128"
        chain._free_proxy_pool = mock_pool

        # Block tor, residential, datacenter
        chain.report_blocked("example.com", PROXY_TOR)
        chain.report_blocked("example.com", PROXY_RESIDENTIAL)
        chain.report_blocked("example.com", PROXY_DATACENTER)

        # Should fall to free
        ptype, url = chain.get_proxy_for_domain("example.com")
        assert ptype == PROXY_FREE
        assert url == "http://free:3128"


# ── Self-Healing Strategy Management ──────────────────────────


class TestSelfHealingRecording:
    """Test that proxy failures and successes are tracked correctly."""

    @pytest.fixture
    def mock_orchestrator(self):
        """Create a minimal mock orchestrator with DB for testing."""
        from monai.db.database import Database
        import tempfile
        from pathlib import Path

        tmpdir = tempfile.mkdtemp()
        db = Database(db_path=Path(tmpdir) / "test.db")

        orch = MagicMock()
        orch.db = db
        orch.SELF_HEALING_CONFIG = {
            "max_consecutive_failures": 3,
            "base_retry_interval": 60,  # Short for testing
            "max_retry_interval": 3600,
            "backoff_factor": 2,
        }

        # Import the actual methods and bind them
        from monai.agents.orchestrator import Orchestrator
        orch._init_strategy_health = lambda: Orchestrator._init_strategy_health(orch)
        orch.record_strategy_proxy_failure = lambda name, reason: \
            Orchestrator.record_strategy_proxy_failure(orch, name, reason)
        orch.record_strategy_success = lambda name: \
            Orchestrator.record_strategy_success(orch, name)
        orch._is_strategy_auto_paused = lambda name: \
            Orchestrator._is_strategy_auto_paused(orch, name)
        orch._get_strategy_pause_reason = lambda name: \
            Orchestrator._get_strategy_pause_reason(orch, name)
        orch._check_strategy_retries = lambda results: \
            Orchestrator._check_strategy_retries(orch, results)

        # Insert a test strategy
        db.execute_insert(
            "INSERT INTO strategies (name, category, status) VALUES (?, ?, ?)",
            ("test_strategy", "test", "active"),
        )

        return orch

    def test_single_failure_does_not_pause(self, mock_orchestrator):
        mock_orchestrator.record_strategy_proxy_failure(
            "test_strategy", "Tor blocked"
        )
        assert not mock_orchestrator._is_strategy_auto_paused("test_strategy")

        # Strategy should still be active
        rows = mock_orchestrator.db.execute(
            "SELECT status FROM strategies WHERE name = 'test_strategy'"
        )
        assert rows[0]["status"] == "active"

    def test_three_failures_triggers_auto_pause(self, mock_orchestrator):
        for i in range(3):
            mock_orchestrator.record_strategy_proxy_failure(
                "test_strategy", f"Tor blocked attempt {i+1}"
            )

        assert mock_orchestrator._is_strategy_auto_paused("test_strategy")

        # Strategy should be paused in DB
        rows = mock_orchestrator.db.execute(
            "SELECT status FROM strategies WHERE name = 'test_strategy'"
        )
        assert rows[0]["status"] == "paused"

    def test_success_resets_failure_counter(self, mock_orchestrator):
        # Two failures
        mock_orchestrator.record_strategy_proxy_failure("test_strategy", "fail 1")
        mock_orchestrator.record_strategy_proxy_failure("test_strategy", "fail 2")

        # Success resets
        mock_orchestrator.record_strategy_success("test_strategy")

        # Two more failures should NOT pause (counter was reset)
        mock_orchestrator.record_strategy_proxy_failure("test_strategy", "fail 3")
        mock_orchestrator.record_strategy_proxy_failure("test_strategy", "fail 4")
        assert not mock_orchestrator._is_strategy_auto_paused("test_strategy")

    def test_pause_reason_includes_failure_info(self, mock_orchestrator):
        for _ in range(3):
            mock_orchestrator.record_strategy_proxy_failure(
                "test_strategy", "AllProxiesBlockedError on upwork.com"
            )

        reason = mock_orchestrator._get_strategy_pause_reason("test_strategy")
        assert "proxy failures" in reason
        assert "AllProxiesBlockedError" in reason

    def test_retry_reactivates_strategy(self, mock_orchestrator):
        # Cause auto-pause
        for _ in range(3):
            mock_orchestrator.record_strategy_proxy_failure(
                "test_strategy", "blocked"
            )
        assert mock_orchestrator._is_strategy_auto_paused("test_strategy")

        # Manually set next_retry_at to the past to simulate timer expiry
        mock_orchestrator.db.execute(
            "UPDATE strategy_health SET next_retry_at = ? "
            "WHERE strategy_name = 'test_strategy'",
            (time.time() - 10,),
        )

        # Run retry check
        results = {}
        mock_orchestrator._check_strategy_retries(results)

        # Strategy should be active again
        rows = mock_orchestrator.db.execute(
            "SELECT status FROM strategies WHERE name = 'test_strategy'"
        )
        assert rows[0]["status"] == "active"
        assert "test_strategy" in results
        assert results["test_strategy"]["status"] == "retrying"

    def test_retry_not_before_timer(self, mock_orchestrator):
        """Strategy should NOT retry before next_retry_at."""
        for _ in range(3):
            mock_orchestrator.record_strategy_proxy_failure(
                "test_strategy", "blocked"
            )

        # next_retry_at is in the future — should NOT reactivate
        results = {}
        mock_orchestrator._check_strategy_retries(results)
        assert "test_strategy" not in results

        rows = mock_orchestrator.db.execute(
            "SELECT status FROM strategies WHERE name = 'test_strategy'"
        )
        assert rows[0]["status"] == "paused"

    def test_backoff_increases_with_retries(self, mock_orchestrator):
        """Each retry should have a longer interval."""
        # First auto-pause
        for _ in range(3):
            mock_orchestrator.record_strategy_proxy_failure(
                "test_strategy", "blocked"
            )
        rows = mock_orchestrator.db.execute(
            "SELECT next_retry_at, auto_paused_at FROM strategy_health "
            "WHERE strategy_name = 'test_strategy'"
        )
        first_interval = rows[0]["next_retry_at"] - rows[0]["auto_paused_at"]

        # Simulate retry and re-failure
        mock_orchestrator.db.execute(
            "UPDATE strategy_health SET next_retry_at = ?, "
            "auto_paused_at = NULL, consecutive_proxy_failures = 0 "
            "WHERE strategy_name = 'test_strategy'",
            (time.time() - 10,),
        )
        mock_orchestrator.db.execute_insert(
            "UPDATE strategies SET status = 'active' WHERE name = 'test_strategy'"
        )

        # Second round of failures
        for _ in range(3):
            mock_orchestrator.record_strategy_proxy_failure(
                "test_strategy", "still blocked"
            )

        rows = mock_orchestrator.db.execute(
            "SELECT next_retry_at, auto_paused_at FROM strategy_health "
            "WHERE strategy_name = 'test_strategy'"
        )
        second_interval = rows[0]["next_retry_at"] - rows[0]["auto_paused_at"]

        # Second interval should be ~2x the first (backoff_factor=2)
        assert second_interval > first_interval * 1.5

    def test_success_after_retry_resets_backoff(self, mock_orchestrator):
        """Success should reset retry_count so next failure starts fresh."""
        # Auto-pause
        for _ in range(3):
            mock_orchestrator.record_strategy_proxy_failure(
                "test_strategy", "blocked"
            )

        # Simulate success
        mock_orchestrator.record_strategy_success("test_strategy")

        rows = mock_orchestrator.db.execute(
            "SELECT retry_count, consecutive_proxy_failures "
            "FROM strategy_health WHERE strategy_name = 'test_strategy'"
        )
        assert rows[0]["retry_count"] == 0
        assert rows[0]["consecutive_proxy_failures"] == 0


class TestProxyFailureDetection:
    """Test that proxy-related errors are correctly identified."""

    @pytest.mark.parametrize("error_msg,should_detect", [
        ("AllProxiesBlockedError: all proxies blocked for upwork.com", True),
        ("403 Forbidden - Access Denied", True),
        ("Tor exit node detected, registration blocked", True),
        ("CAPTCHA required for account creation", True),
        ("Registration failed: proxy detected", True),
        ("Connection timeout to database", False),
        ("Invalid JSON response", False),
        ("Rate limit exceeded - try again later", False),
        ("File not found: template.html", False),
    ])
    def test_proxy_failure_patterns(self, error_msg, should_detect):
        """Verify proxy failure detection heuristic."""
        err_lower = error_msg.lower()
        is_proxy = any(s in err_lower for s in [
            "allproxiesblocked", "proxy", "tor",
            "blocked", "403", "captcha",
            "anonymity", "registration failed",
            "account creation", "access denied",
        ])
        assert is_proxy == should_detect, (
            f"Expected {'detection' if should_detect else 'no detection'} "
            f"for: {error_msg!r}"
        )


# ── Free Proxy Pool Unit Tests ─────────────────────────────────


class TestFreeProxyPool:
    """Test the FreeProxyPool without network access."""

    def test_pool_starts_empty(self):
        from monai.utils.free_proxies import FreeProxyPool
        pool = FreeProxyPool()
        assert pool.pool_size() == 0

    def test_report_success_increases_score(self):
        from monai.utils.free_proxies import FreeProxyPool
        pool = FreeProxyPool()
        pool._proxies = [{"url": "http://1.2.3.4:8080", "protocol": "http"}]
        pool._initialized = True
        pool._last_refresh = time.time()  # Prevent refresh

        pool.report_success("http://1.2.3.4:8080")
        assert pool._stats["http://1.2.3.4:8080"]["successes"] == 1

    def test_repeated_failures_remove_proxy(self):
        from monai.utils.free_proxies import FreeProxyPool
        pool = FreeProxyPool()
        pool._proxies = [{"url": "http://bad:8080", "protocol": "http"}]
        pool._initialized = True
        pool._last_refresh = time.time()

        # 3 failures with >70% failure rate should remove it
        for _ in range(3):
            pool.report_failure("http://bad:8080")

        assert pool.pool_size() == 0

    def test_get_proxy_returns_from_pool(self):
        from monai.utils.free_proxies import FreeProxyPool
        pool = FreeProxyPool()
        pool._proxies = [
            {"url": "http://a:8080", "protocol": "http"},
            {"url": "http://b:8080", "protocol": "http"},
        ]
        pool._initialized = True
        pool._last_refresh = time.time()

        proxy = pool.get_proxy()
        assert proxy in ("http://a:8080", "http://b:8080")

    def test_geonode_parser(self):
        from monai.utils.free_proxies import FreeProxyPool
        data = {
            "data": [
                {"ip": "1.2.3.4", "port": "1080", "protocols": ["socks5"], "country": "US"},
                {"ip": "5.6.7.8", "port": "8080", "protocols": ["https"], "country": "DE"},
            ]
        }
        result = FreeProxyPool._parse_geonode(data)
        assert len(result) == 2
        assert result[0]["url"] == "socks5://1.2.3.4:1080"
        assert result[1]["url"] == "http://5.6.7.8:8080"

    def test_html_table_parser(self):
        from monai.utils.free_proxies import FreeProxyPool
        # Must match the regex: IP, Port, CountryCode, CountryCode2, field, field, yes/no
        # The regex expects exactly 7 <td> groups per <tr>
        html = (
            "<tr><td>1.2.3.4</td><td>8080</td>"
            "<td>US</td><td>US</td>"
            "<td>elite proxy</td><td>no</td>"
            "<td>yes</td></tr>"
            "<tr><td>5.6.7.8</td><td>3128</td>"
            "<td>DE</td><td>DE</td>"
            "<td>anonymous</td><td>no</td>"
            "<td>no</td></tr>"
        )
        result = FreeProxyPool._parse_html_table(html)
        # Only HTTPS=yes should be included
        assert len(result) == 1
        assert result[0]["url"] == "http://1.2.3.4:8080"
