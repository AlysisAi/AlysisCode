from __future__ import annotations

import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from alysis_code.agent.anytime_checkpoint import AnytimeCheckpointManager
from alysis_code.config import AnytimeCheckpointConfig
from alysis_code.run_outcome import task_outcome_record
from alysis_code.session_artifacts import SessionArtifactLayout


def test_plain_directory_snapshot_does_not_start_git(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    (manager.root / "notes.txt").write_bytes(b"disposable notes")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("No repository to query"))
    files, omitted = manager._snapshot(time.monotonic() + 2, ["notes.txt"])
    assert files == {"notes.txt": b"disposable notes"}
    assert not omitted


def test_new_repository_still_applies_git_ignore_rules(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)
    (manager.root / "notes.txt").write_bytes(b"keep")
    (manager.root / "ignored.txt").write_bytes(b"ignored")
    before, _ = manager._snapshot(time.monotonic() + 2, ["notes.txt"])
    assert "ignored.txt" in before
    subprocess.run(["git", "init", str(manager.root)], capture_output=True, check=True)
    (manager.root / ".gitignore").write_text("ignored.txt\n")
    after, _ = manager._snapshot(time.monotonic() + 2, ["notes.txt"])
    assert after["notes.txt"] == b"keep"
    assert "ignored.txt" not in after


@pytest.mark.parametrize("location", ["file", "ancestor", "bare", "environment", "unreadable"])
def test_possible_git_locations_keep_git_inventory_authority(tmp_path, monkeypatch, location):
    manager = _manager(tmp_path)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)
    (manager.root / "notes.txt").write_bytes(b"keep")
    (manager.root / "ignored.txt").write_bytes(b"ignored")
    if location == "file":
        (manager.root / ".git").write_text("gitdir: ../separate")
    elif location == "ancestor":
        (tmp_path / ".git").mkdir()
    elif location == "bare":
        (manager.root / "HEAD").write_text("ref: refs/heads/main")
    elif location == "environment":
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "separate"))
    else:
        original = Path.lstat

        def unreadable(path):
            if path == manager.root / ".git":
                raise PermissionError("Cannot determine repository state")
            return original(path)

        monkeypatch.setattr(Path, "lstat", unreadable)
    calls = []

    def inventory(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"notes.txt\0")

    monkeypatch.setattr(subprocess, "run", inventory)
    files, _ = manager._snapshot(time.monotonic() + 2, ["notes.txt"])
    assert len(calls) == 1
    assert files == {"notes.txt": b"keep"}


def test_worker_does_not_import_workspace_or_pythonpath_hooks(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    (manager.root / "deliverable.py").write_text("value = 7\n")
    poison = manager.root / "alysis_code"
    poison.mkdir()
    (poison / "__init__.py").write_text("raise RuntimeError('workspace package imported')")
    marker = tmp_path / "environment-hook-ran"
    (manager.root / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')"
    )
    monkeypatch.chdir(manager.root)
    monkeypatch.setenv("PYTHONPATH", str(manager.root))
    event = manager.consider(outcome=_outcome(0), result={}, accepted_commands=set())
    assert event["status"] == "verified_checkpoint_preserved", event
    assert not marker.exists()


def _outcome(generation: int, *, passed: bool = True, task_id: str = "task-one"):
    return task_outcome_record(
        exit_code=0,
        reason="completed",
        task_id=task_id,
        state={
            "material_edit_count": 1,
            "verification_relevant_edit_generation": generation,
            "touched_repo_paths": ["deliverable.py"],
            "completion_certificate": {"status": "SUFFICIENT" if passed else "CONTRADICTED"},
            "accepted_verification_evidence": [{"evidence_category": "USER_EXPLICIT"}],
        },
    )


def _manager(tmp_path, **config):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    return AnytimeCheckpointManager(
        root=root,
        layout=SessionArtifactLayout(tmp_path / "artifacts"),
        config=AnytimeCheckpointConfig(**config),
    )


def test_real_numeric_check_keeps_better_candidate_and_fresh_consumer_can_run_it(tmp_path):
    command = "python deliverable.py"
    manager = _manager(tmp_path, objective_command=command)
    for generation, score in enumerate([7, 3, 9]):
        (manager.root / "deliverable.py").write_text(
            f'import json\nprint(json.dumps({{"score": {score}}}))\n'
        )
        process = subprocess.run(
            [sys.executable, "deliverable.py"],
            cwd=manager.root,
            capture_output=True,
            text=True,
            check=True,
        )
        event = manager.consider(
            outcome=_outcome(generation),
            result={"cmd": command, "exit_code": process.returncode, "stdout": process.stdout},
            accepted_commands={command},
        )
        assert event
        assert event["status"] == (
            "prior_candidate_retained" if score == 9 else "verified_checkpoint_preserved"
        ), event
    assert event["status"] == "prior_candidate_retained"
    assert manager.best["objective_value"] == 3
    consumer = tmp_path / "fresh-consumer"
    with zipfile.ZipFile(manager.best["archive_path"]) as archive:
        archive.extractall(consumer)
    result = subprocess.run(
        [sys.executable, "deliverable.py"], cwd=consumer, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout)["score"] == 3
    assert "9" in (manager.root / "deliverable.py").read_text()
    # Resume validates archive identity and keeps its original version.
    resumed = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    resumed.select_task("task-one", acknowledged_checkpoint=manager.best)
    assert resumed.best == manager.best
    resumed.select_task("a-foreign-task")
    assert resumed.best is None


def test_failed_unmeasured_and_model_claimed_scores_never_replace_verified_candidate(tmp_path):
    manager = _manager(tmp_path)
    (manager.root / "deliverable.py").write_bytes(b'print("working")\n')
    # This case starts with a known-good checkpoint and exercises rejection of
    # later candidates through the real bounded worker. Keep its baseline setup
    # independent of a separate process startup; the positive integration case
    # above verifies real worker publication and fresh-consumer recovery.
    baseline = manager._consider_inline(outcome=_outcome(0), result={}, accepted_commands=set())
    assert baseline["status"] == "verified_checkpoint_preserved", baseline
    original = manager.best.copy()
    (manager.root / "deliverable.py").write_text('raise RuntimeError("broken")\n')
    assert (
        manager.consider(
            outcome=_outcome(1, passed=False), result={"score": 100}, accepted_commands=set()
        )["status"]
        == "unverified_candidate_rejected"
    )
    unmeasured = manager.consider(
        outcome=_outcome(2), result={"score": 100}, accepted_commands=set()
    )
    assert unmeasured["status"] == "prior_candidate_retained", unmeasured
    assert manager.best == original
    with zipfile.ZipFile(original["archive_path"]) as archive:
        assert archive.read("deliverable.py") == b'print("working")\n'


def test_checkpoint_excludes_credentials_and_rejects_expired_or_oversized_candidate(tmp_path):
    manager = _manager(tmp_path, max_total_bytes=1024)
    (manager.root / "deliverable.py").write_text("pass\n")
    (manager.root / ".env").write_text("SECRET=private\n")
    # Exercise actual snapshot bytes without making these data-policy checks
    # depend on a cold worker starting within its two-second production budget.
    # Real worker execution/deadlines are covered in test_checkpoint_deadline_boundaries.
    event = manager._consider_inline(outcome=_outcome(0), result={}, accepted_commands=set())
    assert event["status"] == "verified_checkpoint_preserved", event
    with zipfile.ZipFile(manager.best["archive_path"]) as archive:
        assert ".env" not in archive.namelist()
    assert ".env" in manager.best["omitted_paths"]
    original = manager.best.copy()
    (manager.root / "deliverable.py").write_bytes(b"x" * 2048)
    other = _manager(tmp_path, max_total_bytes=1024)
    event = other._consider_inline(
        outcome=_outcome(1, task_id="new"), result={}, accepted_commands=set()
    )
    assert event["status"] == "checkpoint_unavailable"
    assert "size_budget" in event["reason"]
    expired = other.consider(
        outcome=_outcome(2, task_id="new"), result={}, accepted_commands=set(), remaining_seconds=0
    )
    assert expired["status"] == "checkpoint_time_budget_exhausted"
    assert manager.best == original


def test_mutated_archive_cannot_be_resumed_as_verified(tmp_path):
    manager = _manager(tmp_path)
    (manager.root / "deliverable.py").write_text("pass\n")
    # Isolate corruption rejection from worker startup. A failed worker must
    # never count as successful archive-integrity validation.
    event = manager._consider_inline(outcome=_outcome(0), result={}, accepted_commands=set())
    assert event["status"] == "verified_checkpoint_preserved", event
    Path(manager.best["archive_path"]).write_bytes(b"corrupted")
    resumed = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    resumed._select_task_inline("task-one", acknowledged_checkpoint=manager.best)
    assert resumed.best is None
    assert resumed._attempted == set(), "Archive validation must finish without granting recovery"


@pytest.mark.parametrize("stop", ["provider", "deadline"])
def test_real_turn_preserves_verified_program_when_later_candidate_and_provider_fail(
    tmp_path, monkeypatch, stop
):
    from alysis_code.agent_loop import create_session
    from alysis_code.config import AppConfig
    from alysis_code.llm.openai_compat import LLMError, LLMResponse, ToolCall
    from alysis_code.surface import ApprovalDecision
    from alysis_code.surface.noop_surface import NoopSurface

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    monkeypatch.setattr(
        NoopSurface,
        "request_approval",
        lambda self, request: ApprovalDecision(request.kind == "shell_run"),
    )
    (tmp_path / "check.py").write_text(
        "import deliverable\nassert deliverable.value == 7\nprint('check passed')\n"
    )
    command = '"' + sys.executable.replace("\\", "/") + '" check.py'

    class Client:
        model = "test-model"
        temperature = 0.2
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                tool = ToolCall(
                    id="good",
                    name="fs_write",
                    arguments={"path": "deliverable.py", "content": "value = 7\n"},
                )
            elif self.calls == 2:
                tool = ToolCall(id="check", name="shell_run", arguments={"cmd": command})
            elif self.calls == 3:
                tool = ToolCall(
                    id="worse",
                    name="fs_write",
                    arguments={"path": "deliverable.py", "content": "value = 999\n"},
                )
            else:
                if stop == "deadline":
                    from alysis_code.cancellation import CooperativeCancellationError

                    raise CooperativeCancellationError("run_budget_exhausted")
                import httpx

                raise LLMError("Provider unavailable after retries") from httpx.ConnectError(
                    "connection refused"
                )
            return LLMResponse(content="", tool_calls=[tool], raw={})

    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", verify_commands=[command]),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=True,
        session_log_dir_override=tmp_path / "logs",
    )
    session.client = Client()
    try:
        session.run_turn(f"Implement deliverable.py with value 7 and verify using `{command}`.")
        outcome = session.last_turn_outcome
        assert outcome["verified_success"] is False
        assert outcome["outcome"] == (
            "provider_failure" if stop == "provider" else "deadline_exceeded"
        )
        best = outcome["best_verified_checkpoint"]
        with zipfile.ZipFile(best["archive_path"]) as archive:
            assert archive.read("deliverable.py") == b"value = 7\n"
            assert not any(name.startswith("logs/") for name in archive.namelist())
        assert (tmp_path / "deliverable.py").read_text() == "value = 999\n"
        finals = [
            event["payload"]["content"]
            for event in session.store.events_snapshot()
            if event["type"] == "final"
        ]
        assert best["archive_path"] in finals[-1]
    finally:
        session.close()


def test_configured_candidate_budget_survives_resume_and_counts_failed_experiment(tmp_path):
    manager = _manager(tmp_path, objective_command="python measure.py", max_candidates=2)
    (manager.root / "deliverable.py").write_text("pass\n")
    manager.consider(outcome=_outcome(0), result={}, accepted_commands=set())
    assert not manager.experiments_exhausted
    manager.consider(outcome=_outcome(1, passed=False), result={}, accepted_commands=set())
    assert manager.experiments_exhausted
    resumed = AnytimeCheckpointManager(
        root=manager.root, layout=manager.layout, config=manager.config
    )
    resumed.select_task("task-one", acknowledged_checkpoint=manager.best)
    assert resumed.experiments_exhausted
