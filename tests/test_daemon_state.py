"""Tests for daemon state management."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from monai.infra.daemon_state import (
    CycleMetrics,
    DaemonState,
    acquire_pid,
    release_pid,
)


@pytest.fixture(autouse=True)
def isolate_state(tmp_path):
    """Redirect state and PID files to tmp for test isolation."""
    state_file = tmp_path / "daemon_state.json"
    pid_file = tmp_path / "monai.pid"
    with patch("monai.infra.daemon_state.STATE_FILE", state_file), \
         patch("monai.infra.daemon_state.PID_FILE", pid_file):
        yield state_file, pid_file


class TestDaemonState:
    def test_fresh_state_has_defaults(self):
        state = DaemonState.load()
        assert state.cycles_completed == 0
        assert state.cycles_failed == 0
        assert state.total_api_cost_eur == 0.0

    def test_save_and_load_round_trips(self, isolate_state):
        state_file, _ = isolate_state
        state = DaemonState(pid=1234, started_at=1000.0, cycles_completed=5)
        state.save()

        loaded = DaemonState.load()
        assert loaded.pid == 1234
        assert loaded.started_at == 1000.0
        assert loaded.cycles_completed == 5

    def test_record_successful_cycle(self):
        state = DaemonState()
        metrics = CycleMetrics(
            cycle=1, started_at=time.time(), duration_secs=10.5,
            success=True, api_calls=42, api_cost_eur=0.05,
            strategies_run=1, net_profit=2.50,
        )
        state.record_cycle(metrics)

        assert state.cycles_completed == 1
        assert state.cycles_failed == 0
        assert state.consecutive_failures == 0
        assert state.total_api_calls == 42
        assert state.total_api_cost_eur == pytest.approx(0.05)
        assert state.total_net_profit == pytest.approx(2.50)
        assert len(state.recent_cycles) == 1

    def test_record_failed_cycle_tracks_errors(self):
        state = DaemonState()
        metrics = CycleMetrics(
            cycle=1, started_at=time.time(), duration_secs=5.0,
            success=False, error="llm_quota_exhausted",
        )
        state.record_cycle(metrics)

        assert state.cycles_failed == 1
        assert state.consecutive_failures == 1
        assert state.last_error == "llm_quota_exhausted"

    def test_success_resets_consecutive_failures(self):
        state = DaemonState(consecutive_failures=3, cycles_failed=3)
        metrics = CycleMetrics(cycle=4, success=True, started_at=time.time())
        state.record_cycle(metrics)

        assert state.consecutive_failures == 0
        assert state.cycles_failed == 3  # Total stays

    def test_recent_cycles_capped(self):
        state = DaemonState()
        for i in range(30):
            state.record_cycle(CycleMetrics(
                cycle=i, success=True, started_at=time.time(),
            ))
        assert len(state.recent_cycles) == DaemonState.MAX_RECENT

    def test_health_healthy(self):
        state = DaemonState(
            pid=os.getpid(),
            started_at=time.time() - 3600,
            last_cycle_at=time.time() - 60,
            cycles_completed=10,
            consecutive_failures=0,
        )
        health = state.to_health()
        assert health["status"] == "healthy"
        assert health["cycles_completed"] == 10
        assert health["consecutive_failures"] == 0

    def test_health_degraded_on_many_failures(self):
        state = DaemonState(
            started_at=time.time(), consecutive_failures=5,
        )
        assert state.to_health()["status"] == "degraded"

    def test_health_degraded_on_stale_cycle(self):
        state = DaemonState(
            started_at=time.time() - 7200,
            last_cycle_at=time.time() - 1800,  # 30 min ago > 15 min threshold
        )
        assert state.to_health()["status"] == "degraded"

    def test_corrupted_file_returns_fresh(self, isolate_state):
        state_file, _ = isolate_state
        state_file.write_text("not valid json")
        state = DaemonState.load()
        assert state.cycles_completed == 0


class TestPIDFile:
    def test_acquire_and_release(self, isolate_state):
        _, pid_file = isolate_state
        assert acquire_pid() is True
        assert pid_file.exists()
        assert pid_file.read_text().strip() == str(os.getpid())

        release_pid()
        assert not pid_file.exists()

    def test_stale_pid_overwritten(self, isolate_state):
        _, pid_file = isolate_state
        # Write a PID that doesn't exist
        pid_file.write_text("999999999")
        assert acquire_pid() is True

    def test_running_pid_blocks(self, isolate_state):
        _, pid_file = isolate_state
        # Write our own PID — simulating another running instance
        pid_file.write_text(str(os.getpid()))
        assert acquire_pid() is False
