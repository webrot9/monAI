"""Resource monitoring — ensures agents don't degrade the creator's computer.

Tracks memory usage, disk usage, and CPU. Agents must stay within limits.
"""

from __future__ import annotations

import logging
import os
import resource
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Limits
MAX_MEMORY_MB = 2048  # Max 2GB RAM for monAI
MAX_DISK_MB = 5120    # Max 5GB disk usage in project + data dirs
WARN_MEMORY_PCT = 75  # Warn at 75% of max


def get_memory_usage_mb() -> float:
    """Get current process memory usage in MB."""
    # rusage maxrss is in KB on Linux
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_maxrss / 1024


def get_disk_usage_mb(path: Path) -> float:
    """Get disk usage of a directory in MB."""
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def check_resources(project_root: Path, data_dir: Path) -> dict[str, Any]:
    """Check all resource limits. Returns health status."""
    memory_mb = get_memory_usage_mb()
    project_disk_mb = get_disk_usage_mb(project_root)
    data_disk_mb = get_disk_usage_mb(data_dir)
    total_disk_mb = project_disk_mb + data_disk_mb

    memory_ok = memory_mb < MAX_MEMORY_MB
    disk_ok = total_disk_mb < MAX_DISK_MB
    memory_warning = memory_mb > (MAX_MEMORY_MB * WARN_MEMORY_PCT / 100)

    if not memory_ok:
        logger.warning(f"MEMORY LIMIT EXCEEDED: {memory_mb:.0f}MB / {MAX_MEMORY_MB}MB")
    if not disk_ok:
        logger.warning(f"DISK LIMIT EXCEEDED: {total_disk_mb:.0f}MB / {MAX_DISK_MB}MB")
    if memory_warning:
        logger.warning(f"Memory warning: {memory_mb:.0f}MB ({memory_mb/MAX_MEMORY_MB*100:.0f}%)")

    return {
        "memory_mb": round(memory_mb, 1),
        "memory_limit_mb": MAX_MEMORY_MB,
        "memory_ok": memory_ok,
        "memory_warning": memory_warning,
        "project_disk_mb": round(project_disk_mb, 1),
        "data_disk_mb": round(data_disk_mb, 1),
        "total_disk_mb": round(total_disk_mb, 1),
        "disk_limit_mb": MAX_DISK_MB,
        "disk_ok": disk_ok,
        "all_ok": memory_ok and disk_ok,
    }


def enforce_limits(project_root: Path, data_dir: Path) -> bool:
    """Check limits and return False if any are exceeded.

    Agents should call this before heavy operations and stop if False.
    """
    status = check_resources(project_root, data_dir)
    if not status["all_ok"]:
        logger.error(f"Resource limits exceeded: {status}")
        return False
    return True
