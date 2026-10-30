from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from alysis_code import durable_service_manager as dsm
from alysis_code.sandbox_settings import ShellSandboxSettings


def manager_at(root: Path) -> dsm.DurableServiceManager:
    return dsm.DurableServiceManager(
        root=root, state_dir=root / "services", settings=ShellSandboxSettings(mode="off")
    )


class CleanupProcess:
    def __init__(self, pid: int, created: float, *, ignores_signals: bool = False):
        self.pid = pid
        self.created = created
        self.alive = True
        self.ignores_signals = ignores_signals
        self.descendants = []
        self.signals = []

    def create_time(self):
        return self.created

    def is_running(self):
        return self.alive

    def children(self, recursive=False):
        assert recursive
        return self.descendants

    def terminate(self):
        self.signals.append("terminate")
        if not self.ignores_signals:
            self.alive = False

    def kill(self):
        self.signals.append("kill")
        if not self.ignores_signals:
            self.alive = False

    def status(self):
        return psutil.STATUS_RUNNING


@pytest.fixture
def incomplete_cleanup(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    leader = CleanupProcess(101, 100.0)
    child = CleanupProcess(102, 101.0, ignores_signals=True)
    leader.descendants = [child]
    processes = {leader.pid: leader, child.pid: child}
    metadata = {
        "service_id": "svc_pending",
        "ownership": "DURABLE_SERVICE",
        "root": str(tmp_path.resolve()),
        "backend": "host",
        "pid": leader.pid,
        "pid_start_token": "owned-leader",
        "readiness": {"type": "process_alive"},
    }
    manager._write_metadata(metadata)
    monkeypatch.setattr(dsm.psutil, "Process", lambda pid: processes[pid])
    monkeypatch.setattr(dsm, "_pid_start_token", lambda _pid: "owned-leader")
    monkeypatch.setattr(dsm, "_pid_exists", lambda pid: processes[pid].alive)
    monkeypatch.setattr(
        dsm.psutil,
        "wait_procs",
        lambda candidates, timeout: (
            [process for process in candidates if not process.alive],
            [process for process in candidates if process.alive],
        ),
    )
    return manager, leader, child, processes


def test_pending_descendant_retains_identity_and_resumed_cleanup_after_leader_exit(
    incomplete_cleanup, monkeypatch
):
    manager, leader, child, _ = incomplete_cleanup
    terminate = leader.terminate

    def observe_durable_identity_before_first_signal():
        saved = manager._read_metadata("svc_pending")
        assert saved["cleanup_pending"] is True
        assert {record["pid"] for record in saved["cleanup_processes"]} == {101, 102}
        terminate()

    monkeypatch.setattr(leader, "terminate", observe_durable_identity_before_first_signal)
    first = manager.stop("svc_pending")
    assert first["stopped"] is False
    assert first["cleanup_pending"] is True
    assert child.signals == ["terminate", "kill"]
    assert manager._read_metadata("svc_pending")["cleanup_processes"] == [
        {"pid": 102, "create_time": 101.0}
    ]
    assert leader.alive is False
    child.ignores_signals = False
    fresh = manager_at(manager.root)
    assert fresh.stop("svc_pending")["stopped"] is True
    assert child.alive is False
    assert fresh._read_metadata("svc_pending") is None


def test_resumed_cleanup_never_signals_reused_descendant_pid(incomplete_cleanup):
    manager, _, child, processes = incomplete_cleanup
    assert manager.stop("svc_pending")["stopped"] is False
    replacement = CleanupProcess(child.pid, 999.0)
    processes[child.pid] = replacement
    fresh = manager_at(manager.root)
    assert fresh.stop("svc_pending")["stopped"] is True
    assert replacement.alive is True
    assert replacement.signals == []


def test_resumed_cleanup_never_signals_reused_leader_pid(incomplete_cleanup):
    manager, leader, child, processes = incomplete_cleanup
    assert manager.stop("svc_pending")["stopped"] is False
    replacement = CleanupProcess(leader.pid, 999.0)
    processes[leader.pid] = replacement
    # Match the real creation-token mismatch for a recycled leader PID.
    original_token = dsm._pid_start_token
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            dsm,
            "_pid_start_token",
            lambda pid: "unrelated" if pid == leader.pid else original_token(pid),
        )
        child.ignores_signals = False
        assert manager_at(manager.root).stop("svc_pending")["stopped"] is True
    assert child.alive is False
    assert replacement.alive is True
    assert replacement.signals == []


def test_uninspectable_retained_descendant_keeps_cleanup_pending(incomplete_cleanup, monkeypatch):
    manager, leader, child, _ = incomplete_cleanup
    assert manager.stop("svc_pending")["stopped"] is False

    def process(pid):
        if pid == child.pid:
            raise psutil.AccessDenied(pid)
        return leader

    monkeypatch.setattr(dsm.psutil, "Process", process)
    fresh = manager_at(manager.root)
    result = fresh.stop("svc_pending")
    assert result["stopped"] is False
    assert result["cleanup_pending"] is True
    assert fresh._read_metadata("svc_pending")["cleanup_processes"] == [
        {"pid": child.pid, "create_time": child.created}
    ]


def test_failure_to_persist_cleanup_inventory_prevents_signalling(incomplete_cleanup, monkeypatch):
    manager, leader, child, _ = incomplete_cleanup

    def disk_failure(_metadata):
        raise OSError("cleanup inventory cannot be persisted")

    monkeypatch.setattr(manager, "_write_metadata", disk_failure)
    with pytest.raises(OSError, match="cannot be persisted"):
        manager.stop("svc_pending")
    assert leader.signals == child.signals == []
    assert leader.alive and child.alive


def test_real_orphaned_child_is_retained_and_stopped_by_fresh_manager(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    child_pid_path = tmp_path / "child.pid"
    child_code = (
        "import os,time; from pathlib import Path; "
        f"ready=Path({str(child_pid_path)!r}); pending=ready.with_suffix('.pending'); "
        "pending.write_text(str(os.getpid())); pending.replace(ready); time.sleep(15)"
    )
    code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); time.sleep(15)"
    )
    # Windows virtualenv redirectors own a kill-on-close job for their Python
    # children. Use the underlying interpreter to exercise an actual orphan.
    argv = [getattr(sys, "_base_executable", sys.executable), "-c", code]
    cmd = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    started = manager.start(cmd=cmd, cwd=tmp_path)
    child = None
    try:
        until = time.monotonic() + 4
        # Publish only the complete PID; file creation alone races write_text.
        while not child_pid_path.exists() and time.monotonic() < until:
            time.sleep(0.02)
        child = psutil.Process(int(child_pid_path.read_text()))
        terminate, kill = psutil.Process.terminate, psutil.Process.kill
        with monkeypatch.context() as patch:
            patch.setattr(dsm, "_STOP_TIMEOUT_S", 0.1)
            patch.setattr(dsm, "_KILL_TIMEOUT_S", 0.1)
            patch.setattr(
                psutil.Process,
                "terminate",
                lambda process: None if process.pid == child.pid else terminate(process),
            )
            patch.setattr(
                psutil.Process,
                "kill",
                lambda process: None if process.pid == child.pid else kill(process),
            )
            result = manager.stop(started.service_id)
        assert result["stopped"] is False
        assert result["cleanup_pending"] is True
        assert child.is_running()
        saved = manager._read_metadata(started.service_id)
        assert {record["pid"] for record in saved["cleanup_processes"]} == {child.pid}
        fresh = manager_at(tmp_path)
        assert fresh.stop(started.service_id)["stopped"] is True
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        assert fresh._read_metadata(started.service_id) is None
    finally:
        manager.stop(started.service_id)
        if child is not None and child.is_running():
            child.kill()
            psutil.wait_procs([child], timeout=2)
