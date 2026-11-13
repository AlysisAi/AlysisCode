"""Idle pauses preserve verified storage without completing or waking a task."""

import json
import sqlite3
import threading
from uuid import uuid4

import pytest
from test_bots_compute import AzureWire
from test_bots_computer_control import setup
from test_bots_desktop_runtime import study as execution_study

from alysis_code.bots.browser_state import BrowserStates
from alysis_code.bots.checkpoint_migration import migrate_checkpoints
from alysis_code.bots.compute import ComputeError, ComputeUncertain
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.store import Conflict, Store
from alysis_code.bots.workspace_objects import WorkspaceObjects

study = execution_study


def test_input_resets_idle_but_viewer_heartbeat_does_not_resume_or_extend_it(study, monkeypatch):
    runtime, request, _, _ = setup(study, monkeypatch)
    store, task, manager, instances = study
    now = [1000.0]
    runtime.computer_controls.wall_clock = lambda: now[0]
    request("take")
    runtime.computer_controls.park(task, [])
    now[0] += 290
    request("heartbeat")
    runtime.computer_controls.expire_idle(runtime._suspend_computer)
    assert not instances[0].closed
    request("input", sequence=1, event={"kind": "text", "text": "Καλημέρα 東京"})
    now[0] += 290
    request("heartbeat")
    runtime.computer_controls.expire_idle(runtime._suspend_computer)
    assert not instances[0].closed
    now[0] += 11
    runtime.computer_controls.expire_idle(runtime._suspend_computer)
    assert instances[0].closed
    state = runtime.computer_controls.status(task["bot_id"])
    assert state["idle_suspended"] and state["computer_lost"]
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert runtime.background.claim() is None
    request("resume")
    assert runtime.background.claim()["id"] == task["id"]


def test_expiry_io_does_not_hold_control_lock_and_stop_wins(study, monkeypatch):
    runtime, request, _, _ = setup(study, monkeypatch)
    store, task, _, _ = study
    now = [1000.0]
    runtime.computer_controls.wall_clock = lambda: now[0]
    request("take")
    runtime.computer_controls.park(task, [])
    now[0] += 301
    started, release = threading.Event(), threading.Event()

    def suspend(*_):
        started.set()
        assert release.wait(3)

    thread = threading.Thread(target=runtime.computer_controls.expire_idle, args=(suspend,))
    thread.start()
    try:
        assert started.wait(3)
        assert runtime.computer_controls.status(task["bot_id"])["state"] == "suspending"
        with pytest.raises(Conflict):
            request("resume")
        with pytest.raises(Conflict):
            request("input", sequence=1, event={"kind": "text", "text": "too late"})
        runtime.cancel(task["id"])
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    final = store.task("owner", task["id"])
    assert final["status"] == "cancelled"
    assert not any(e["kind"] == "computer_idle_suspended" for e in final["events"])


def test_idle_disposal_does_not_require_guest_input_dispatch(study, monkeypatch):
    runtime, request, _, _ = setup(study, monkeypatch)
    store, task, manager, instances = study
    now = [1000.0]
    runtime.computer_controls.wall_clock = lambda: now[0]
    request("take")
    runtime.computer_controls.park(task, [])
    view, _, _ = manager.views.current("owner", task["bot_id"])
    dispatched = []

    def unavailable(event):
        dispatched.append(event)
        raise RuntimeError("Guest input unavailable")

    view.human_input = unavailable
    now[0] += 301
    runtime.computer_controls.expire_idle(runtime._suspend_computer)
    assert instances[0].closed
    assert dispatched == []
    assert runtime.computer_controls.status(task["bot_id"])["idle_suspended"]
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert runtime.background.claim() is None


def paused_store(tmp_path):
    store = Store(tmp_path / "bots.db", objects=LocalObjectStorage(tmp_path / "objects"))
    bot = store.save_bot("owner", BotInput(name="雲", purpose="Files"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue("owner", chat["id"], TaskInput(message="Work", request_id=uuid4().hex))
    task = store.claim("owner")
    session = uuid4().hex
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (task["id"],))
        db.execute(
            "INSERT INTO background_turns VALUES(?,'computer_control','pause','[]',0)",
            (task["id"],),
        )
        db.execute(
            "INSERT INTO bot_computer_control VALUES(?,?,?,'suspending',0)",
            (bot["id"], task["id"], session),
        )
    return store, task, session


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_paused_workspace_and_encrypted_browser_checkpoint_are_committed_without_task_success(
    tmp_path,
    outcome,
):
    store, task, session = paused_store(tmp_path)
    workspace = WorkspaceObjects(store)
    browser = BrowserStates(store, b"k" * 32)
    data = json.dumps({"cookies": [], "origins": []}).encode()
    saved = workspace.stage_pause(
        "owner",
        task["bot_id"],
        task["id"],
        session,
        [b"checkpoint"],
        10,
        browser_states=browser,
        browser_data=data,
    )
    assert workspace.current("owner", task["bot_id"]) == saved
    assert browser.current("owner", task["bot_id"]) == data
    assert store.task("owner", task["id"])["status"] == "waiting"
    # The same task can later stage a final candidate; its terminal transaction
    # still decides whether that candidate replaces the committed pause point.
    with store.connect(write=True) as db:
        db.execute("UPDATE background_turns SET state='running' WHERE task_id=?", (task["id"],))
        db.execute("DELETE FROM bot_computer_control WHERE bot_id=?", (task["bot_id"],))
    final = workspace.stage("owner", task["bot_id"], task["id"], [b"finished"], 8)
    with pytest.raises(Conflict):
        workspace.current("owner", task["bot_id"])
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status=? WHERE id=?", (outcome, task["id"]))
    assert workspace.current("owner", task["bot_id"]) == (
        final if outcome == "completed" else saved
    )


@pytest.mark.parametrize("race", ["cancel", "resume"])
def test_idle_upload_race_cannot_replace_either_previous_checkpoint(tmp_path, monkeypatch, race):
    store, task, session = paused_store(tmp_path)
    workspace = WorkspaceObjects(store)
    browser = BrowserStates(store, b"k" * 32)
    old_browser = b'{"cookies":[],"origins":[]}'
    old = workspace.stage_pause(
        "owner",
        task["bot_id"],
        task["id"],
        session,
        [b"old"],
        3,
        browser_states=browser,
        browser_data=old_browser,
    )
    original = store.put_object

    def upload(*args, **kwargs):
        ref = original(*args, **kwargs)
        with store.connect(write=True) as db:
            if race == "cancel":
                db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task["id"],))
            else:
                db.execute("DELETE FROM bot_computer_control WHERE bot_id=?", (task["bot_id"],))
        return ref

    monkeypatch.setattr(store, "put_object", upload)
    with pytest.raises(Conflict):
        workspace.stage_pause(
            "owner",
            task["bot_id"],
            task["id"],
            session,
            [b"new"],
            3,
            browser_states=browser,
            browser_data=b"changed",
        )
    assert workspace.current("owner", task["bot_id"]) == old
    assert browser.current("owner", task["bot_id"]) == old_browser


def test_another_computer_session_cannot_publish_a_pause_checkpoint(tmp_path):
    store, task, _ = paused_store(tmp_path)
    with pytest.raises(Conflict):
        WorkspaceObjects(store).stage_pause(
            "owner", task["bot_id"], task["id"], uuid4().hex, [b"bad"], 3
        )
    assert WorkspaceObjects(store).current("owner", task["bot_id"]) is None


@pytest.mark.parametrize("state", ["Stopped", "expired", "wrong-owner"])
def test_azure_activity_never_resumes_an_idle_machine_or_bypasses_expiry_and_ownership(
    monkeypatch, state
):
    wire = AzureWire()
    if state == "expired":
        monkeypatch.setattr("alysis_code.bots.compute_azure.time.time", lambda: 1000)
        wire.provider.expires_at = 999
    elif state == "wrong-owner":
        wire.labels = {}
    else:
        wire.state = state
    with pytest.raises(ComputeError):
        wire.provider.run(
            wire.ref, ["/opt/alysis/.venv/bin/python", "-I", "-c", "pass"], timeout=10
        )
    assert not any(
        request.url.path.endswith(("/resume", "/executeShellCommand")) for request in wire.requests
    )


def test_azure_activity_requires_a_receipt_and_does_not_replay_an_uncertain_command():
    wire = AzureWire()
    wire.provider.run(wire.ref, ["/opt/alysis/.venv/bin/python", "-I", "-c", "pass"], timeout=10)
    wire.drop = True
    with pytest.raises(ComputeUncertain):
        wire.provider.run(
            wire.ref, ["/opt/alysis/.venv/bin/python", "-I", "-c", "pass"], timeout=10
        )
    commands = [
        request for request in wire.requests if request.url.path.endswith("/executeShellCommand")
    ]
    assert len(commands) == 2
    assert "pass" in commands[0].content.decode()


def test_schema_upgrade_preserves_all_checkpoint_references_and_allows_multiple_points_per_task():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE bots(id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO tasks VALUES('task')")
        db.execute("INSERT INTO bots VALUES('bot')")
        for table in ("workspace_objects", "browser_states"):
            db.execute(
                f"CREATE TABLE {table}(id TEXT PRIMARY KEY,task_id TEXT UNIQUE REFERENCES tasks(id),bot_id TEXT NOT NULL REFERENCES bots(id),ref TEXT NOT NULL,created_at REAL NOT NULL)"
            )
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?,?,?)",
                ("original", "task", "bot", "opaque-Αθήνα-東京", 100.5),
            )
        migrate_checkpoints(db)
        for table in ("workspace_objects", "browser_states"):
            assert db.execute(f"SELECT * FROM {table}").fetchone() == (
                "original",
                "task",
                "bot",
                "opaque-Αθήνα-東京",
                100.5,
                0,
            )
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?,?,?,1)", ("pause", "task", "bot", "new-ref", 101)
            )
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_bot_summary_distinguishes_control_pause_from_unsettled_work(study, monkeypatch):
    runtime, request, _, _ = setup(study, monkeypatch)
    store, task, _, _ = study
    request("take")
    runtime.computer_controls.park(task, [])
    bot = next(b for b in store.list_bots("owner") if b["id"] == task["bot_id"])
    assert bot["active_tasks"] == bot["waiting_tasks"] == 1
    request("resume")
    bot = next(b for b in store.list_bots("owner") if b["id"] == task["bot_id"])
    assert bot["active_tasks"] == 1 and bot["waiting_tasks"] == 0
    assert store.list_bots("other-owner") == []
