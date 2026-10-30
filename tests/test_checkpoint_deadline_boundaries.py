from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import psutil
import pytest

from alysis_code import process_reaping
from alysis_code.agent import checkpoint_worker
from alysis_code.agent.anytime_checkpoint import AnytimeCheckpointManager
from alysis_code.config import AnytimeCheckpointConfig
from alysis_code.session_artifacts import SessionArtifactLayout

_REAL_WORKER_COMMAND = checkpoint_worker.worker_command


def _fault_command(code):
    # Retain the production interpreter and isolation flags; only the owned
    # worker's test program changes to inject/observe a fault.
    command = _REAL_WORKER_COMMAND()
    command[command.index("-c") + 1] = code
    return command


@pytest.mark.skipif(os.name != "nt", reason="Windows communicate uses synchronous stdin writes")
def test_checkpoint_input_backpressure_cannot_outlive_deadline(monkeypatch):
    previous_threads = set(threading.enumerate())
    processes = []
    original_popen = checkpoint_worker.subprocess.Popen

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(checkpoint_worker.subprocess, "Popen", popen)
    monkeypatch.setattr(
        checkpoint_worker,
        "worker_command",
        lambda: _fault_command("import time; time.sleep(3)"),
    )
    started = time.monotonic()
    with pytest.raises(checkpoint_worker.CheckpointWorkerExpired):
        checkpoint_worker.run_checkpoint_worker({"input": "x" * (1024 * 1024)}, timeout=0.4)
    assert time.monotonic() - started < 1.5
    assert len(processes) == 1
    processes[0].wait(timeout=0)
    for thread in set(threading.enumerate()) - previous_threads:
        if thread.name == "alysis-checkpoint-input":
            thread.join(timeout=1)
            assert not thread.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="Windows inventory must not extend caller deadline")
def test_checkpoint_slow_descendant_inventory_retains_cleanup_without_blocking(monkeypatch):
    gate = threading.Event()
    entered = threading.Event()
    original_children = psutil.Process.children
    original_popen = checkpoint_worker.subprocess.Popen
    processes = []

    def children(process, *, recursive):
        entered.set()
        gate.wait(timeout=2)
        return original_children(process, recursive=recursive)

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(psutil.Process, "children", children)
    monkeypatch.setattr(checkpoint_worker.subprocess, "Popen", popen)
    monkeypatch.setattr(
        checkpoint_worker, "worker_command", lambda: _fault_command("import time; time.sleep(5)")
    )
    started = time.monotonic()
    try:
        with pytest.raises(checkpoint_worker.CheckpointWorkerExpired) as caught:
            checkpoint_worker.run_checkpoint_worker({"input": "x"}, timeout=0.4)
        assert time.monotonic() - started < 1.5
        assert caught.value.cleanup_pending
        assert entered.wait(timeout=1)
        cleanup = processes[0]._alysis_checkpoint_cleanup
        assert cleanup in process_reaping._windows_command_cleanups
        assert not cleanup.finished.is_set()
    finally:
        gate.set()
        for process in processes:
            process.wait(timeout=3)
            cleanup = process._alysis_checkpoint_cleanup
            assert cleanup.finished.wait(timeout=2)
            assert cleanup.clean
            assert cleanup not in process_reaping._windows_command_cleanups


@pytest.mark.skipif(os.name != "nt", reason="Windows retained cleanup ownership")
def test_checkpoint_cleanup_start_failure_keeps_stream_ownership(monkeypatch):
    import io

    original_start = threading.Thread.start
    killed = []

    class Process:
        pid = 0
        args = ["owned checkpoint"]
        stdin = io.StringIO()
        stdout = io.StringIO()
        stderr = io.StringIO()

        def communicate(self, **kwargs):
            if "timeout" in kwargs:
                raise checkpoint_worker.subprocess.TimeoutExpired(self.args, 0.4)
            return "", ""

        def kill(self):
            killed.append(True)

        def __exit__(self, *_args):
            self.stdout.close()
            self.stderr.close()

    def start(thread):
        if thread.name == "alysis-command-cleanup":
            raise RuntimeError("synthetic cleanup start failure")
        return original_start(thread)

    process = Process()
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(checkpoint_worker.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(process_reaping, "_stop_windows_process_tree", lambda _process: [])
    started = time.monotonic()
    try:
        with pytest.raises(checkpoint_worker.CheckpointWorkerExpired) as caught:
            checkpoint_worker.run_checkpoint_worker({"input": "x"}, timeout=0.4)
        assert time.monotonic() - started < 1.5
        assert caught.value.cleanup_pending
        cleanup = process._alysis_checkpoint_cleanup
        assert cleanup in process_reaping._windows_command_cleanups
        assert not cleanup.thread_started
        assert not cleanup.finished.is_set()
        assert str(cleanup.error) == "synthetic cleanup start failure"
        assert killed == [True]
        assert not process.stdout.closed
        assert not process.stderr.closed
    finally:
        process._alysis_checkpoint_cleanup.run()
    assert process.stdout.closed and process.stderr.closed
    assert cleanup.finished.is_set() and cleanup.clean
    assert cleanup not in process_reaping._windows_command_cleanups


def test_checkpoint_large_input_is_delivered_completely(monkeypatch):
    monkeypatch.setattr(
        checkpoint_worker,
        "worker_command",
        lambda: _fault_command(
            "import json,sys; data=json.load(sys.stdin); "
            "print(json.dumps({'state':{},'length':len(data['input'])}))"
        ),
    )
    result = checkpoint_worker.run_checkpoint_worker({"input": "x" * (1024 * 1024)}, timeout=2)
    assert result == {"state": {}, "length": 1024 * 1024}


@pytest.mark.skipif(os.name != "nt", reason="Windows input writer ownership")
def test_checkpoint_input_writer_start_failure_keeps_owned_cleanup(monkeypatch):
    original_start = threading.Thread.start
    original_kill = checkpoint_worker._kill_owned_worker
    cleaned = []

    def start(thread):
        if thread.name == "alysis-checkpoint-input":
            raise RuntimeError("synthetic writer start failure")
        return original_start(thread)

    def kill(process, *, stop_at):
        result = original_kill(process, stop_at=stop_at)
        cleaned.append((result, process.wait(timeout=0)))
        return result

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(checkpoint_worker, "_kill_owned_worker", kill)
    monkeypatch.setattr(
        checkpoint_worker, "worker_command", lambda: _fault_command("import time; time.sleep(3)")
    )
    with pytest.raises(RuntimeError, match="synthetic writer start failure"):
        checkpoint_worker.run_checkpoint_worker({"input": "x"}, timeout=2)
    assert len(cleaned) == 1 and cleaned[0][0]


def test_checkpoint_child_error_is_preserved_when_input_pipe_closes(monkeypatch):
    monkeypatch.setattr(
        checkpoint_worker,
        "worker_command",
        lambda: _fault_command(
            "import sys; sys.stdin.close(); print('synthetic worker failure',file=sys.stderr); "
            "sys.exit(9)"
        ),
    )
    with pytest.raises(ValueError, match="synthetic worker failure"):
        checkpoint_worker.run_checkpoint_worker({"input": "x" * (1024 * 1024)}, timeout=2)


def _candidate(generation):
    return {
        "task_id": "bounded-checkpoint",
        "generation": generation,
        "verified_success": True,
        "terminal": True,
        "material_paths": ["deliverable.py"],
    }


def _manager(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "deliverable.py").write_text("value = 7\n")
    return AnytimeCheckpointManager(
        root=root,
        layout=SessionArtifactLayout(tmp_path / "artifacts"),
        config=AnytimeCheckpointConfig(objective_command="python measure.py"),
    )


def _preserve(manager, generation, score, remaining=None):
    return manager.consider(
        outcome=_candidate(generation),
        result={"cmd": "python measure.py", "exit_code": 0, "stdout": json.dumps({"score": score})},
        accepted_commands={"python measure.py"},
        remaining_seconds=remaining,
    )


def _seed_prior_checkpoint(manager):
    # The fault under test starts after a valid candidate already exists. Seed
    # that fixture directly so an unrelated extra process startup cannot prevent
    # the real worker from reaching the injected fault or recovery boundary.
    event = manager._consider_inline(
        outcome=_candidate(0),
        result={"cmd": "python measure.py", "exit_code": 0, "stdout": '{"score": 7}'},
        accepted_commands={"python measure.py"},
    )
    assert event["status"] == "verified_checkpoint_preserved", event


def _stall_command(manager, marker, phase):
    source = str(Path(checkpoint_worker.__file__).resolve().parents[2])
    # Fault injection is isolated to the owned worker, with an observable marker
    # proving the real snapshot/publication path reached the blocked operation.
    code = f"""
import sys
sys.path.insert(0, {source!r})
import os, time, json
from pathlib import Path
from alysis_code.agent import checkpoint_io, anytime_checkpoint
target = Path({str(manager.root / "deliverable.py")!r})
marker = Path({str(marker)!r})
def stall():
    marker.write_text(json.dumps({{'pid': os.getpid()}}))
    time.sleep(30)
phase = {phase!r}
if phase in ('read', 'restore'):
    original = Path.read_bytes
    def slow_read(path):
        if path == target or (phase == 'restore' and path.suffix == '.zip'):
            stall()
        return original(path)
    Path.read_bytes = slow_read
elif phase == 'archive':
    original = anytime_checkpoint.os.open
    def slow_open(path, *args, **kwargs):
        if str(path).endswith('.zip'):
            stall()
        return original(path, *args, **kwargs)
    anytime_checkpoint.os.open = slow_open
else:
    original = anytime_checkpoint.atomic_write_json
    def slow_publish(path, *args, **kwargs):
        if phase == 'after_publish' and path.name == 'best.json':
            original(path, *args, **kwargs)
            stall()
            return
        if path.name == 'best.json':
            stall()
        return original(path, *args, **kwargs)
    anytime_checkpoint.atomic_write_json = slow_publish
checkpoint_io.main()
"""
    return _fault_command(code)


def _inject_stall(monkeypatch, manager, marker, phase):
    # Observe identity in the already-running parent. Importing psutil inside
    # the child adds native-module startup absent from the production worker
    # and can consume its I/O allowance before it reaches the injected fault.
    identity = {}
    original_kill = checkpoint_worker._kill_owned_worker

    def observe_cleanup(process, *, stop_at):
        try:
            if marker.exists():
                pid = json.loads(marker.read_text())["pid"]
                if pid != process.pid:
                    leader = psutil.Process(process.pid)
                    owned = {child.pid for child in leader.children(recursive=True)}
                    # A launcher may own a different interpreter PID. Only that
                    # case needs ancestry discovery. For the direct worker its
                    # retained Popen already proves ownership; a duplicate census
                    # here consumes the production cleanup reserve under test.
                    assert pid in owned
                identity.update(pid=pid, created=psutil.Process(pid).create_time())
                if pid == process.pid:
                    identity["owned_process"] = process
        finally:
            clean = original_kill(process, stop_at=stop_at)
        return clean

    monkeypatch.setattr(checkpoint_worker, "_kill_owned_worker", observe_cleanup)
    monkeypatch.setattr(
        checkpoint_worker, "worker_command", lambda: _stall_command(manager, marker, phase)
    )
    return identity


def _assert_worker_gone(marker, identity):
    assert json.loads(marker.read_text())["pid"] == identity["pid"]
    owned_process = identity.get("owned_process")
    if owned_process is not None:
        assert owned_process.pid == identity["pid"]
        # A terminated Windows process can remain queryable while handles are
        # open. The retained Popen handle proves this exact worker has exited;
        # a zero-time wait still rejects a live worker without any grace period.
        owned_process.wait(timeout=0)
        return
    try:
        process = psutil.Process(identity["pid"])
        assert process.create_time() != identity["created"]
    except psutil.NoSuchProcess:
        pass


@pytest.mark.parametrize("marker_pid", [101, 102, 103])
def test_fault_observer_proves_ownership_without_duplicate_direct_worker_inventory(
    tmp_path, monkeypatch, marker_pid
):
    from types import SimpleNamespace

    manager = _manager(tmp_path)
    marker = tmp_path / "worker-entered.json"
    marker.write_text(json.dumps({"pid": marker_pid}))
    inventories = []
    kills = []
    process = SimpleNamespace(pid=101)

    def children(*, recursive):
        assert recursive
        inventories.append(True)
        return [SimpleNamespace(pid=102)]

    monkeypatch.setattr(
        checkpoint_worker.psutil,
        "Process",
        lambda pid: SimpleNamespace(pid=pid, children=children, create_time=lambda: 123.0),
    )
    monkeypatch.setattr(
        checkpoint_worker,
        "_kill_owned_worker",
        lambda owned, *, stop_at: kills.append((owned, stop_at)) or True,
    )
    identity = _inject_stall(monkeypatch, manager, marker, "read")
    if marker_pid == 103:
        with pytest.raises(AssertionError):
            checkpoint_worker._kill_owned_worker(process, stop_at=456.0)
        assert not identity
    else:
        assert checkpoint_worker._kill_owned_worker(process, stop_at=456.0)
        assert identity["pid"] == marker_pid and identity["created"] == 123.0
        assert identity.get("owned_process") is (process if marker_pid == 101 else None)
    assert inventories == ([] if marker_pid == 101 else [True])
    assert kills == [(process, 456.0)]


def test_real_checkpoint_publication_has_no_application_configuration_imports(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    source = str(Path(checkpoint_worker.__file__).resolve().parents[2])
    # Guard the entire real operation, including late artifact-handle creation.
    # Import-only smoke checks miss dependencies first reached during publication.
    code = f"""
import sys
sys.path.insert(0, {source!r})
assert 'site' not in sys.modules, 'The filesystem worker must not run ambient site hooks'
class WorkerDependencyGuard:
    def find_spec(self, fullname, path=None, target=None):
        forbidden = ('psutil', 'pydantic', 'pydantic_core', 'alysis_code.config',
                     'alysis_code.llm', 'alysis_code.compaction',
                     'alysis_code.tools.fs', 'alysis_code.file_classification', 'subprocess',
                     'dataclasses', 'inspect')
        if any(fullname == prefix or fullname.startswith(prefix + '.') for prefix in forbidden):
            raise RuntimeError('Checkpoint worker imported forbidden dependency: ' + fullname)
sys.meta_path.insert(0, WorkerDependencyGuard())
from alysis_code.agent.checkpoint_io import main
main()
"""
    monkeypatch.setattr(checkpoint_worker, "worker_command", lambda: _fault_command(code))
    started = time.monotonic()
    event = _preserve(manager, 0, 7)
    assert event["status"] == "verified_checkpoint_preserved", event
    assert time.monotonic() - started < 2.1
    assert manager.best["objective_value"] == 7
    assert manager.best["manifest_handle"].startswith("artifact:")
    assert manager._task_index("bounded-checkpoint").is_file()
    assert (
        manager.layout.resolve_handle(manager.best["manifest_handle"]) == manager.best["manifest"]
    )


def test_receipt_recovery_does_not_load_archive_writer_or_filesystem_tools(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    _seed_prior_checkpoint(manager)
    source = str(Path(checkpoint_worker.__file__).resolve().parents[2])
    code = f"""
import sys
sys.path.insert(0, {source!r})
class RecoveryDependencyGuard:
    def find_spec(self, fullname, path=None, target=None):
        forbidden = ('zipfile', 'subprocess', 'alysis_code.tools.fs',
                     'alysis_code.file_classification', 'dataclasses', 'inspect')
        if any(fullname == prefix or fullname.startswith(prefix + '.') for prefix in forbidden):
            raise RuntimeError('Recovery imported an unused dependency: ' + fullname)
sys.meta_path.insert(0, RecoveryDependencyGuard())
from alysis_code.agent.checkpoint_io import main
main()
"""
    monkeypatch.setattr(checkpoint_worker, "worker_command", lambda: _fault_command(code))
    resumed = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    resumed.select_task("bounded-checkpoint", acknowledged_checkpoint=manager.best)
    assert resumed.best == manager.best
    assert resumed._attempted == manager._attempted


@pytest.mark.parametrize("phase", ["read", "archive", "publish"])
def test_hung_checkpoint_io_is_reaped_and_cannot_replace_prior_best(tmp_path, monkeypatch, phase):
    manager = _manager(tmp_path)
    _seed_prior_checkpoint(manager)
    previous = dict(manager.best)
    index = manager._task_index("bounded-checkpoint")
    index_before = index.read_bytes()
    marker = tmp_path / "worker-entered.json"
    identity = _inject_stall(monkeypatch, manager, marker, phase)
    (manager.root / "deliverable.py").write_text("value = 3\n")
    started = time.monotonic()
    event = _preserve(manager, 1, 3, remaining=1.6)
    elapsed = time.monotonic() - started
    assert marker.exists(), ("The worker must actually reach the injected I/O stall", event)
    assert event["status"] == "checkpoint_time_budget_exhausted", event
    assert not event["cleanup_pending"]
    assert elapsed < 2.1  # Includes a scheduling tolerance, not a worker grace extension.
    assert manager.best == previous
    assert index.read_bytes() == index_before
    _assert_worker_gone(marker, identity)
    # A timed-out background writer must never promote a candidate afterwards.
    time.sleep(0.1)
    assert index.read_bytes() == index_before


def test_resume_archive_validation_has_the_same_owned_io_deadline(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    _seed_prior_checkpoint(manager)
    marker = tmp_path / "restore-entered.json"
    identity = _inject_stall(monkeypatch, manager, marker, "restore")
    resumed = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    started = time.monotonic()
    resumed.select_task(
        "bounded-checkpoint", remaining_seconds=1.6, acknowledged_checkpoint=manager.best
    )
    assert time.monotonic() - started < 2.1
    assert marker.exists()
    assert resumed.best is None
    _assert_worker_gone(marker, identity)


def test_expired_checkpoint_never_spawns_a_worker(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        checkpoint_worker, "worker_command", lambda: pytest.fail("worker launched after deadline")
    )
    event = _preserve(manager, 0, 7, remaining=0)
    assert event["status"] == "checkpoint_time_budget_exhausted"
    assert manager.best is None


def test_unacknowledged_publication_cannot_replace_receipt_on_resume(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    _seed_prior_checkpoint(manager)
    receipt = dict(manager.best)
    marker = tmp_path / "published-before-timeout.json"
    with monkeypatch.context() as fault_patch:
        identity = _inject_stall(fault_patch, manager, marker, "after_publish")
        (manager.root / "deliverable.py").write_text("value = 3\n")
        event = _preserve(manager, 1, 3, remaining=1.6)
        assert marker.exists(), ("Must reach the post-publication acknowledgment boundary", event)
        assert event["status"] == "checkpoint_time_budget_exhausted", event
        assert manager.best == receipt
        assert json.loads(manager._task_index("bounded-checkpoint").read_text())["generation"] == 1
        _assert_worker_gone(marker, identity)

    resumed = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    resumed.select_task("bounded-checkpoint", acknowledged_checkpoint=receipt)
    assert resumed.best == receipt
    assert resumed.best["generation"] == 0

    without_receipt = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    without_receipt.select_task("bounded-checkpoint")
    assert without_receipt.best is None


@pytest.mark.parametrize(
    "field", ["task_id", "acceptance_revision", "workspace_root", "archive_sha256"]
)
def test_recovery_receipt_must_match_identity_and_actual_bytes(tmp_path, field):
    manager = _manager(tmp_path)
    _seed_prior_checkpoint(manager)
    invalid_receipt = {**manager.best, field: "wrong"}
    resumed = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    resumed.select_task("bounded-checkpoint", acknowledged_checkpoint=invalid_receipt)
    assert resumed.best is None
    assert resumed._attempted == set(), "Validation must finish; a worker timeout is not rejection"
