"""Daemon state management for monAI.

Persists cycle metrics, uptime, and error history to a JSON file.
Survives restarts. Used by the daemon loop and health endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from monai.config import CONFIG_DIR

logger = logging.getLogger(__name__)

STATE_FILE = CONFIG_DIR / "daemon_state.json"
PID_FILE = CONFIG_DIR / "monai.pid"


@dataclass
class CycleMetrics:
    """Metrics from a single cycle."""
    cycle: int = 0
    started_at: float = 0.0
    duration_secs: float = 0.0
    success: bool = False
    error: str = ""
    api_calls: int = 0
    api_cost_eur: float = 0.0
    strategies_run: int = 0
    net_profit: float = 0.0


@dataclass
class DaemonState:
    """Persistent daemon state across restarts."""
    pid: int = 0
    started_at: float = 0.0
    last_cycle_at: float = 0.0
    cycles_completed: int = 0
    cycles_failed: int = 0
    consecutive_failures: int = 0
    total_api_calls: int = 0
    total_api_cost_eur: float = 0.0
    total_net_profit: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    recent_cycles: list[dict[str, Any]] = field(default_factory=list)

    MAX_RECENT = 20  # Keep last N cycle summaries

    @classmethod
    def load(cls) -> DaemonState:
        """Load state from disk, or return fresh state."""
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                # Filter to known fields only
                known = {f.name for f in cls.__dataclass_fields__.values()}
                filtered = {k: v for k, v in data.items() if k in known}
                return cls(**filtered)
            except Exception as e:
                logger.warning(f"Could not load daemon state: {e}")
        return cls()

    def save(self) -> None:
        """Persist state to disk."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(asdict(self), indent=2))
        except Exception as e:
            logger.warning(f"Could not save daemon state: {e}")

    def record_cycle(self, metrics: CycleMetrics) -> None:
        """Record a completed cycle's metrics."""
        self.last_cycle_at = time.time()
        self.cycles_completed += 1
        self.total_api_calls += metrics.api_calls
        self.total_api_cost_eur += metrics.api_cost_eur
        self.total_net_profit += metrics.net_profit

        if metrics.success:
            self.consecutive_failures = 0
        else:
            self.cycles_failed += 1
            self.consecutive_failures += 1
            self.last_error = metrics.error
            self.last_error_at = time.time()

        # Keep recent cycle summaries
        self.recent_cycles.append(asdict(metrics))
        if len(self.recent_cycles) > self.MAX_RECENT:
            self.recent_cycles = self.recent_cycles[-self.MAX_RECENT:]

        self.save()

    def to_health(self) -> dict[str, Any]:
        """Return health summary for the /health endpoint."""
        now = time.time()
        uptime = now - self.started_at if self.started_at else 0
        since_last = now - self.last_cycle_at if self.last_cycle_at else 0

        # Healthy: last cycle within 15 min and no 5+ consecutive failures
        healthy = (
            self.started_at > 0
            and (self.last_cycle_at == 0 or since_last < 900)
            and self.consecutive_failures < 5
        )

        return {
            "status": "healthy" if healthy else "degraded",
            "pid": self.pid,
            "uptime_secs": int(uptime),
            "cycles_completed": self.cycles_completed,
            "cycles_failed": self.cycles_failed,
            "consecutive_failures": self.consecutive_failures,
            "last_cycle_secs_ago": int(since_last) if self.last_cycle_at else None,
            "total_api_cost_eur": round(self.total_api_cost_eur, 4),
            "total_net_profit": round(self.total_net_profit, 2),
            "last_error": self.last_error or None,
        }


# ── PID File ────────────────────────────────────────────────────

def acquire_pid() -> bool:
    """Write PID file. Returns False if another instance is running."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if process is still alive
            os.kill(old_pid, 0)
            # Process exists — another daemon is running
            return False
        except (ValueError, OSError):
            # PID file stale (process dead) — safe to overwrite
            pass

    PID_FILE.write_text(str(os.getpid()))
    return True


def release_pid() -> None:
    """Remove PID file on shutdown."""
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass
