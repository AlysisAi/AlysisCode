"""Native ownership and PID-reuse guards; use only disposable child processes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

import alysis_code.macos_process_tree as native
from alysis_code.terminal_ownership import _cleanup_owned_resource, seatbelt_scratch_cleanup_id

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native macOS process ownership")


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Native fixture did not reach its expected state")


@pytest.mark.parametrize("changed", ["reused", "unknown"])
def test_identity_change_never_authorizes_a_signal(monkeypatch, changed):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    tree = native.MacProcessTree.start(child.pid)
    assert tree is not None
    identity = native.process_identity(child.pid)
    assert identity is not None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                native,
                "process_identity",
                lambda _: (
                    replace(identity, start_usec=identity.start_usec + 1)
                    if changed == "reused"
                    else None
                ),
            )
            tree.signal(signal.SIGKILL)
            assert child.poll() is None
            assert tree.alive() is (changed == "unknown")
    finally:
        tree.close()
        child.kill()
        child.wait(timeout=5)


def test_observed_detached_child_remains_owned_after_parent_exits(tmp_path):
    ready = tmp_path / "ready.json"
    finish = tmp_path / "finish"
    code = f"""import json,subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'],start_new_session=True)
Path({str(ready)!r}).write_text(json.dumps({{'child':p.pid}}))
while not Path({str(finish)!r}).exists(): time.sleep(.01)
"""
    parent = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    tree = native.MacProcessTree.start(parent.pid)
    assert tree is not None
    try:
        wait_until(ready.exists)
        child = json.loads(ready.read_text())["child"]
        assert tree.alive()  # Observe the known parent-child relationship before exit.
        finish.touch()
        parent.wait(timeout=5)
        assert tree.alive()
        assert tree.terminate(0.1)
        wait_until(lambda: native.process_identity(child) is None)
        assert decoy.poll() is None
    finally:
        tree.terminate(0)
        tree.close()
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        decoy.kill()
        decoy.wait(timeout=5)


def test_zombie_is_finished_without_stealing_the_popen_exit_status():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    tree = native.MacProcessTree.start(child.pid)
    assert tree is not None
    try:
        child.kill()
        wait_until(lambda: not tree.alive())
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        tree.close()
        child.wait(timeout=5)


def test_recovery_removes_only_the_recorded_scratch_and_does_not_follow_links(tmp_path):
    canary = tmp_path / "canary"
    canary.write_text("unchanged")
    with tempfile.TemporaryDirectory(prefix="alysis-seatbelt-") as directory:
        path = Path(directory).resolve()
        (path / "link").symlink_to(tmp_path, target_is_directory=True)
        (path / "nested").mkdir()
        (path / "nested/file").write_text("disposable")
        payload = {
            "cleanup_kind": "seatbelt-scratch",
            "cleanup_id": seatbelt_scratch_cleanup_id(path),
        }
        assert _cleanup_owned_resource(payload)
        assert not path.exists()
        assert canary.read_text() == "unchanged"
        assert _cleanup_owned_resource(payload)


def test_recovery_refuses_replacement_directory(tmp_path):
    with tempfile.TemporaryDirectory(prefix="alysis-seatbelt-") as directory:
        path = Path(directory).resolve()
        payload = {
            "cleanup_kind": "seatbelt-scratch",
            "cleanup_id": seatbelt_scratch_cleanup_id(path),
        }
        moved = tmp_path / "original"
        path.rename(moved)
        path.mkdir()
        (path / "canary").write_text("replacement")
        assert not _cleanup_owned_resource(payload)
        assert (path / "canary").read_text() == "replacement"


def test_recovery_refuses_arbitrary_directory_even_with_valid_inode(tmp_path):
    (tmp_path / "canary").write_text("unchanged")
    assert not _cleanup_owned_resource(
        {
            "cleanup_kind": "seatbelt-scratch",
            "cleanup_id": seatbelt_scratch_cleanup_id(tmp_path),
        }
    )
    assert (tmp_path / "canary").read_text() == "unchanged"


def test_recovery_does_not_confirm_removal_when_a_child_disappears(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="alysis-seatbelt-") as directory:
        path = Path(directory).resolve()
        (path / "file").write_text("disposable")
        payload = {
            "cleanup_kind": "seatbelt-scratch",
            "cleanup_id": seatbelt_scratch_cleanup_id(path),
        }
        real_stat = os.stat

        def disappeared(target, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and target == "file":
                raise FileNotFoundError(target)
            return real_stat(target, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "stat", disappeared)
            assert not _cleanup_owned_resource(payload)
        assert path.exists()
        assert _cleanup_owned_resource(payload)


def test_start_refuses_processes_not_spawned_by_this_owner():
    assert native.MacProcessTree.start(os.getpid()) is None
    assert native.MacProcessTree.start(os.getpid(), expected_token="wrong") is None
