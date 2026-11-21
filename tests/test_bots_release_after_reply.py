"""A finished task keeps its reply when its computer is released afterwards.

Saving the owl's browser storage (sign-ins) at release serves the next task; it is not the task's
work. A computer that refuses to make that save, or storage that fails it, leaves the owl's last
saved browser storage in place, says so on the task, and still saves the computer's files and
retires the computer. A release that fails in any other way after the reply keeps the reply too:
the computer's handles stay with the reaper, and the task says what was not confirmed. The
computer reports every refused operation with its real error type.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import socket
import threading
from uuid import uuid4

import pytest
from test_bots_browser_runtime import BrowserFixture, ClientFixture, response
from test_bots_cloud_desktop import Provider
from test_bots_cut_off_replies import Reviewer
from test_bots_desktop_runtime import run_task, study  # noqa: F401  (a fixture)

from alysis_code.bots import diagnostics
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import CommandReceipt, ComputerSpec
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage, ObjectStorageError
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.bots.workspace_objects import WorkspaceObjects
from alysis_code.cancellation import EventCancellationToken

POSIX_COMPUTER = pytest.mark.skipif(
    not hasattr(os, "killpg") or not hasattr(socket, "AF_UNIX"),
    reason="The owl computer's bridge and leases are POSIX-only.",
)


@pytest.fixture
def support(tmp_path, monkeypatch):
    """The diagnostics file support copies, as entries."""
    path = tmp_path / "diagnostics.jsonl"
    monkeypatch.setattr(diagnostics, "_path", path)

    def entries(event=None):
        if not path.exists():
            return []
        found = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [entry for entry in found if event is None or entry["event"] == event]

    return entries


# The computer's side: every refused operation says what kind of error it was.


def test_a_refused_operation_carries_its_error_type_and_only_a_data_free_reason():
    from alysis_code.bots.cloud_desktop_worker import rejected_reply
    from alysis_code.bots.workspace_archive import WorkspaceArchiveError
    from alysis_code.ide.browser_profiles import BrowserProfileError

    archive = rejected_reply(WorkspaceArchiveError("Workspace exceeds 4096 entries."), fatal=False)
    assert archive == {
        "error": "Computer operation rejected; inspect before retrying.",
        "error_type": "WorkspaceArchiveError",
        "error_class": "WorkspaceArchiveError",
        "error_detail": "Workspace exceeds 4096 entries.",
        "fatal": False,
    }
    unclean = rejected_reply(
        BrowserProfileError("Browser shutdown was unclean; the previous saved state was retained."),
        fatal=True,
    )
    assert (unclean["error_class"], unclean["fatal"]) == ("BrowserProfileError", True)
    assert unclean["error_detail"].startswith("Browser shutdown was unclean")
    # An operating-system error says which (its errno name), never the path it names.
    full = rejected_reply(
        OSError(errno.ENOSPC, "No space left on device", "/tmp/Αθηνά/προφίλ"), fatal=False
    )
    assert (full["error_type"], full["error_detail"]) == ("OSError", "ENOSPC")
    assert "Αθηνά" not in json.dumps(full, ensure_ascii=False)
    # A ValueError or KeyError can quote what it was given: its type only.
    quoted = rejected_reply(ValueError("invalid literal for int(): 'Γιώργος 4111'"), fatal=False)
    assert quoted["error_type"] == "ValueError" and "error_detail" not in quoted
    assert rejected_reply(KeyError("arguments"), fatal=False)["error_class"] == "KeyError"


class RecoveredProfiles:
    """A profile store whose lease shows the browser did not shut down cleanly."""

    def __init__(self):
        self.released = []

    def acquire(self, account, owner):
        profiles = self

        class Lease:
            recovered = True
            path = None

            def release(self):
                profiles.released.append(owner)

        return Lease()


@POSIX_COMPUTER
def test_an_unclean_browser_shutdown_is_refused_as_a_typed_profile_error(tmp_path):
    from alysis_code.bots.cloud_browser_worker import BrowserBridge
    from alysis_code.bots.cloud_desktop_worker import rejected_reply
    from alysis_code.ide.browser_profiles import BrowserProfileError

    bridge = object.__new__(BrowserBridge)
    bridge.session, bridge.root = uuid4().hex, tmp_path
    bridge.profiles = RecoveredProfiles()
    bridge.close = lambda: None
    with pytest.raises(BrowserProfileError) as refused:
        bridge.snapshot()
    assert bridge.profiles.released == [bridge.session]
    reply = rejected_reply(refused.value, fatal=False)
    assert reply["error_type"] == "BrowserProfileError"
    assert reply["error_detail"] == (
        "Browser shutdown was unclean; the previous saved state was retained."
    )


# The host's side: a refused browser save does not stop the computer's release.

GOOD = b"browser storage saved by the first task"


class GuestProvider(Provider):
    """A computer whose guest restores and starts the browser, answers the browser snapshot
    with ``browser_reply`` (a refusal, or None to make it), and makes a workspace snapshot."""

    def __init__(self):
        super().__init__()
        self.browser_reply = None
        self.browser_data = b"browser storage of this task"
        self.workspace = "Αρχεία της εργασίας".encode()
        self.files = {}
        self.uploads = []
        self.requests = []

    def run(self, ref, argv, *, timeout):
        if argv[-2] == "start":
            return super().run(ref, argv, timeout=timeout)
        request = json.loads(base64.b64decode(argv[-1]))
        operation = request["operation"]
        self.requests.append(operation)
        root = f"/tmp/alysis-cloud-{ref.identity.allocation_id}/"
        reply = {key: request[key] for key in ("id", "session")}

        def export(data):
            path = root + "export-" + uuid4().hex
            self.files[path] = data
            return {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

        if operation == "browser_snapshot" and self.browser_reply is not None:
            reply.update(self.browser_reply)
        elif operation == "browser_snapshot":
            reply["result"] = export(self.browser_data)
        elif operation == "workspace_snapshot":
            reply["result"] = export(self.workspace)
        elif operation in {"browser_restore_prepare", "workspace_restore_prepare"}:
            reply["result"] = {"path": root + "transfer-" + uuid4().hex, "transfer_id": uuid4().hex}
        elif operation == "browser_start":
            reply["result"] = {"ready": True, "features": []}
        else:
            reply["result"] = {"ready": True}
        return CommandReceipt(0, json.dumps(reply), "")

    def download(self, ref, path, *, size, cancel=lambda: False):
        data = self.files.pop(path)
        assert size == len(data)
        yield data

    def upload(self, ref, path, chunks, *, size, cancel=lambda: False):
        data = b"".join(chunks)
        assert len(data) == size
        self.uploads.append(data)


@pytest.fixture
def owl(tmp_path):
    store = Store(tmp_path / "bots.db", objects=LocalObjectStorage(tmp_path / "objects"))
    provider = GuestProvider()
    factory = CloudDesktopFactory(
        "owner",
        provider,
        ComputerSpec("fixture-image"),
        tmp_path / "leases",
        store=store,
        browser_enabled=True,
        persist_workspace=True,
        browser_state_key=b"k" * 32,
    )
    bot = store.save_bot("owner", BotInput(name="Γλαύκα"))["id"]
    chat = store.create_chat("owner", bot, "Κρατήσεις")["id"]
    store.enqueue("owner", chat, TaskInput(message="Πρώτη φορά", request_id=uuid4().hex))
    first = store.claim("owner")
    factory.browser_states.stage("owner", bot, first["id"], GOOD)
    store.finish("owner", first["id"], "completed", content="Έγινε.")
    store.enqueue("owner", chat, TaskInput(message="Ξανά, το ίδιο", request_id=uuid4().hex))
    second = store.claim("owner")
    return store, provider, factory, bot, second["id"]


def opened(owl_parts, tmp_path):
    store, provider, factory, bot, task_id = owl_parts
    computer = factory(bot, tmp_path, cancel=lambda: False)
    computer.bind_task(task_id)
    assert provider.uploads == [GOOD]  # its browser started from the saved storage
    return computer


def events(store, task_id, kind):
    return [
        event["data"] for event in store.task("owner", task_id)["events"] if event["kind"] == kind
    ]


@POSIX_COMPUTER
@pytest.mark.parametrize(
    "refusal",
    [
        {"error_type": "WorkspaceArchiveError", "error_class": "WorkspaceArchiveError",
         "error_detail": "Workspace archive exceeds 50 MiB including overhead."},
        {"error_type": "BrowserProfileError", "error_class": "BrowserProfileError",
         "error_detail": "Browser shutdown was unclean; the previous saved state was retained."},
        {},  # an older computer image, which names no type
    ],
)  # fmt: skip
def test_a_refused_browser_save_keeps_the_last_good_storage_and_still_saves_the_files(
    owl, tmp_path, support, refusal
):
    store, provider, factory, bot, task_id = owl
    computer = opened(owl, tmp_path)
    provider.browser_reply = {
        "error": "Computer operation rejected; inspect before retrying.",
        "fatal": False,
        **refusal,
    }
    computer.finish()  # no error: the release goes on
    assert provider.requests[-2:] == ["browser_snapshot", "workspace_snapshot"]
    assert provider.status(computer.ref).state == "missing" and not factory._instances
    # The computer's files are saved for this task, as on any completed release.
    assert WorkspaceObjects(store).current("owner", bot, task_id=task_id) is not None
    (unsaved,) = events(store, task_id, "browser_state_not_saved")
    assert unsaved["error_type"] == "BrowserError" and "last saved storage" in unsaved["message"]
    store.finish("owner", task_id, "completed", content="Έγινε ξανά.")
    # The owl's next browser starts from the last storage that was saved.
    assert factory.browser_states.current("owner", bot) == GOOD
    (rejected,) = support("computer_operation_rejected")
    assert rejected["operation"] == "browser_snapshot"
    assert rejected["error_type"] == refusal.get("error_type", "")
    assert rejected["detail"] == refusal.get("error_detail", "")
    assert support("browser_state_not_saved")[0]["error"] == "BrowserError"


@POSIX_COMPUTER
def test_a_refusal_that_stopped_the_computer_is_still_a_failed_release(owl, tmp_path):
    from alysis_code.ide.managed_browser import BrowserError

    store, provider, factory, bot, task_id = owl
    computer = opened(owl, tmp_path)
    provider.browser_reply = {
        "error": "Computer operation rejected; inspect before retrying.",
        "error_type": "BrowserError",
        "fatal": True,
    }
    with pytest.raises(BrowserError):
        computer.finish()
    assert "workspace_snapshot" not in provider.requests
    assert provider.status(computer.ref).state == "missing"  # never left running
    assert events(store, task_id, "browser_state_not_saved") == []


@POSIX_COMPUTER
def test_browser_storage_that_object_storage_refuses_still_saves_the_files(
    owl, tmp_path, monkeypatch, support
):
    store, provider, factory, bot, task_id = owl
    computer = opened(owl, tmp_path)

    def refuse(*args, **kwargs):
        raise ObjectStorageError(
            "Browser storage exceeds the configured object allowance; the previous state was retained."
        )

    monkeypatch.setattr(factory.browser_states, "stage", refuse)
    computer.finish()
    assert provider.status(computer.ref).state == "missing"
    assert WorkspaceObjects(store).current("owner", bot, task_id=task_id) is not None
    (unsaved,) = events(store, task_id, "browser_state_not_saved")
    assert unsaved["error_type"] == "ObjectStorageError"
    store.finish("owner", task_id, "completed", content="Έγινε ξανά.")
    assert factory.browser_states.current("owner", bot) == GOOD


@POSIX_COMPUTER
def test_a_saved_browser_storage_still_replaces_the_last_one(owl, tmp_path):
    store, provider, factory, bot, task_id = owl
    computer = opened(owl, tmp_path)
    computer.finish()
    store.finish("owner", task_id, "completed", content="Έγινε ξανά.")
    assert factory.browser_states.current("owner", bot) == provider.browser_data
    assert events(store, task_id, "browser_state_not_saved") == []


# The runtime's side: a finished reply stands whatever the release after it does.


class FailingRelease(BrowserFixture):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def release(self, task_id, *, cancelled):
        super().release(task_id, cancelled=cancelled)
        if self.error is not None:
            error, self.error = self.error, None
            raise error


def checked_turn(tmp_path, error, *, stop_during_release=False):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("owner", BotInput(name="Γλαύκα", purpose="Τιμές"))
    chat = store.create_chat("owner", bot["id"], "Τιμές")
    store.enqueue(
        "owner", chat["id"], TaskInput(message="Βρες την τιμή", request_id="price-request-1")
    )
    task = store.claim("owner")
    client = ClientFixture(
        [
            response(name="browser_navigate", arguments={"url": "https://shop.example/"}),
            response("Η τιμή είναι 12 €."),
        ]
    )
    browser = FailingRelease(error)
    if stop_during_release:
        original = browser.release

        def release(task_id, *, cancelled):
            # The person pressed Stop while the computer was being saved.
            with store.connect(write=True) as db:
                db.execute(
                    "UPDATE tasks SET status='cancelling' WHERE id=? AND status='running'",
                    (task_id,),
                )
            original(task_id, cancelled=cancelled)

        browser.release = release
    runtime = BotRuntime(store, "owner", lambda _: client, browser_computers=browser)
    runtime.reply_review = Reviewer()
    runtime.run(task, EventCancellationToken(threading.Event()))
    return store, runtime, browser, store.task("owner", task["id"])


@pytest.mark.parametrize(
    "error,storage",
    [
        (RuntimeError("private cleanup detail"), False),
        (ObjectStorageError("private storage detail"), True),
    ],
)
def test_a_checked_reply_is_kept_when_its_computer_release_fails(tmp_path, support, error, storage):
    store, runtime, browser, task = checked_turn(tmp_path, error)
    assert task["status"] == "completed" and task["output"] == "Η τιμή είναι 12 €."
    assert task["error"] is None and task["failure"] is None
    (pending,) = [e["data"] for e in task["events"] if e["kind"] == "computer_cleanup_pending"]
    assert pending["after_reply"] is True and pending["storage"] is storage
    assert "private" not in json.dumps(task, ensure_ascii=False)
    assert runtime.browser_cleanup_pending
    # Released once, with the task's work kept: not again as a cancelled one.
    assert browser.releases == [(task["id"], False)]
    assert support("task_failed") == []
    (failed,) = support("computer_release_failed")
    assert failed["error"] == type(error).__name__
    assert failed["where"].startswith("alysis_code.bots.runtime:_release_computers:")


def test_stop_during_the_release_still_ends_the_task_as_stopped(tmp_path):
    store, _, _, task = checked_turn(
        tmp_path, RuntimeError("private cleanup detail"), stop_during_release=True
    )
    assert task["status"] == "cancelled" and task["output"] == ""


def test_finished_work_off_the_reply_loop_keeps_its_result_when_cleanup_fails(study, support):  # noqa: F811
    store, task, manager, instances = study
    client = ClientFixture(
        [
            response(name="computer_launch", arguments={"command": "fixture"}),
            response("Το έγγραφο είναι έτοιμο."),
        ]
    )
    original = manager.factory

    def fail_close(*args, **kwargs):
        executor = original(*args, **kwargs)
        executor.fail_close = True
        return executor

    manager.factory = fail_close
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    try:
        run_task(runtime, task)
        current = store.task("owner", task["id"])
        assert current["status"] == "completed"
        assert current["output"].endswith("Το έγγραφο είναι έτοιμο.")
        assert any(
            e["kind"] == "computer_cleanup_pending" and e["data"]["after_reply"]
            for e in current["events"]
        )
        # The computer's handles stay with the reaper: never dropped while it may still run.
        assert runtime.browser_cleanup_pending
        assert manager._computers[task["bot_id"]].retired and not instances[0].closed
        assert support("computer_release_failed")[0]["error"] == "DesktopError"
    finally:
        instances[0].fail_close = False
    manager.reap()
    assert instances[0].closed and not manager._computers
