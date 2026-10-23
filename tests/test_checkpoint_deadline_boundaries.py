from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from alysis_code.agent import checkpoint_worker
from alysis_code.agent.anytime_checkpoint import AnytimeCheckpointManager
from alysis_code.command_text import command_text
from alysis_code.config import AnytimeCheckpointConfig
from alysis_code.session_artifacts import SessionArtifactLayout


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
import psutil
from alysis_code.agent import checkpoint_worker, anytime_checkpoint
target = Path({str(manager.root / "deliverable.py")!r})
marker = Path({str(marker)!r})
def stall():
    marker.write_text(json.dumps({{'pid': os.getpid(), 'created': psutil.Process().create_time()}}))
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
checkpoint_worker.main()
"""
    return [sys.executable, "-I", "-c", code]


@pytest.fixture
def ready_stall_worker(monkeypatch):
    """Reach the I/O fault under the real deadline, independent of cold imports.

    Cold worker startup has its own bounded publication test below. These tests
    need to exercise cancellation after the specific filesystem fault is reached.
    """
    processes = []
    original_popen = subprocess.Popen

    def prepare(manager, marker, phase):
        ready = marker.with_suffix(".ready")
        command = _stall_command(manager, marker, phase)
        command[-1] = command[-1].replace(
            "checkpoint_worker.main()",
            f"Path({str(ready)!r}).write_text('ready')\ncheckpoint_worker.main()",
        )
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = original_popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **command_text(),
            **options,
        )
        processes.append(process)
        stop_at = time.monotonic() + 10
        while not ready.exists():
            assert process.poll() is None, "fault-injection worker exited during setup"
            assert time.monotonic() < stop_at, "fault-injection worker did not become ready"
            time.sleep(0.01)
        adopted = False

        def adopt_worker(args, **kwargs):
            nonlocal adopted
            if args == command and not adopted:
                adopted = True
                return process
            return original_popen(args, **kwargs)

        monkeypatch.setattr(checkpoint_worker, "worker_command", lambda: command)
        monkeypatch.setattr(checkpoint_worker.subprocess, "Popen", adopt_worker)

    yield prepare
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _assert_worker_gone(marker):
    identity = json.loads(marker.read_text())
    try:
        process = psutil.Process(identity["pid"])
        assert process.create_time() != identity["created"]
    except psutil.NoSuchProcess:
        pass


def test_real_checkpoint_publication_has_no_application_configuration_imports(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    source = str(Path(checkpoint_worker.__file__).resolve().parents[2])
    # Guard the entire real operation, including late artifact-handle creation.
    # Import-only smoke checks miss dependencies first reached during publication.
    code = f"""
import sys, importlib.abc
sys.path.insert(0, {source!r})
class WorkerDependencyGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        forbidden = ('pydantic', 'pydantic_core', 'alysis_code.config',
                     'alysis_code.llm', 'alysis_code.compaction')
        if any(fullname == prefix or fullname.startswith(prefix + '.') for prefix in forbidden):
            raise RuntimeError('Checkpoint worker imported forbidden dependency: ' + fullname)
sys.meta_path.insert(0, WorkerDependencyGuard())
from alysis_code.agent.checkpoint_worker import main
main()
"""
    monkeypatch.setattr(
        checkpoint_worker, "worker_command", lambda: [sys.executable, "-I", "-c", code]
    )
    started = time.monotonic()
    event = _preserve(manager, 0, 7)
    assert event["status"] == "verified_checkpoint_preserved", event
    assert time.monotonic() - started < 2.1
    assert manager.best["objective_value"] == 7
    assert manager.best["manifest_handle"].startswith("artifact:")
    assert manager._task_index("bounded-checkpoint").is_file()


@pytest.mark.parametrize("phase", ["read", "archive", "publish"])
def test_hung_checkpoint_io_is_reaped_and_cannot_replace_prior_best(
    tmp_path, ready_stall_worker, phase
):
    manager = _manager(tmp_path)
    _seed_prior_checkpoint(manager)
    previous = dict(manager.best)
    index = manager._task_index("bounded-checkpoint")
    index_before = index.read_bytes()
    marker = tmp_path / "worker-entered.json"
    ready_stall_worker(manager, marker, phase)
    (manager.root / "deliverable.py").write_text("value = 3\n")
    started = time.monotonic()
    event = _preserve(manager, 1, 3, remaining=1.6)
    elapsed = time.monotonic() - started
    assert marker.exists(), "The worker must actually reach the injected I/O stall"
    assert event["status"] == "checkpoint_time_budget_exhausted", event
    assert not event["cleanup_pending"]
    assert elapsed < 2.1  # Includes a scheduling tolerance, not a worker grace extension.
    assert manager.best == previous
    assert index.read_bytes() == index_before
    _assert_worker_gone(marker)
    # A timed-out background writer must never promote a candidate afterwards.
    time.sleep(0.1)
    assert index.read_bytes() == index_before


def test_resume_archive_validation_has_the_same_owned_io_deadline(tmp_path, ready_stall_worker):
    manager = _manager(tmp_path)
    _seed_prior_checkpoint(manager)
    marker = tmp_path / "restore-entered.json"
    ready_stall_worker(manager, marker, "restore")
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
    _assert_worker_gone(marker)


def test_expired_checkpoint_never_spawns_a_worker(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        checkpoint_worker, "worker_command", lambda: pytest.fail("worker launched after deadline")
    )
    event = _preserve(manager, 0, 7, remaining=0)
    assert event["status"] == "checkpoint_time_budget_exhausted"
    assert manager.best is None


def test_unacknowledged_publication_cannot_replace_receipt_on_resume(
    tmp_path, monkeypatch, ready_stall_worker
):
    manager = _manager(tmp_path)
    _seed_prior_checkpoint(manager)
    receipt = dict(manager.best)
    marker = tmp_path / "published-before-timeout.json"
    command = checkpoint_worker.worker_command
    ready_stall_worker(manager, marker, "after_publish")
    (manager.root / "deliverable.py").write_text("value = 3\n")
    event = _preserve(manager, 1, 3, remaining=1.6)
    assert marker.exists(), "Must reach the post-publication acknowledgment boundary"
    assert event["status"] == "checkpoint_time_budget_exhausted", event
    assert manager.best == receipt
    assert json.loads(manager._task_index("bounded-checkpoint").read_text())["generation"] == 1
    _assert_worker_gone(marker)
    monkeypatch.setattr(checkpoint_worker, "worker_command", command)

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
