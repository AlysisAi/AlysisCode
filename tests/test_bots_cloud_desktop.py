import base64
import hashlib
import json
import threading
from pathlib import Path

import pytest

from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.cloud_desktop_worker import Transfers
from alysis_code.bots.compute import (
    CommandReceipt,
    ComputerRef,
    ComputerSpec,
    ComputerStatus,
    ComputeUncertain,
)
from alysis_code.bots.desktop_linux import (
    ComputerActionUncertain,
    DesktopError,
    DesktopStartupCleanupError,
)
from alysis_code.bots.workspaces import WorkspaceError
from alysis_code.cancellation import CooperativeCancellationError


class Provider:
    scope = "synthetic-scope"

    def __init__(self):
        self.allocations = {}
        self.actions = []
        self.mode = None
        self.started = threading.Event()
        self.stopped = threading.Event()

    def find(self, identity):
        return next(
            (
                ref
                for ref, state in self.allocations.values()
                if ref.identity == identity and state != "missing"
            ),
            None,
        )

    def create(self, identity, spec):
        if self.mode == "lost-create":
            raise ComputeUncertain("Lost creation response")
        ref = ComputerRef("synthetic", self.scope, identity.allocation_id, identity)
        self.allocations[ref.resource_id] = (ref, "running")
        return ref

    def status(self, ref):
        state = self.allocations[ref.resource_id][1]
        return ComputerStatus(state, state)

    def stop(self, ref):
        self.allocations[ref.resource_id] = (ref, "stopped")
        self.stopped.set()
        return self.status(ref)

    def delete(self, ref):
        if self.mode == "lost-delete":
            raise ComputeUncertain("Lost delete response")
        self.allocations[ref.resource_id] = (ref, "missing")
        return self.status(ref)

    def run(self, ref, argv, *, timeout):
        self.actions.append(argv)
        if argv[-2] == "start":
            if self.mode == "broken-start":
                return CommandReceipt(1, "", "private upstream failure")
            return CommandReceipt(
                0, json.dumps({"ready": True, "session": argv[-1], "uid": 1000}), ""
            )
        request = json.loads(base64.b64decode(argv[-1]))
        self.started.set()
        if self.mode == "delay":
            assert self.stopped.wait(3)
        if self.mode == "lost-reply":
            raise ComputeUncertain("Lost command receipt")
        if self.mode == "wrong-session":
            request["session"] = "foreign"
        result = {"exit_code": 0, "stdout": "Ω 日本語", "stderr": ""}
        reply = {k: request[k] for k in ("session", "id")}
        reply.update({"error": "rejected"} if self.mode == "rejected" else {"result": result})
        return CommandReceipt(0, json.dumps(reply), "")


@pytest.fixture
def setup(tmp_path):
    provider = Provider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("prepared-image"), tmp_path / "ledger"
    )
    return provider, factory


def test_each_task_has_its_own_owned_allocation_and_confirmed_cleanup(setup, tmp_path):
    provider, factory = setup
    first = factory("bot", tmp_path, cancel=lambda: False)
    with pytest.raises(WorkspaceError):
        factory("bot", tmp_path, cancel=lambda: False)
    with pytest.raises(DesktopError, match="another bot"):
        first.call("other", "run", {"command": "no"})
    assert first.call("bot", "run", {"command": "yes"})["stdout"] == "Ω 日本語"
    assert json.loads(first._record.read_text())["state"] == "allocated"
    first.close()
    assert provider.status(first.ref).state == "missing"
    assert json.loads(first._record.read_text())["state"] == "deleted"
    second = factory("bot", tmp_path, cancel=lambda: False)
    assert first.identity != second.identity
    second.close()


@pytest.mark.parametrize("lost_reply", [False, True])
def test_idle_heartbeat_reaches_guest_without_provider_hook_and_never_replays(
    setup, tmp_path, monkeypatch, lost_reply
):
    provider, factory = setup
    computer = factory("bot", tmp_path, cancel=lambda: False)
    assert computer._keepalive_thread.is_alive()
    # Advance one heartbeat without waiting for the production interval.
    waits = iter([False, True])
    monkeypatch.setattr(computer._stop, "wait", lambda _: next(waits))
    if lost_reply:
        provider.mode = "lost-reply"
    computer._keepalive()
    requests = [json.loads(base64.b64decode(argv[-1])) for argv in provider.actions[1:]]
    assert len(requests) == 1
    assert requests[0]["operation"] == "ping"
    assert requests[0]["arguments"] == {}
    assert requests[0]["session"] == computer.identity.allocation_id
    assert provider.status(computer.ref).state == ("missing" if lost_reply else "running")
    computer.close()


def test_computer_lifecycle_reports_pending_creation_and_preserves_failure(setup, tmp_path):
    from alysis_code.bots.computer import ComputerViews

    provider, factory = setup
    views = ComputerViews()
    factory.lifecycle = lambda bot, phase: views.set_lifecycle("owner", bot, phase)
    creating, release = threading.Event(), threading.Event()
    original = provider.create

    def create(*args):
        creating.set()
        assert release.wait(3)
        return original(*args)

    provider.create = create
    result = []
    thread = threading.Thread(
        target=lambda: result.append(factory("bot", tmp_path, cancel=lambda: False))
    )
    thread.start()
    try:
        assert creating.wait(3)
        assert views.lifecycle("owner", "bot") == "starting"
        assert views.lifecycle("other", "bot") is None
        assert views.frame("owner", "bot") == {"state": "not_running"}
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert views.lifecycle("owner", "bot") == "ready"
    computer = result[0]
    provider.mode = "lost-reply"
    with pytest.raises(ComputerActionUncertain):
        computer.call("bot", "run", {"command": "one-write"})
    computer.close()
    assert views.lifecycle("owner", "bot") == "failed"
    provider.mode = None
    replacement = factory("bot", tmp_path, cancel=lambda: False)
    assert views.lifecycle("owner", "bot") == "ready"
    replacement.close()
    assert views.lifecycle("owner", "bot") == "stopped"


@pytest.mark.parametrize("failure", ["lost-reply", "wrong-session"])
def test_uncertain_commands_retire_without_replay(setup, tmp_path, failure):
    provider, factory = setup
    computer = factory("bot", tmp_path, cancel=lambda: False)
    provider.mode = failure
    with pytest.raises(ComputerActionUncertain):
        computer.call("bot", "run", {"command": "one-write"})
    assert len(provider.actions) == 2  # startup and a single dispatched action
    assert provider.status(computer.ref).state == "missing"


def test_safe_rejection_preserves_session_for_fresh_observation(setup, tmp_path):
    provider, factory = setup
    computer = factory("bot", tmp_path, cancel=lambda: False)
    provider.mode = "rejected"
    with pytest.raises(DesktopError, match="rejected"):
        computer.call("bot", "click", {"frame_id": "stale"})
    assert computer.alive()
    computer.close()


def test_stop_kills_computer_while_remote_command_is_pending(setup, tmp_path):
    provider, factory = setup
    computer = factory("bot", tmp_path, cancel=lambda: False)
    provider.mode = "delay"
    cancel = threading.Event()

    def stop():
        assert provider.started.wait(3)
        cancel.set()

    worker = threading.Thread(target=stop)
    worker.start()
    with pytest.raises(CooperativeCancellationError):
        computer.call("bot", "run", {"command": "slow"}, cancel=cancel.is_set)
    worker.join(3)
    assert provider.status(computer.ref).state == "missing"


def test_cleanup_failure_retains_lease_and_intent_until_confirmed(setup, tmp_path):
    provider, factory = setup
    computer = factory("bot", tmp_path, cancel=lambda: False)
    provider.mode = "lost-delete"
    with pytest.raises(ComputeUncertain):
        computer.close()
    with pytest.raises(WorkspaceError):
        factory("bot", tmp_path, cancel=lambda: False)
    assert json.loads(computer._record.read_text())["state"] == "allocated"
    provider.mode = None
    computer.close()
    replacement = factory("bot", tmp_path, cancel=lambda: False)
    replacement.close()


def test_unknown_creation_without_inventory_match_is_not_automatically_retried(setup, tmp_path):
    provider, factory = setup
    provider.mode = "lost-create"
    with pytest.raises(DesktopStartupCleanupError) as caught:
        factory("bot", tmp_path, cancel=lambda: False)
    pending = caught.value.executor
    assert json.loads(pending._record.read_text())["state"] == "creating"
    with pytest.raises(WorkspaceError):
        factory("bot", tmp_path, cancel=lambda: False)
    # Fixture reconciliation supplies the actual late-created resource.
    provider.mode = None
    pending.ref = provider.create(pending.identity, pending.spec)
    pending.close()


def test_definite_creation_refusal_releases_intent_and_allows_later_work(setup, tmp_path):
    from alysis_code.bots.compute import ComputeCreationRefused

    provider, factory = setup
    create = provider.create

    def refuse(*_):
        raise ComputeCreationRefused("Ανεπαρκής χώρος 東京", code="insufficient_local_storage")

    provider.create = refuse
    with pytest.raises(ComputeCreationRefused):
        factory("bot", tmp_path, cancel=lambda: False)
    records = list((tmp_path / "ledger").rglob("allocation.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["state"] == "deleted"
    assert provider.allocations == {}
    assert provider.actions == []
    assert factory.capacity()["cleanup_pending"] == 0
    provider.create = create
    replacement = factory("bot", tmp_path, cancel=lambda: False)
    replacement.close()


def test_service_crash_reconciles_old_allocation_before_new_task(setup, tmp_path):
    provider, factory = setup
    first = factory("bot", tmp_path, cancel=lambda: False)
    first._lease.cleanup(clean=False)  # simulate controller OS releasing its lock
    factory = CloudDesktopFactory("owner", provider, factory.spec, tmp_path / "ledger")
    second = factory("bot", tmp_path, cancel=lambda: False)
    assert provider.status(first.ref).state == "missing"
    assert second.identity != first.identity
    second.close()


def test_explicit_uncapped_factory_starts_more_than_two_bots_and_keeps_ownership(setup, tmp_path):
    provider, previous = setup
    factory = CloudDesktopFactory(
        "owner", provider, previous.spec, tmp_path / "uncapped", max_computers=None
    )
    computers = []
    try:
        computers = [factory("bot-" + str(i), tmp_path, cancel=lambda: False) for i in range(4)]
        assert factory.capacity()["active"] == 4
        assert factory.capacity()["limit"] is None
        with pytest.raises(WorkspaceError):
            factory("bot-0", tmp_path, cancel=lambda: False)
    finally:
        for computer in computers:
            computer.close()
    assert factory.capacity()["active"] == 0


def test_browser_and_desktop_share_one_factory_capacity(setup, tmp_path):
    provider, factory = setup
    first = factory("a", tmp_path, cancel=lambda: False)
    second = factory._acquire("b", "browser", lambda: False)
    sibling = factory("b", tmp_path, cancel=lambda: False)
    with pytest.raises(DesktopError, match="busy"):
        factory("c", tmp_path, cancel=lambda: False)
    assert len(provider.allocations) == 2
    first.close()
    third = factory("c", tmp_path, cancel=lambda: False)
    third.close()
    second.close()
    sibling.close()


def test_hosted_capacity_queue_waits_without_allocating_and_can_be_cancelled(setup, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    provider, factory = setup
    factory.wait_for_capacity = True
    factory.max_computers = 1
    phases = []
    queued = threading.Event()

    def lifecycle(bot, phase):
        phases.append((bot, phase))
        if phase == "queued":
            queued.set()

    factory.lifecycle = lifecycle
    first = factory("first", tmp_path, cancel=lambda: False)
    cancelled = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(factory, "second", tmp_path, cancel=cancelled.is_set)
        assert queued.wait(2)
        assert len(provider.allocations) == 1 and factory.capacity()["queued"] == 1
        cancelled.set()
        with pytest.raises(CooperativeCancellationError):
            waiting.result(timeout=2)
    assert len(provider.allocations) == 1
    assert ("second", "stopped") in phases and factory.capacity()["queued"] == 0
    queued.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(factory, "third", tmp_path, cancel=lambda: False)
        assert queued.wait(2)
        first.close()
        third = waiting.result(timeout=2)
    assert len(provider.allocations) == 2 and factory.capacity()["active"] == 1
    third.close()
    assert factory.capacity()["active"] == 0


def test_capacity_status_does_not_block_behind_remote_computer_startup(setup, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    provider, factory = setup
    started, proceed = threading.Event(), threading.Event()
    run = provider.run

    def delayed(ref, argv, *, timeout):
        if argv[-2] == "start":
            started.set()
            assert proceed.wait(3)
        return run(ref, argv, timeout=timeout)

    provider.run = delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        computer = pool.submit(factory, "first", tmp_path, cancel=lambda: False)
        assert started.wait(2)
        try:
            status = pool.submit(factory.capacity).result(timeout=0.5)
            assert status["active"] == 1
        finally:
            proceed.set()
        computer.result(timeout=2).close()


def test_unknown_startup_consumes_capacity_until_confirmed_cleanup(setup, tmp_path):
    provider, factory = setup
    factory.max_computers = 1
    provider.mode = "lost-create"
    with pytest.raises(DesktopStartupCleanupError) as caught:
        factory("a", tmp_path, cancel=lambda: False)
    with pytest.raises(DesktopError, match="busy"):
        factory("b", tmp_path, cancel=lambda: False)
    provider.mode = None
    pending = caught.value.executor
    pending.ref = provider.create(pending.identity, pending.spec)
    pending.close()
    replacement = factory("b", tmp_path, cancel=lambda: False)
    replacement.close()


def transfers(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Transfers(tmp_path, workspace)


def test_transfer_snapshots_preserve_binary_bytes_and_never_overwrite(tmp_path):
    transport = transfers(tmp_path)
    data = bytes(range(256)) * 12000
    digest = hashlib.sha256(data).hexdigest()
    prepared = transport.prepare({"name": "日本語.bin", "size": len(data), "sha256": digest})
    Path(prepared["path"]).write_bytes(data)
    receipt = transport.commit(prepared)
    assert receipt["sha256"] == digest
    snapshot = transport.export({"name": "日本語.bin"})
    assert Path(snapshot["path"]).read_bytes() == data
    prepared = transport.prepare({"name": "日本語.bin", "size": len(data), "sha256": digest})
    Path(prepared["path"]).write_bytes(data)
    with pytest.raises(FileExistsError):
        transport.commit(prepared)
    assert (transport.workspace / "日本語.bin").read_bytes() == data


def test_transfer_rejects_symlinks_and_incomplete_upload(tmp_path):
    transport = transfers(tmp_path)
    (transport.workspace / "link").symlink_to("/etc/passwd")
    with pytest.raises(OSError):
        transport.export({"name": "link"})
    prepared = transport.prepare(
        {"name": "bad.bin", "size": 5, "sha256": hashlib.sha256(b"right").hexdigest()}
    )
    Path(prepared["path"]).write_bytes(b"wrong")
    with pytest.raises(ValueError):
        transport.commit(prepared)
    assert not (transport.workspace / "bad.bin").exists()
    assert not list(tmp_path.glob("publish-*"))


def test_native_disk_start_ignores_ambient_python_path_and_reports_runtime_error(setup, tmp_path):
    provider, factory = setup
    provider.mode = "broken-start"
    with pytest.raises(DesktopError, match="runtime failed to start"):
        factory("bot", tmp_path, cancel=lambda: False)
    assert all(state == "missing" for ref, state in provider.allocations.values())
    assert provider.actions[0][1:3] == ["-I", "-c"]
    assert "/opt/alysis/src" in provider.actions[0][3]


def test_cloud_export_discards_corrupt_payload_and_closes_transport(tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4

    from alysis_code.bots.cloud_desktop import CloudDesktopFiles
    from alysis_code.bots.models import BotInput
    from alysis_code.bots.store import Store
    from alysis_code.cancellation import EventCancellationToken

    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Files", purpose="Test file integrity"))
    session = uuid4().hex
    closed = []
    data = b"\x00" * 200000

    def download(*args, **kwargs):
        try:
            yield data
        finally:
            closed.append(True)

    executor = SimpleNamespace(
        identity=SimpleNamespace(allocation_id=session),
        ref=None,
        provider=SimpleNamespace(download=download),
        _request=lambda *args, **kwargs: {
            "path": f"/tmp/alysis-cloud-{session}/export-{uuid4().hex}",
            "name": "actual.bin",
            "bytes": len(data),
            "sha256": hashlib.sha256(b"different").hexdigest(),
        },
    )
    task = {"id": None, "bot_id": bot["id"]}
    with pytest.raises(DesktopError, match="integrity"):
        CloudDesktopFiles().export_file(
            executor, store, "owner", task, "actual.bin", EventCancellationToken(threading.Event())
        )
    assert closed == [True]
    assert store.list_files("owner", bot["id"]) == []


def test_cloud_transfer_paths_cannot_leave_the_exact_session():
    from types import SimpleNamespace
    from uuid import uuid4

    from alysis_code.bots.cloud_desktop import CloudDesktopFiles

    session = uuid4().hex
    executor = SimpleNamespace(identity=SimpleNamespace(allocation_id=session))
    path = f"/tmp/alysis-cloud-{session}/export-{uuid4().hex}"
    assert CloudDesktopFiles._path(executor, path, "export-") == path
    for value in (path.replace(session, uuid4().hex), path + "/../../other", "/etc/passwd"):
        with pytest.raises((ValueError, DesktopError)):
            CloudDesktopFiles._path(executor, value, "export-")


class CheckpointProvider(Provider):
    def __init__(self):
        super().__init__()
        self.checkpoint = b"synthetic archive wire payload"
        self.restored = []
        self.corrupt = False

    def run(self, ref, argv, *, timeout):
        if argv[-2] == "start":
            return super().run(ref, argv, timeout=timeout)
        from uuid import uuid4

        request = json.loads(base64.b64decode(argv[-1]))
        operation = request["operation"]
        self.actions.append(operation)
        root = f"/tmp/alysis-cloud-{ref.identity.allocation_id}/"
        result = {}
        if operation in {"workspace_snapshot", "workspace_checkpoint"}:
            result = {
                "path": root + "export-" + uuid4().hex,
                "bytes": len(self.checkpoint),
                "sha256": hashlib.sha256(self.checkpoint).hexdigest(),
            }
        elif operation == "workspace_restore_prepare":
            result = {"path": root + "transfer-" + uuid4().hex, "transfer_id": uuid4().hex}
        return CommandReceipt(
            0,
            json.dumps({"id": request["id"], "session": request["session"], "result": result}),
            "",
        )

    def download(self, ref, path, *, size, cancel=lambda: False):
        assert size == len(self.checkpoint)
        yield b"x" * size if self.corrupt else self.checkpoint

    def upload(self, ref, path, chunks, *, size, cancel=lambda: False):
        data = b"".join(chunks)
        assert len(data) == size
        self.restored.append(data)


class CheckpointTasks:
    def __init__(self):
        self.states = {}

    def task(self, owner, task_id):
        return {"bot_id": "bot", "status": self.states.get(task_id, "running")}


def checkpoint_factory(tmp_path):
    provider = CheckpointProvider()
    tasks = CheckpointTasks()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("image"), tmp_path, persist_workspace=True, store=tasks
    )
    return provider, tasks, factory


def completed_checkpoint(factory, tasks, tmp_path):
    computer = factory("bot", tmp_path, cancel=lambda: False)
    computer.bind_task("first")
    computer.finish()
    assert not computer._snapshot.exists()  # not authoritative until DB commit
    tasks.states["first"] = "completed"
    following = factory("bot", tmp_path, cancel=lambda: False)
    following.bind_task("next")
    return following


def test_checkpoint_commits_only_after_task_completion_and_restores_next_allocation(tmp_path):
    provider, tasks, factory = checkpoint_factory(tmp_path)
    following = completed_checkpoint(factory, tasks, tmp_path)
    snapshot = following._snapshot
    assert snapshot.read_bytes() == provider.checkpoint
    assert following.workspace_reopened
    assert provider.restored == [provider.checkpoint]
    provider.checkpoint = b"cancelled new writes"
    following.close()
    assert snapshot.read_bytes() != provider.checkpoint


@pytest.mark.parametrize("failure", ["corrupt", "cleanup", "cancel"])
def test_failed_checkpoint_retains_last_successful_archive(tmp_path, failure):
    provider, tasks, factory = checkpoint_factory(tmp_path)
    computer = completed_checkpoint(factory, tasks, tmp_path)
    snapshot = computer._snapshot
    before = snapshot.read_bytes()
    provider.checkpoint = b"new generation"
    provider.corrupt = failure == "corrupt"
    provider.mode = "lost-delete" if failure == "cleanup" else None
    with pytest.raises((DesktopError, ComputeUncertain, CooperativeCancellationError)):
        computer.finish(cancel=lambda: failure == "cancel")
    assert snapshot.read_bytes() == before
    assert not snapshot.with_suffix(".pending").exists()
    provider.mode = None
    computer.close()
    assert provider.status(computer.ref).state == "missing"


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_late_terminal_failure_never_promotes_prepared_checkpoint(tmp_path, status):
    provider, tasks, factory = checkpoint_factory(tmp_path)
    computer = completed_checkpoint(factory, tasks, tmp_path)
    snapshot = computer._snapshot
    before = snapshot.read_bytes()
    provider.checkpoint = b"new writes staged before late failure"
    computer.finish()
    assert computer._candidate.exists()
    tasks.states["next"] = status  # cancellation/failure wins Store.finish transaction
    reopened = factory("bot", tmp_path, cancel=lambda: False)
    assert snapshot.read_bytes() == before
    assert provider.restored[-1] == before
    reopened.close()


def test_pending_task_blocks_checkpoint_settlement_until_status_is_known(tmp_path):
    provider, tasks, factory = checkpoint_factory(tmp_path)
    computer = factory("bot", tmp_path, cancel=lambda: False)
    computer.bind_task("pending")
    computer.finish()
    with pytest.raises(DesktopError, match="finalizing"):
        factory("bot", tmp_path, cancel=lambda: False)
    assert computer._candidate.exists()
    tasks.states["pending"] = "completed"
    reopened = factory("bot", tmp_path, cancel=lambda: False)
    assert reopened.workspace_reopened
    reopened.close()


def test_a_paused_task_resumes_from_its_own_checkpoint_while_others_still_wait(tmp_path):
    from alysis_code.bots.computer_context import opening_for

    provider, tasks, factory = checkpoint_factory(tmp_path)
    computer = factory("bot", tmp_path, cancel=lambda: False)
    computer.bind_task("paused")  # e.g. waiting for the person to approve an action
    provider.checkpoint = b"state at the pause"
    computer.finish()
    with pytest.raises(DesktopError, match="finalizing"):
        factory("bot", tmp_path, cancel=lambda: False)
    with opening_for("another"), pytest.raises(DesktopError, match="finalizing"):
        factory("bot", tmp_path, cancel=lambda: False)
    with opening_for("paused"):
        resumed = factory("bot", tmp_path, cancel=lambda: False)
    resumed.bind_task("paused")
    assert provider.restored[-1] == b"state at the pause"
    assert resumed._candidate_record.exists()  # the task's outcome still decides
    provider.checkpoint = b"state after the approval"
    resumed.finish()
    tasks.states["paused"] = "completed"
    following = factory("bot", tmp_path, cancel=lambda: False)
    assert provider.restored[-1] == b"state after the approval"
    following.close()


def test_selective_stop_uses_the_same_owned_cloud_protocol(setup, tmp_path):
    import base64

    provider, factory = setup
    computer = factory("bot", tmp_path, cancel=lambda: False)
    try:
        with pytest.raises(DesktopError, match="another bot"):
            computer.call("other", "stop_process", {"process_id": "owned"})
        computer.call("bot", "stop_process", {"process_id": "owned"})
        request = json.loads(base64.b64decode(provider.actions[-1][-1]))
        assert request["operation"] == "stop_process"
        assert request["arguments"] == {"process_id": "owned"}
    finally:
        computer.close()
