"""Filesystem sandbox — agents CANNOT escape their allowed directories.

This is an absolute rule. Agents can only operate within:
1. The monAI project directory (where the code lives)
2. ~/.monai/ (data directory)
3. /tmp/monai-* (temporary files)

NOTHING else on the creator's computer. Ever.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolve the project root (monAI repo root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = Path.home() / ".monai"
TEMP_PREFIX = "/tmp/monai-"

ALLOWED_ROOTS = [
    PROJECT_ROOT,
    DATA_DIR,
]


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

        # Check temp directory
        if str(resolved).startswith(TEMP_PREFIX):
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


def _dir_size_mb(path: Path) -> float:
    """Get directory size in MB."""
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return round(total / (1024 * 1024), 2)
