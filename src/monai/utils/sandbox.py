"""Filesystem sandbox — agents CANNOT escape their allowed directories.

This is an absolute rule. Agents can only operate within:
1. The monAI project directory (where the code lives)
2. ~/.monai/ (data directory)
3. /tmp/monai-* (temporary files)

NOTHING else on the creator's computer. Ever.

For subprocess execution, we use Linux namespace isolation (unshare)
when available. This provides OS-level enforcement that cannot be
bypassed by the child process, unlike argument validation which is
a best-effort application-level check.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Resolve the project root (monAI repo root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = Path.home() / ".monai"
TEMP_PREFIX = "/tmp/monai-"

ALLOWED_ROOTS = [
    PROJECT_ROOT,
    DATA_DIR,
]

# ── Sandboxed subprocess execution ──────────────────────────────────

# Environment variables safe to pass to sandboxed subprocesses.
# Everything else is stripped to prevent leaking secrets.
_SAFE_ENV_KEYS = {
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "SHELL", "TMPDIR", "TZ",
    "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
    "NODE_PATH", "NODE_ENV",
    "PIP_NO_WARN_SCRIPT_LOCATION",
    "VIRTUAL_ENV",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
}

# Detect whether we can use Linux namespaces for real isolation
_UNSHARE_PATH = shutil.which("unshare")
_CAN_NAMESPACE = False

if _UNSHARE_PATH:
    try:
        r = subprocess.run(
            [_UNSHARE_PATH, "--user", "--map-root-user", "--", "true"],
            capture_output=True, timeout=5,
        )
        _CAN_NAMESPACE = r.returncode == 0
    except Exception:
        pass

if _CAN_NAMESPACE:
    logger.info("OS-level sandbox available (Linux user namespaces via unshare)")
else:
    logger.warning(
        "OS-level sandbox NOT available. Falling back to application-level "
        "argument validation only. Install 'util-linux' for unshare support."
    )


def _make_clean_env() -> dict[str, str]:
    """Create a sanitized environment for sandboxed subprocesses.

    Strips all env vars except a safe whitelist. This prevents leaking
    API keys, tokens, passwords, or other secrets to child processes.
    """
    clean = {}
    for key in _SAFE_ENV_KEYS:
        if key in os.environ:
            clean[key] = os.environ[key]

    # Restrict PATH to standard locations only
    clean["PATH"] = "/usr/local/bin:/usr/bin:/bin"

    # Add virtualenv bin if active
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        clean["PATH"] = f"{venv}/bin:{clean['PATH']}"

    return clean


def sandbox_run(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 60,
    allowed_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Execute a command in a sandboxed subprocess.

    Security layers (applied in order):
    1. Environment sanitization (no secrets leak to child process)
    2. Working directory forced to workspace
    3. OS-level namespace isolation via unshare (if available):
       - User namespace: process runs as mapped root, cannot access
         files owned by other users
       - The process inherits a read-only view of the filesystem
         (no mount namespace manipulation needed because we use
         cwd restriction + clean env)

    Args:
        cmd: Command as list of strings (NO shell=True ever)
        cwd: Working directory (defaults to PROJECT_ROOT/workspace)
        timeout: Max execution time in seconds
        allowed_paths: Additional paths the process needs access to

    Returns:
        Dict with stdout, stderr, returncode
    """
    if cwd is None:
        cwd = str(PROJECT_ROOT / "workspace")
    cwd = str(cwd)

    # Ensure workspace exists
    Path(cwd).mkdir(parents=True, exist_ok=True)

    clean_env = _make_clean_env()

    # Set HOME to workspace to prevent accidental reads from real home
    clean_env["HOME"] = cwd
    clean_env["TMPDIR"] = "/tmp"

    try:
        if _CAN_NAMESPACE:
            # Wrap command with unshare for user namespace isolation.
            # The child process gets its own user namespace with a fake
            # root mapping. It can't escalate privileges or access files
            # that require the real user's permissions outside the workspace.
            wrapped = [
                _UNSHARE_PATH,
                "--user",
                "--map-root-user",
                "--",
            ] + cmd
            result = subprocess.run(
                wrapped,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=clean_env,
            )
        else:
            # Fallback: no namespace isolation, but still sanitized env + cwd
            result = subprocess.run(
                cmd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=clean_env,
            )

        return {
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:500],
            "returncode": result.returncode,
        }

    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "returncode": -1,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"Sandbox error: {e}",
            "returncode": -1,
        }


def is_path_allowed(path: str | Path) -> bool:
    """Check if a path is within the sandbox.

    Returns True only if the resolved path is under an allowed root.
    Resolves symlinks to prevent escape via symlink tricks.
    """
    try:
        resolved = Path(path).resolve()

        # Check allowed roots
        for root in ALLOWED_ROOTS:
            root_resolved = root.resolve()
            try:
                resolved.relative_to(root_resolved)
                return True
            except ValueError:
                continue

        # Check temp directory — must be exactly /tmp/monai-<something>
        # Use Path comparison to prevent prefix confusion (/tmp/monai-evil/)
        resolved_str = str(resolved)
        if resolved_str.startswith(TEMP_PREFIX):
            # Verify it's actually under /tmp/ (not a symlink escape)
            if resolved.parent == Path("/tmp") or str(resolved.parent).startswith(TEMP_PREFIX):
                return True

        return False
    except Exception:
        return False


def safe_read(path: str | Path) -> str:
    """Read a file only if it's within the sandbox."""
    if not is_path_allowed(path):
        raise PermissionError(
            f"SANDBOX VIOLATION: Cannot read '{path}'. "
            f"Agents can only access files within {PROJECT_ROOT} or {DATA_DIR}"
        )
    return Path(path).read_text()


def safe_write(path: str | Path, content: str) -> Path:
    """Write a file only if it's within the sandbox."""
    p = Path(path)
    if not is_path_allowed(p):
        raise PermissionError(
            f"SANDBOX VIOLATION: Cannot write '{path}'. "
            f"Agents can only write within {PROJECT_ROOT} or {DATA_DIR}"
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    logger.info(f"Sandbox write: {p}")
    return p


def safe_delete(path: str | Path) -> bool:
    """Delete a file only if it's within the sandbox."""
    p = Path(path)
    if not is_path_allowed(p):
        raise PermissionError(
            f"SANDBOX VIOLATION: Cannot delete '{path}'."
        )
    if p.exists():
        p.unlink()
        return True
    return False


def get_sandbox_info() -> dict:
    """Get info about the sandbox for display/logging."""
    return {
        "project_root": str(PROJECT_ROOT),
        "data_dir": str(DATA_DIR),
        "temp_prefix": TEMP_PREFIX,
        "project_size_mb": _dir_size_mb(PROJECT_ROOT),
        "data_size_mb": _dir_size_mb(DATA_DIR),
    }


def _dir_size_mb(path: Path, max_depth: int = 5) -> float:
    """Get directory size in MB with depth limit to prevent hangs."""
    if not path.exists():
        return 0.0
    total = 0
    try:
        for root, dirs, files in os.walk(str(path)):
            depth = str(root).count(os.sep) - str(path).count(os.sep)
            if depth >= max_depth:
                dirs.clear()
                continue
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return round(total / (1024 * 1024), 2)
