"""Private computers and disk space (AL, 2026-11-07): realistic room to start, and a guard that
stops running computers cleanly before the device's disk fills, telling the owl why."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_bots_local_hcs import SPEC, guest, identity, provider

from alysis_code.bots.cloud_desktop import CloudDesktop
from alysis_code.bots.compute import ComputeCreationRefused, ComputerIdentity, ComputeStopped
from alysis_code.bots.desktop_linux import ComputerActionUncertain, DesktopError
from alysis_code.bots.local_resources import (
    DISK_CRITICAL,
    GIB,
    DiskGuard,
    disk_required,
    low_space_message,
    sweep_leftover_computers,
)


def wait_until(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_starting_needs_room_for_the_device_and_each_computer():
    assert disk_required(0) == 3 * GIB  # 2 GB for the device + 1 GB for this computer
    assert disk_required(1) == 4 * GIB
    assert DISK_CRITICAL == 1 * GIB


def test_guard_stops_running_computers_once_space_is_critical():
    readings = iter([5 * GIB, 4 * GIB, int(0.6 * GIB)])
    running, stops = [True], []

    def stop_all(message):
        stops.append(message)
        running[0] = False

    guard = DiskGuard(
        Path("."), lambda: running[0], stop_all, interval=0.01, free=lambda _: next(readings, 0)
    )
    guard.watch()
    wait_until(lambda: guard._thread is None)
    assert len(stops) == 1
    assert "almost out of disk space (0.6 GB free)" in stops[0]
    assert "Files saved before this task are kept" in stops[0]


def test_guard_leaves_computers_alone_with_enough_space_and_sleeps_when_none_run():
    checks, stops = [], []
    running = threading.Event()
    running.set()

    def free(_):
        checks.append(1)
        if len(checks) >= 3:
            running.clear()
        return 10 * GIB

    guard = DiskGuard(Path("."), running.is_set, stops.append, interval=0.01, free=free)
    guard.watch()
    wait_until(lambda: guard._thread is None)
    assert stops == [] and len(checks) >= 3
    running.set()
    guard.watch()  # the next computer starts it again
    assert guard._thread is not None
    guard.close()


def test_windows_start_uses_the_realistic_reservation(tmp_path, monkeypatch):
    computers = provider(tmp_path, monkeypatch)
    guest(monkeypatch)
    monkeypatch.setattr(computers, "_watch_disk", lambda: None)
    monkeypatch.setattr(
        "alysis_code.bots.local_hcs.shutil.disk_usage",
        lambda _: SimpleNamespace(free=int(2.9 * GIB)),
    )
    with pytest.raises(ComputeCreationRefused) as refused:
        computers.create(identity(), SPEC)
    assert "needs about 3 GB free and this device has 2.9 GB" in str(refused.value)
    monkeypatch.setattr(
        "alysis_code.bots.local_hcs.shutil.disk_usage",
        lambda _: SimpleNamespace(free=int(3.1 * GIB)),
    )
    assert computers.status(computers.create(identity(), SPEC)).state == "running"


def test_windows_computer_stopped_for_space_says_why(tmp_path, monkeypatch):
    computers = provider(tmp_path, monkeypatch)
    guest(monkeypatch)
    monkeypatch.setattr(computers, "_watch_disk", lambda: None)
    ref = computers.create(identity(), SPEC)
    computers._stop_for_space(low_space_message(int(0.8 * GIB)))
    status = computers.status(ref)
    assert status.state == "stopped" and "0.8 GB free" in status.raw_state
    with pytest.raises(ComputeStopped, match="almost out of disk space"):
        computers.run(ref, ["/usr/bin/true"])
    computers.delete(ref)
    assert computers._space_stop(ref.resource_id) is None


def test_a_space_stop_reaches_the_owl_in_its_own_words():
    desktop = object.__new__(CloudDesktop)
    desktop._lock = threading.RLock()
    desktop._closed = False
    desktop._stop = threading.Event()
    desktop._shared_ledger = None
    desktop.identity = ComputerIdentity("owner", "bot", uuid4().hex)
    desktop.owner = "bot"
    desktop.ref = object()
    phases, closed = [], []
    desktop._lifecycle = lambda bot, phase: phases.append(phase)
    desktop.close = lambda: closed.append(True)

    class Provider:
        def run(self, ref, argv, *, timeout):
            raise ComputeStopped(low_space_message(int(0.7 * GIB)))

    desktop.provider = Provider()
    with pytest.raises(DesktopError) as error:
        desktop._request("observe", {}, cancel=lambda: False)
    # Refused before dispatch: a plain, explainable error, not an "outcome unknown" stop.
    assert not isinstance(error.value, ComputerActionUncertain)
    assert "almost out of disk space (0.7 GB free)" in str(error.value)
    assert phases == ["failed"] and closed == [True]


def test_start_removes_computers_a_crash_left_behind(tmp_path):
    crashed = tmp_path / ("a" * 32)
    (crashed / "nested").mkdir(parents=True)
    (crashed / "nested" / "disk.vhdx").write_bytes(b"child disk")
    other = tmp_path / "not-a-computer"
    other.mkdir()
    (tmp_path / ("b" * 32)).write_text("a file, not a computer folder")
    assert sweep_leftover_computers(tmp_path) == ["a" * 32]
    assert not crashed.exists() and other.exists() and (tmp_path / ("b" * 32)).exists()
    assert sweep_leftover_computers(tmp_path / "missing") == []


def test_local_providers_say_a_missing_computer_is_really_gone():
    from alysis_code.bots.local_hcs import WindowsVmComputers
    from alysis_code.bots.local_vm import LocalVmComputers

    assert WindowsVmComputers.find_is_complete and LocalVmComputers.find_is_complete
