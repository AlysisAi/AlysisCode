"""Controller/real archive checks; actual guest/native qualification is separate."""

import threading
from uuid import uuid4

import pytest
from test_bots_cloud_desktop import CheckpointProvider
from test_bots_computer_control import setup
from test_bots_desktop_runtime import study as execution_study
from test_bots_idle_lifecycle import paused_store

from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputerSpec
from alysis_code.bots.computer_control import ControlRequest
from alysis_code.bots.desktop_linux import DesktopError
from alysis_code.bots.hosted_maintenance import MaintenanceLease
from alysis_code.bots.store import Conflict
from alysis_code.bots.workspace_archive import pack_workspace, restore_workspace
from alysis_code.bots.workspace_objects import WorkspaceObjects

study = execution_study


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_human_checkpoint_is_durable_before_resume_and_restores_after_failure(tmp_path, terminal):
    store, task, session = paused_store(tmp_path)
    with store.connect(write=True) as db:
        db.execute("UPDATE bot_computer_control SET state='returning'")
    source = tmp_path / "human-work"
    source.mkdir()
    data = "Human saved work — Αθήνα 東京\n".encode()
    (source / "notes.txt").write_bytes(data)
    archive = tmp_path / "saved.tar"
    pack_workspace(source, archive)
    provider = CheckpointProvider()
    provider.checkpoint = archive.read_bytes()
    computers = CloudDesktopFactory(
        "owner",
        provider,
        ComputerSpec("image"),
        tmp_path / "leases",
        persist_workspace=True,
        store=store,
    )
    computer = computers(task["bot_id"], tmp_path, cancel=lambda: False)
    computer.bind_task(task["id"])
    computer.checkpoint(session)
    assert computer.alive() and provider.status(computer.ref).state == "running"
    assert provider.actions[-1] == "workspace_checkpoint"
    saved = WorkspaceObjects(store).current("owner", task["bot_id"])
    assert saved is not None
    with store.connect(write=True) as db:
        db.execute("DELETE FROM bot_computer_control")
        db.execute("UPDATE tasks SET status=? WHERE id=?", (terminal, task["id"]))
    computer.close()
    assert provider.status(computer.ref).state == "missing"
    following = computers(task["bot_id"], tmp_path, cancel=lambda: False)
    assert provider.restored == [archive.read_bytes()]
    restored = tmp_path / "verified.tar"
    restored.write_bytes(b"".join(store.objects.read(saved)))
    destination = tmp_path / "restored"
    destination.mkdir(mode=0o700)
    restore_workspace(restored, destination)
    assert (destination / "notes.txt").read_bytes() == data
    following.close()


def test_corrupt_checkpoint_keeps_computer_and_previous_workspace(tmp_path):
    store, task, session = paused_store(tmp_path)
    previous = WorkspaceObjects(store).stage_pause(
        "owner", task["bot_id"], task["id"], session, [b"old"], 3
    )
    # Do not restore the intentionally opaque previous transport fixture.
    provider = CheckpointProvider()
    computers = CloudDesktopFactory(
        "owner",
        provider,
        ComputerSpec("image"),
        tmp_path / "leases",
        persist_workspace=True,
        store=store,
    )
    computer = computers(task["bot_id"], tmp_path, cancel=lambda: False)
    computer.bind_task(task["id"])
    provider.corrupt = True
    with pytest.raises(DesktopError, match="integrity"):
        computer.checkpoint(session)
    assert computer.alive() and WorkspaceObjects(store).current("owner", task["bot_id"]) == previous
    computer.close()


@pytest.mark.parametrize("outcome", ["success", "failure", "stop"])
def test_return_waits_for_checkpoint_without_holding_request_or_resuming_early(
    study, monkeypatch, outcome
):
    runtime, request, _, session = setup(study, monkeypatch)
    store, task, manager, instances = study
    request("take")
    runtime.computer_controls.park(task, [])
    view, _, _ = manager.views.current("owner", task["bot_id"])
    started, release = threading.Event(), threading.Event()

    def checkpoint(current):
        assert current == session
        assert runtime.background.claim() is None
        started.set()
        assert release.wait(5)
        if outcome == "failure":
            raise RuntimeError("Private storage failure")

    view.checkpoint = checkpoint
    decision = ControlRequest(action="resume", client_id=uuid4().hex, session_id=session)
    # An unexpired competing grant cannot return another window's computer.
    with pytest.raises(Conflict):
        runtime.return_computer(task["bot_id"], decision)
    request("disconnect")
    result = runtime.return_computer(task["bot_id"], decision)
    assert result["state"] == "returning" and started.wait(3)
    assert runtime.computer_controls.status(task["bot_id"])["state"] == "returning"
    assert runtime.background.claim() is None
    assert MaintenanceLease(store).acquire() is None
    with pytest.raises(Conflict):
        request("take")
    with runtime._computer_open_lock:
        worker = runtime._computer_openers[task["bot_id"]]
    try:
        if outcome == "stop":
            runtime.cancel(task["id"])
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    claimed = runtime.background.claim()
    if outcome == "success":
        assert claimed["id"] == task["id"]
        assert not manager.views.fenced("owner", task["bot_id"])
    else:
        assert claimed is None
        assert not instances[0].closed  # runtime cleanup, not this failed return, owns disposal
        assert "Private" not in runtime.computer_open_errors[task["bot_id"]]
        if outcome == "failure":
            assert runtime.computer_controls.status(task["bot_id"])["state"] == "paused"
            assert manager.views.fenced("owner", task["bot_id"])


def test_restart_during_handback_restores_pause_without_automatic_replay(study, monkeypatch):
    runtime, request, _, session = setup(study, monkeypatch)
    store, task, _, _ = study
    request("take")
    runtime.computer_controls.park(task, [])
    request("disconnect")
    runtime.computer_controls.begin_return(
        task["bot_id"], ControlRequest(action="resume", client_id=uuid4().hex, session_id=session)
    )
    runtime.computer_controls.recover()
    assert runtime.computer_controls.status(task["bot_id"])["state"] == "paused"
    assert runtime.background.claim() is None
