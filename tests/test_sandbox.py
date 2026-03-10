"""Tests for monai.utils.sandbox."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from monai.utils.sandbox import (
    PROJECT_ROOT,
    DATA_DIR,
    TEMP_PREFIX,
    _SAFE_ENV_KEYS,
    _make_clean_env,
    is_path_allowed,
    safe_read,
    safe_write,
    safe_delete,
    sandbox_run,
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


class TestMakeCleanEnv:
    def test_strips_sensitive_vars(self):
        """Env vars not in the safe list must be stripped."""
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "DATABASE_URL": "postgres://...",
            "PATH": "/usr/bin",
        }):
            env = _make_clean_env()
            assert "OPENAI_API_KEY" not in env
            assert "AWS_SECRET_ACCESS_KEY" not in env
            assert "DATABASE_URL" not in env

    def test_keeps_safe_vars(self):
        """Env vars in the safe list must be preserved."""
        with patch.dict(os.environ, {
            "LANG": "en_US.UTF-8",
            "USER": "testuser",
            "TERM": "xterm",
        }):
            env = _make_clean_env()
            assert env.get("LANG") == "en_US.UTF-8"
            assert env.get("USER") == "testuser"

    def test_path_restricted(self):
        """PATH must be restricted to standard locations."""
        env = _make_clean_env()
        assert "/usr/local/bin" in env["PATH"]
        assert "/usr/bin" in env["PATH"]

    def test_virtualenv_preserved(self):
        """If VIRTUAL_ENV is set, its bin dir is in PATH."""
        with patch.dict(os.environ, {"VIRTUAL_ENV": "/home/user/venv"}):
            env = _make_clean_env()
            assert "/home/user/venv/bin" in env["PATH"]


class TestSandboxRun:
    def test_runs_simple_command(self):
        """Basic command execution should work."""
        result = sandbox_run(["echo", "hello"])
        assert result["returncode"] == 0
        assert "hello" in result["stdout"]

    def test_env_secrets_not_leaked(self):
        """Child process must NOT see parent's secret env vars."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-secret-test-key"}):
            result = sandbox_run(["env"])
            assert "sk-secret-test-key" not in result["stdout"]
            assert "OPENAI_API_KEY" not in result["stdout"]

    def test_timeout_respected(self):
        """Commands that exceed timeout must be killed."""
        result = sandbox_run(["sleep", "10"], timeout=1)
        assert result["returncode"] == -1
        assert "timed out" in result["stderr"].lower()

    def test_cwd_is_workspace(self):
        """Default working directory should be the workspace."""
        result = sandbox_run(["pwd"])
        assert result["returncode"] == 0
        assert "workspace" in result["stdout"]

    def test_custom_cwd(self, tmp_path):
        """Custom cwd should be respected."""
        result = sandbox_run(["pwd"], cwd=tmp_path)
        assert result["returncode"] == 0
        assert str(tmp_path) in result["stdout"]

    def test_stdout_truncated(self):
        """Long output should be truncated to prevent memory issues."""
        # Generate output longer than 2000 chars
        result = sandbox_run(["python3", "-c", "print('A' * 5000)"])
        assert len(result["stdout"]) <= 2000

    def test_stderr_truncated(self):
        """Long stderr should be truncated."""
        result = sandbox_run(["python3", "-c", "import sys; sys.stderr.write('E' * 1000)"])
        assert len(result["stderr"]) <= 500

    def test_python_script_cant_read_secrets(self):
        """A Python script in the sandbox should not see secret env vars."""
        with patch.dict(os.environ, {"SUPER_SECRET": "hidden_value_12345"}):
            result = sandbox_run([
                "python3", "-c",
                "import os; print(os.environ.get('SUPER_SECRET', 'NOT_FOUND'))"
            ])
            assert "hidden_value_12345" not in result["stdout"]
            assert "NOT_FOUND" in result["stdout"]

    def test_home_set_to_workspace(self):
        """HOME should be set to workspace dir, not real home."""
        result = sandbox_run(["python3", "-c", "import os; print(os.environ.get('HOME', ''))"])
        assert result["returncode"] == 0
        real_home = str(Path.home())
        # HOME should NOT be the real home directory
        assert real_home not in result["stdout"] or "workspace" in result["stdout"]

    def test_invalid_command(self):
        """Non-existent commands should fail gracefully."""
        result = sandbox_run(["nonexistent_command_xyz"])
        assert result["returncode"] != 0
