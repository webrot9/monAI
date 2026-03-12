"""Tests for ProxyFallbackChain — Tor detection fallback.

Covers:
- Basic proxy resolution for domains
- Fallback chain ordering (Tor → residential → datacenter)
- Block detection and automatic fallback
- Preferred proxy after success
- AllProxiesBlockedError when chain exhausted
- Block page content detection
- Status code detection (403, 429, 503)
- Thread safety (concurrent access)
- Fallback disabled mode
"""

import threading

import pytest

from monai.config import PrivacyConfig
from monai.utils.privacy import (
    PROXY_TOR,
    PROXY_RESIDENTIAL,
    PROXY_DATACENTER,
    AllProxiesBlockedError,
    ProxyFallbackChain,
)


@pytest.fixture
def full_chain():
    """Chain with all three proxy types configured."""
    config = PrivacyConfig(
        proxy_type="tor",
        tor_socks_port=9050,
        residential_proxy="socks5://res.proxy:1080",
        datacenter_proxy="http://dc.proxy:8080",
        fallback_enabled=True,
    )
    return ProxyFallbackChain(config)


@pytest.fixture
def tor_only():
    """Chain with only Tor configured (no fallbacks)."""
    config = PrivacyConfig(
        proxy_type="tor",
        tor_socks_port=9050,
        residential_proxy="",
        datacenter_proxy="",
        fallback_enabled=True,
    )
    return ProxyFallbackChain(config)


class TestBasicResolution:
    def test_default_returns_tor(self, full_chain):
        ptype, url = full_chain.get_proxy_for_domain("example.com")
        assert ptype == PROXY_TOR
        assert "9050" in url

    def test_different_domains_independent(self, full_chain):
        p1, _ = full_chain.get_proxy_for_domain("site-a.com")
        p2, _ = full_chain.get_proxy_for_domain("site-b.com")
        assert p1 == PROXY_TOR
        assert p2 == PROXY_TOR


class TestFallbackOrdering:
    def test_tor_blocked_falls_to_residential(self, full_chain):
        full_chain.report_blocked("example.com", PROXY_TOR)
        ptype, url = full_chain.get_proxy_for_domain("example.com")
        assert ptype == PROXY_RESIDENTIAL
        assert "res.proxy" in url

    def test_tor_and_residential_blocked_falls_to_datacenter(self, full_chain):
        full_chain.report_blocked("example.com", PROXY_TOR)
        full_chain.report_blocked("example.com", PROXY_RESIDENTIAL)
        ptype, url = full_chain.get_proxy_for_domain("example.com")
        assert ptype == PROXY_DATACENTER
        assert "dc.proxy" in url

    def test_all_blocked_raises_error(self, full_chain):
        full_chain.report_blocked("example.com", PROXY_TOR)
        full_chain.report_blocked("example.com", PROXY_RESIDENTIAL)
        full_chain.report_blocked("example.com", PROXY_DATACENTER)
        with pytest.raises(AllProxiesBlockedError):
            full_chain.get_proxy_for_domain("example.com")

    def test_tor_only_blocked_raises_immediately(self, tor_only):
        tor_only.report_blocked("example.com", PROXY_TOR)
        with pytest.raises(AllProxiesBlockedError):
            tor_only.get_proxy_for_domain("example.com")

    def test_get_next_fallback(self, full_chain):
        """get_next_fallback should block current and return next."""
        ptype, url = full_chain.get_next_fallback("example.com", PROXY_TOR)
        assert ptype == PROXY_RESIDENTIAL
        assert full_chain.is_blocked("example.com", PROXY_TOR)


class TestPreferredProxy:
    def test_success_sets_preferred(self, full_chain):
        full_chain.report_success("example.com", PROXY_RESIDENTIAL)
        ptype, _ = full_chain.get_proxy_for_domain("example.com")
        assert ptype == PROXY_RESIDENTIAL

    def test_preferred_skipped_if_blocked(self, full_chain):
        full_chain.report_success("example.com", PROXY_RESIDENTIAL)
        full_chain.report_blocked("example.com", PROXY_RESIDENTIAL)
        ptype, _ = full_chain.get_proxy_for_domain("example.com")
        assert ptype == PROXY_TOR  # Falls back to start of chain

    def test_success_unblocks_previously_blocked(self, full_chain):
        full_chain.report_blocked("example.com", PROXY_TOR)
        assert full_chain.is_blocked("example.com", PROXY_TOR)
        full_chain.report_success("example.com", PROXY_TOR)
        assert not full_chain.is_blocked("example.com", PROXY_TOR)

    def test_block_clears_preferred(self, full_chain):
        full_chain.report_success("example.com", PROXY_TOR)
        full_chain.report_blocked("example.com", PROXY_TOR)
        # Should fall through to residential, not try Tor
        ptype, _ = full_chain.get_proxy_for_domain("example.com")
        assert ptype == PROXY_RESIDENTIAL


class TestBlockDetection:
    def test_block_page_with_multiple_patterns(self):
        content = (
            "<html><body><h1>Access Denied</h1>"
            "<p>Please verify you are a human. Complete the CAPTCHA below.</p>"
            "</body></html>"
        )
        assert ProxyFallbackChain.detect_block_page(content) is True

    def test_normal_page_not_detected(self):
        content = (
            "<html><body><h1>Welcome</h1>"
            "<p>This is a normal website with real content.</p>"
            "</body></html>"
        )
        assert ProxyFallbackChain.detect_block_page(content) is False

    def test_single_pattern_not_enough(self):
        """Single pattern match should NOT trigger (reduce false positives)."""
        content = "<html><body><p>Complete the captcha to continue.</p></body></html>"
        assert ProxyFallbackChain.detect_block_page(content) is False

    def test_cloudflare_block_detected(self):
        content = (
            "<html><body>"
            "<p>Checking your browser before accessing the site.</p>"
            "<p>Ray ID: abc123</p>"
            "<p>Attention Required! Enable JavaScript and Cookies.</p>"
            "</body></html>"
        )
        assert ProxyFallbackChain.detect_block_page(content) is True

    def test_empty_content_not_detected(self):
        assert ProxyFallbackChain.detect_block_page("") is False
        assert ProxyFallbackChain.detect_block_page(None) is False

    def test_blocked_status_codes(self):
        assert ProxyFallbackChain.is_blocked_status_code(403) is True
        assert ProxyFallbackChain.is_blocked_status_code(429) is True
        assert ProxyFallbackChain.is_blocked_status_code(503) is True
        assert ProxyFallbackChain.is_blocked_status_code(200) is False
        assert ProxyFallbackChain.is_blocked_status_code(404) is False
        assert ProxyFallbackChain.is_blocked_status_code(500) is False


class TestDomainStatus:
    def test_status_snapshot(self, full_chain):
        full_chain.report_blocked("site-a.com", PROXY_TOR)
        full_chain.report_success("site-b.com", PROXY_RESIDENTIAL)

        status = full_chain.get_domain_status()
        assert PROXY_TOR in status["blocked"]["site-a.com"]
        assert status["preferred"]["site-b.com"] == PROXY_RESIDENTIAL

    def test_is_blocked(self, full_chain):
        assert not full_chain.is_blocked("example.com", PROXY_TOR)
        full_chain.report_blocked("example.com", PROXY_TOR)
        assert full_chain.is_blocked("example.com", PROXY_TOR)
        assert not full_chain.is_blocked("example.com", PROXY_RESIDENTIAL)


class TestFallbackDisabled:
    def test_returns_tor_when_disabled(self):
        config = PrivacyConfig(
            proxy_type="tor",
            tor_socks_port=9050,
            residential_proxy="socks5://res:1080",
            fallback_enabled=False,
        )
        chain = ProxyFallbackChain(config)
        ptype, _ = chain.get_proxy_for_domain("example.com")
        assert ptype == PROXY_TOR


class TestThreadSafety:
    def test_concurrent_block_and_resolve(self, full_chain):
        """Multiple threads blocking and resolving should not crash."""
        errors = []

        def worker(domain_idx):
            try:
                domain = f"site-{domain_idx}.com"
                for _ in range(10):
                    full_chain.get_proxy_for_domain(domain)
                    full_chain.report_blocked(domain, PROXY_TOR)
                    try:
                        full_chain.get_proxy_for_domain(domain)
                    except AllProxiesBlockedError:
                        pass
                    full_chain.report_success(domain, PROXY_TOR)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Thread errors: {errors}"


class TestPerDomainIsolation:
    def test_blocking_one_domain_doesnt_affect_another(self, full_chain):
        full_chain.report_blocked("blocked.com", PROXY_TOR)
        full_chain.report_blocked("blocked.com", PROXY_RESIDENTIAL)
        full_chain.report_blocked("blocked.com", PROXY_DATACENTER)

        # Different domain should still work fine
        ptype, _ = full_chain.get_proxy_for_domain("fine.com")
        assert ptype == PROXY_TOR
