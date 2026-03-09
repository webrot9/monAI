"""Tests for monai.utils.resources."""

from pathlib import Path

from monai.utils.resources import (
    MAX_DISK_MB,
    MAX_MEMORY_MB,
    check_resources,
    enforce_limits,
    get_disk_usage_mb,
    get_memory_usage_mb,
)


class TestGetMemoryUsageMb:
    def test_returns_positive_float(self):
        usage = get_memory_usage_mb()
        assert isinstance(usage, float)
        assert usage > 0


class TestGetDiskUsageMb:
    def test_existing_directory(self, tmp_path):
        # Create a file with known size
        (tmp_path / "test.bin").write_bytes(b"\x00" * 1024 * 1024)  # 1MB
        usage = get_disk_usage_mb(tmp_path)
        assert usage >= 0.9  # Allow some filesystem overhead
        assert usage <= 1.5

    def test_nonexistent_directory(self):
        usage = get_disk_usage_mb(Path("/nonexistent/path"))
        assert usage == 0.0

    def test_empty_directory(self, tmp_path):
        usage = get_disk_usage_mb(tmp_path)
        assert usage == 0.0


class TestCheckResources:
    def test_returns_expected_keys(self, tmp_path):
        status = check_resources(tmp_path, tmp_path)
        assert "memory_mb" in status
        assert "memory_ok" in status
        assert "disk_ok" in status
        assert "all_ok" in status
        assert "memory_limit_mb" in status
        assert status["memory_limit_mb"] == MAX_MEMORY_MB
        assert status["disk_limit_mb"] == MAX_DISK_MB

    def test_small_dirs_pass(self, tmp_path):
        status = check_resources(tmp_path, tmp_path)
        assert status["disk_ok"] is True
        assert status["total_disk_mb"] < MAX_DISK_MB


class TestEnforceLimits:
    def test_passes_within_limits(self, tmp_path):
        assert enforce_limits(tmp_path, tmp_path) is True
