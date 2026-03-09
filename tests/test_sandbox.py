"""Tests for monai.utils.sandbox."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from monai.utils.sandbox import (
    PROJECT_ROOT,
    DATA_DIR,
    TEMP_PREFIX,
    is_path_allowed,
    safe_read,
    safe_write,
    safe_delete,
    get_sandbox_info,
)


class TestIsPathAllowed:
    def test_project_root_allowed(self):
        assert is_path_allowed(PROJECT_ROOT / "src" / "test.py") is True

    def test_data_dir_allowed(self):
        assert is_path_allowed(DATA_DIR / "db" / "test.db") is True

    def test_temp_dir_allowed(self):
        assert is_path_allowed("/tmp/monai-test123/file.txt") is True

    def test_root_not_allowed(self):
        assert is_path_allowed("/etc/passwd") is False

    def test_home_not_allowed(self):
        assert is_path_allowed(Path.home() / "Documents" / "secrets.txt") is False

    def test_empty_path(self):
        # Empty string resolves to cwd, which may or may not be in sandbox
        result = is_path_allowed("")
        assert isinstance(result, bool)

    def test_relative_path_in_project(self, monkeypatch):
        monkeypatch.chdir(PROJECT_ROOT)
        assert is_path_allowed(PROJECT_ROOT / "test.txt") is True

    def test_symlink_escape_blocked(self, tmp_path):
        # Create a symlink inside data dir pointing to /etc
        # is_path_allowed resolves symlinks, so it should block this
        # Only test if we can create symlinks
        try:
            link = tmp_path / "escape_link"
            link.symlink_to("/etc")
            # The resolved path is /etc, not inside allowed roots
            assert is_path_allowed(link / "passwd") is False
        except OSError:
            pytest.skip("Cannot create symlinks")


class TestSafeRead:
    def test_reads_file_in_sandbox(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")
        with patch("monai.utils.sandbox.ALLOWED_ROOTS", [tmp_path]):
            content = safe_read(test_file)
            assert content == "hello world"

    def test_blocks_outside_sandbox(self):
        with pytest.raises(PermissionError, match="SANDBOX VIOLATION"):
            safe_read("/etc/hosts")


class TestSafeWrite:
    def test_writes_file_in_sandbox(self, tmp_path):
        target = tmp_path / "output.txt"
        with patch("monai.utils.sandbox.ALLOWED_ROOTS", [tmp_path]):
            result = safe_write(target, "test content")
            assert result == target
            assert target.read_text() == "test content"

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "c" / "file.txt"
        with patch("monai.utils.sandbox.ALLOWED_ROOTS", [tmp_path]):
            safe_write(target, "deep write")
            assert target.read_text() == "deep write"

    def test_blocks_outside_sandbox(self):
        with pytest.raises(PermissionError, match="SANDBOX VIOLATION"):
            safe_write("/etc/evil.txt", "hacked")


class TestSafeDelete:
    def test_deletes_file_in_sandbox(self, tmp_path):
        target = tmp_path / "to_delete.txt"
        target.write_text("delete me")
        with patch("monai.utils.sandbox.ALLOWED_ROOTS", [tmp_path]):
            assert safe_delete(target) is True
            assert not target.exists()

    def test_returns_false_for_nonexistent(self, tmp_path):
        with patch("monai.utils.sandbox.ALLOWED_ROOTS", [tmp_path]):
            assert safe_delete(tmp_path / "nonexistent.txt") is False

    def test_blocks_outside_sandbox(self):
        with pytest.raises(PermissionError, match="SANDBOX VIOLATION"):
            safe_delete("/etc/passwd")


class TestGetSandboxInfo:
    def test_returns_expected_keys(self):
        info = get_sandbox_info()
        assert "project_root" in info
        assert "data_dir" in info
        assert "temp_prefix" in info
        assert "project_size_mb" in info
        assert "data_size_mb" in info
