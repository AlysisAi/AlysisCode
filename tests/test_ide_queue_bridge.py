from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.change_ledger import ChangeLedger, ChangeLedgerError
from alysis_code.ide.prompt_queue import DurablePromptQueue
from alysis_code.ide.stdio_bridge import StdioBridge


def _request(method: str, params: dict[str, Any], request_id: str) -> str:
    return (
        json.dumps(
            {
                "protocol_version": "1",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        + "\n"
    )


def _lines(output: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]


def _response(output: io.StringIO, request_id: str) -> dict[str, Any]:
    return next(item for item in _lines(output) if item.get("id") == request_id)


def _wait(output: io.StringIO, predicate: Any, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in _lines(output):
            if predicate(item):
                return item
        time.sleep(0.01)
    raise AssertionError("timed out waiting for bridge event")


@pytest.fixture
def configured_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    return workspace


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_checkpoint_failure_blocks_edits_and_recovery_preserves_turn_outcome(
    configured_workspace: Path, tmp_path: Path, outcome: str
) -> None:
    output = io.StringIO()
    events: list[dict[str, Any]] = []
    captures: list[str] = []
    messages: list[str] = []

    class MemoryStore:
        session_artifact_root = tmp_path / "artifacts"

        def append(self, event_type: str, payload: dict[str, Any]) -> None:
            events.append({"type": event_type, "payload": payload})

        def events_snapshot(self) -> list[dict[str, Any]]:
            return list(events)

    class FakeSession:
        store = MemoryStore()

        def __init__(self, guard: Any) -> None:
            self.guard = guard

        def run_turn(self, message: str) -> int:
            self.guard.check_tool_call("file_edit", {})
            (configured_workspace / "value.txt").write_text(message, encoding="utf-8")
            self.workspace_touched_paths.add("value.txt")
            messages.append(message)
            if outcome == "failed":
                raise RuntimeError("turn failed independently of checkpoints")
            if outcome == "cancelled":
                raise stdio_bridge.BridgeCancellationError("cancelled_by_user")
            return 0

        def close(self) -> None:
            pass

    class FailedLedger:
        def ensure_baseline(self, session_id: str) -> None:
            raise ChangeLedgerError("storage unavailable")

        def capture(self, session_id: str, **kwargs: Any) -> SimpleNamespace:
            captures.append(session_id)
            return SimpleNamespace(
                checkpoint_id="unexpected-checkpoint",
                changes=[],
                omitted_paths=[],
                non_revertible_paths=[],
            )

    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **kwargs: FakeSession(kwargs["tool_dispatch_guard"]),
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    try:
        bridge.process_line(
            _request(
                "session.create",
                {
                    "workspace": os.fspath(configured_workspace),
                    "mode": "readonly",
                    "session_id": "failed-checkpoint-session",
                },
                "create",
            )
        )
        assert _response(output, "create")["ok"] is True, _response(output, "create")
        session = bridge._sessions["failed-checkpoint-session"]
        session.change_ledger = FailedLedger()
        bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": session.session_id,
                    "message": "blocked edit",
                    "idempotency_key": "failed-checkpoint-turn",
                },
                "send",
            )
        )
        job_id = _response(output, "send")["result"]["job_id"]
        job = bridge._jobs[job_id]
        assert job.thread is not None
        job.thread.join(timeout=5)
        assert not job.thread.is_alive()
        assert job.status == "failed"
        assert messages == []
        assert not (configured_workspace / "value.txt").exists()
        assert captures == []
        assert session.change_ledger is None
        assert any(event["type"] == "ide_prompt_started" for event in events)
        assert "checkpoint_unavailable" in output.getvalue()
        session.change_ledger = ChangeLedger(configured_workspace)
        bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": session.session_id,
                    "message": "retry with checkpoints",
                    "idempotency_key": "recovered-checkpoint-turn",
                },
                "retry",
            )
        )
        recovered = bridge._jobs[_response(output, "retry")["result"]["job_id"]]
        assert recovered.thread is not None
        recovered.thread.join(timeout=10)
        assert not recovered.thread.is_alive()
        assert recovered.status == outcome
        assert messages == ["retry with checkpoints"]
        assert (configured_workspace / "value.txt").read_text(encoding="utf-8") == messages[0]
        assert session.change_ledger is not None
    finally:
        bridge.close()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_text_only_turn_never_waits_for_or_warns_about_checkpoint_storage(
    configured_workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    outcome: str,
) -> None:
    output = io.StringIO()
    events: list[str] = []

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Text-only turns must not initialize or capture checkpoints")

    ledger = SimpleNamespace(ensure_baseline=unexpected, capture=unexpected)
    monkeypatch.setattr(stdio_bridge, "ChangeLedger", unexpected)

    class FakeSession:
        store = SimpleNamespace(
            session_artifact_root=tmp_path / "artifacts",
            append=lambda kind, payload: events.append(kind),
            events_snapshot=lambda: [],
        )

        def run_turn(self, message: str) -> int:
            if outcome == "failed":
                raise RuntimeError("provider failed")
            if outcome == "cancelled":
                raise stdio_bridge.BridgeCancellationError("cancelled_by_user")
            return 0

        def close(self) -> None:
            pass

    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **kwargs: FakeSession(),
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    try:
        bridge.process_line(
            _request(
                "session.create",
                {
                    "workspace": os.fspath(configured_workspace),
                    "mode": "review",
                    "session_id": "text-only",
                },
                "create",
            )
        )
        assert _response(output, "create")["ok"] is True
        session = bridge._sessions["text-only"]
        session.change_ledger = ledger if existing else None
        bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": session.session_id,
                    "message": "hi",
                    "idempotency_key": "greeting",
                },
                "send",
            )
        )
        job = bridge._jobs[_response(output, "send")["result"]["job_id"]]
        assert job.thread is not None
        job.thread.join(timeout=5)
        assert not job.thread.is_alive()
        assert job.status == outcome
        assert session.change_ledger is (ledger if existing else None)
        assert "ide_prompt_started" in events
        assert "Checkpoint" not in output.getvalue()
        if outcome == "completed":
            assert "ide_prompt_completed" in events
            assert job.result["checkpoint_id"] is None
            assert job.result["changed_files"] == []
    finally:
        bridge.close()


def test_chat_send_queues_and_deletes_follow_up(configured_workspace: Path, tmp_path: Path) -> None:
    output = io.StringIO()
    release = threading.Event()
    started = threading.Event()
    messages: list[str] = []

    class FakeSession:
        store = SimpleNamespace(session_artifact_root=tmp_path / "artifacts")

        def run_turn(self, message: str) -> int:
            messages.append(message)
            if len(messages) == 1:
                started.set()
                assert release.wait(timeout=5)
            return 0

        def close(self) -> None:
            return

    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **_kwargs: FakeSession(),
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(configured_workspace),
                "mode": "review",
                "model": "test-model",
                "session_id": "queue-session",
            },
            "create",
        )
    )
    bridge.process_line(
        _request(
            "chat.send",
            {
                "session_id": "queue-session",
                "message": "first",
                "idempotency_key": "request-one",
            },
            "first",
        )
    )
    assert started.wait(timeout=5)
    bridge.process_line(
        _request(
            "chat.send",
            {
                "session_id": "queue-session",
                "message": "second",
                "idempotency_key": "request-two",
            },
            "second",
        )
    )

    first = _response(output, "first")["result"]
    second = _response(output, "second")["result"]
    assert first["status"] == "started"
    assert second["status"] == "queued"

    bridge.process_line(_request("chat.queue.list", {"session_id": "queue-session"}, "queue-list"))
    queue_items = _response(output, "queue-list")["result"]["items"]
    assert [item["state"] for item in queue_items] == ["running", "pending"]

    bridge.process_line(
        _request(
            "chat.queue.delete",
            {"session_id": "queue-session", "prompt_id": second["prompt_id"]},
            "delete",
        )
    )
    assert _response(output, "delete")["result"]["state"] == "cancelled"
    release.set()
    _wait(
        output,
        lambda item: (
            item.get("type") == "info_emitted"
            and f"job_completed {first['job_id']}" in str(item.get("payload", {}).get("message"))
        ),
    )
    assert messages == ["first"]
    bridge.close()


def test_chat_send_injects_validated_context_and_rejects_sensitive_paths(
    configured_workspace: Path, tmp_path: Path
) -> None:
    output = io.StringIO()
    captured: list[str] = []
    captured_context: list[str] = []
    source = configured_workspace / "app.py"
    source.write_text("answer = 42\n", encoding="utf-8")
    (configured_workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    class FakeSession:
        store = SimpleNamespace(session_artifact_root=tmp_path / "artifacts")

        def run_turn(
            self, message: str, *, ephemeral_user_messages: list[str] | None = None
        ) -> int:
            captured.append(message)
            captured_context.extend(ephemeral_user_messages or [])
            return 0

        def close(self) -> None:
            return

    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **_kwargs: FakeSession(),
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(configured_workspace),
                "mode": "readonly",
                "model": "test-model",
            },
            "create",
        )
    )
    session_id = _response(output, "create")["result"]["session_id"]
    selection = {
        "type": "selection",
        "path": "app.py",
        "content": "answer = 42",
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 11},
        },
        "provenance": {"source": "vscode.selection", "version": 1},
    }
    bridge.process_line(
        _request(
            "chat.send",
            {
                "session_id": session_id,
                "message": "explain",
                "context_blocks": [selection],
                "idempotency_key": "context-one",
            },
            "context",
        )
    )
    job_id = _response(output, "context")["result"]["job_id"]
    _wait(
        output,
        lambda item: (
            item.get("type") == "info_emitted"
            and f"job_completed {job_id}" in str(item.get("payload", {}).get("message"))
        ),
    )
    assert captured == ["explain"]
    assert "IDE CONTEXT (untrusted data" in captured_context[0]
    assert '"path":"app.py"' in captured_context[0]

    sensitive = dict(selection)
    sensitive["path"] = ".env"
    bridge.process_line(
        _request(
            "chat.send",
            {
                "session_id": session_id,
                "message": "inspect secret",
                "context_blocks": [sensitive],
                "idempotency_key": "context-two",
            },
            "sensitive",
        )
    )
    rejected = _response(output, "sensitive")
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "sensitive_path"
    assert "TOKEN=secret" not in output.getvalue()
    bridge.close()


def test_chat_turn_checkpoint_revert_and_redo(configured_workspace: Path, tmp_path: Path) -> None:
    output = io.StringIO()
    target = configured_workspace / "value.txt"
    target.write_text("before\n", encoding="utf-8")

    class MemoryStore:
        session_artifact_root = tmp_path / "artifacts"

        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def append(self, event_type: str, payload: dict[str, Any]) -> None:
            self.events.append({"type": event_type, "payload": payload})

        def events_snapshot(self) -> list[dict[str, Any]]:
            return list(self.events)

    class FakeSession:
        def __init__(self, guard: Any) -> None:
            self.store = MemoryStore()
            self.guard = guard

        def run_turn(self, _message: str) -> int:
            self.guard.check_tool_call("file_edit", {"path": "value.txt"})
            target.write_text("after\n", encoding="utf-8")
            # Real agent turns populate this set from successful mutating tool results. The fake
            # must model that contract instead of relying on an unsafe whole-workspace scan.
            self.workspace_touched_paths.add(target.relative_to(configured_workspace).as_posix())
            return 0

        def close(self) -> None:
            return

    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **kwargs: FakeSession(kwargs["tool_dispatch_guard"]),
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(configured_workspace),
                "mode": "review",
                "model": "test-model",
            },
            "create",
        )
    )
    session_id = _response(output, "create")["result"]["session_id"]
    bridge.process_line(
        _request(
            "chat.send",
            {
                "session_id": session_id,
                "message": "change value",
                "idempotency_key": "checkpoint-turn",
            },
            "chat",
        )
    )
    job_id = _response(output, "chat")["result"]["job_id"]
    _wait(
        output,
        lambda item: (
            item.get("type") == "info_emitted"
            and f"job_completed {job_id}" in str(item.get("payload", {}).get("message"))
        ),
    )
    bridge.process_line(_request("job.status", {"job_id": job_id}, "job"))
    checkpoint_id = _response(output, "job")["result"]["result"]["checkpoint_id"]
    assert checkpoint_id

    bridge.process_line(
        _request(
            "checkpoint.diff",
            {"session_id": session_id, "checkpoint_id": checkpoint_id},
            "diff",
        )
    )
    assert "-before" in _response(output, "diff")["result"]["diff"]
    assert "+after" in _response(output, "diff")["result"]["diff"]

    bridge.process_line(
        _request(
            "checkpoint.revert",
            {
                "session_id": session_id,
                "checkpoint_id": checkpoint_id,
                "workspace_trusted": True,
                "confirm": True,
            },
            "revert",
        )
    )
    assert _response(output, "revert")["ok"] is True
    assert target.read_text(encoding="utf-8") == "before\n"

    bridge.process_line(
        _request(
            "checkpoint.redo",
            {"session_id": session_id, "workspace_trusted": True, "confirm": True},
            "redo",
        )
    )
    assert _response(output, "redo")["ok"] is True
    assert target.read_text(encoding="utf-8") == "after\n"
    bridge.close()


def test_session_resume_adopts_checkpoint_lineage_and_denies_other_sessions(
    configured_workspace: Path, tmp_path: Path
) -> None:
    target = configured_workspace / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    old_ledger = ChangeLedger(configured_workspace)
    old_ledger.ensure_baseline("old-session")
    target.write_text("after\n", encoding="utf-8")
    turn = old_ledger.capture("old-session", turn_id="turn-1", paths=("value.txt",))

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "old-session.jsonl").write_text(
        json.dumps({"type": "user_message", "payload": {"content": "change"}}) + "\n",
        encoding="utf-8",
    )

    class Store:
        session_artifact_root = tmp_path / "artifacts"

        def __init__(self) -> None:
            self.sessions_dir = sessions_dir
            self.events: list[dict[str, Any]] = []

        def append(self, event_type: str, payload: dict[str, Any]) -> None:
            self.events.append({"type": event_type, "payload": payload})

        def events_snapshot(self) -> list[dict[str, Any]]:
            return list(self.events)

    class FakeSession:
        def __init__(self) -> None:
            self.store = Store()
            self.messages: list[dict[str, Any]] = []

        def close(self) -> None:
            return

    output = io.StringIO()
    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **_kwargs: FakeSession(),
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    for session_id in ("new-session", "unrelated-session"):
        bridge.process_line(
            _request(
                "session.create",
                {
                    "workspace": os.fspath(configured_workspace),
                    "mode": "review",
                    "model": "test-model",
                    "session_id": session_id,
                },
                f"create-{session_id}",
            )
        )
        assert _response(output, f"create-{session_id}")["ok"] is True

    bridge.process_line(
        _request(
            "checkpoint.diff",
            {"session_id": "new-session", "checkpoint_id": turn.checkpoint_id},
            "diff-before-resume",
        )
    )
    denied_before_resume = _response(output, "diff-before-resume")
    assert denied_before_resume["ok"] is False
    assert denied_before_resume["error"]["code"] == "checkpoint_error"

    bridge.process_line(
        _request(
            "session.resume",
            {"session_id": "new-session", "target_session_id": "old-session"},
            "resume",
        )
    )
    resumed = _response(output, "resume")
    assert resumed["ok"] is True
    assert resumed["result"]["checkpoint_lineage_adopted"] is True
    assert resumed["result"]["checkpoint_lineage_session_id"] == "old-session"

    bridge.process_line(
        _request("checkpoint.list", {"session_id": "new-session"}, "list-after-resume")
    )
    listed = _response(output, "list-after-resume")
    assert listed["ok"] is True
    assert turn.checkpoint_id in {item["checkpoint_id"] for item in listed["result"]["checkpoints"]}

    bridge.process_line(
        _request(
            "checkpoint.diff",
            {"session_id": "new-session", "checkpoint_id": turn.checkpoint_id},
            "diff-after-resume",
        )
    )
    assert "+after" in _response(output, "diff-after-resume")["result"]["diff"]

    bridge.process_line(
        _request(
            "checkpoint.diff",
            {"session_id": "unrelated-session", "checkpoint_id": turn.checkpoint_id},
            "cross-session-diff",
        )
    )
    cross_session = _response(output, "cross-session-diff")
    assert cross_session["ok"] is False
    assert cross_session["error"]["code"] == "checkpoint_error"

    bridge.process_line(
        _request(
            "checkpoint.branch",
            {
                "session_id": "unrelated-session",
                "checkpoint_id": turn.checkpoint_id,
                "name": "forbidden",
            },
            "cross-session-branch",
        )
    )
    assert _response(output, "cross-session-branch")["ok"] is False

    bridge.process_line(
        _request(
            "checkpoint.revert",
            {
                "session_id": "new-session",
                "checkpoint_id": turn.checkpoint_id,
                "workspace_trusted": True,
                "confirm": True,
            },
            "revert-after-resume",
        )
    )
    assert _response(output, "revert-after-resume")["ok"] is True
    assert target.read_text(encoding="utf-8") == "before\n"

    bridge.process_line(
        _request(
            "checkpoint.redo",
            {"session_id": "new-session", "workspace_trusted": True, "confirm": True},
            "redo-after-resume",
        )
    )
    assert _response(output, "redo-after-resume")["ok"] is True
    assert target.read_text(encoding="utf-8") == "after\n"

    bridge.process_line(
        _request(
            "session.resume",
            {"session_id": "new-session", "target_session_id": "old-session"},
            "resume-again",
        )
    )
    assert _response(output, "resume-again")["result"]["checkpoint_lineage_adopted"] is True
    bridge.close()


def test_started_prompt_is_not_reexecuted_by_new_bridge_and_new_session(
    configured_workspace: Path, tmp_path: Path
) -> None:
    now = [100.0]
    queue_path = tmp_path / "queue.sqlite3"
    old_queue = DurablePromptQueue(queue_path, clock=lambda: now[0])
    queued = old_queue.enqueue(
        session_id="old-session",
        idempotency_key="original-send",
        payload={
            "message": "dangerous work",
            "image_paths": [],
            "basket_image_paths": [],
            "context": {},
            "context_prompt": "",
        },
    ).item
    lease = old_queue.claim_next(session_id="old-session", owner_id="old-bridge", lease_seconds=1)
    assert lease is not None
    old_queue.mark_execution_started(
        session_id="old-session",
        prompt_id=queued.prompt_id,
        lease_token=lease.lease_token,
    )
    now[0] = 102.0
    turns: list[str] = []
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "old-session.jsonl").write_text(
        json.dumps(
            {
                "type": "ide_prompt_started",
                "payload": {"prompt_id": queued.prompt_id},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class FakeSession:
        def __init__(self) -> None:
            self.store = SimpleNamespace(
                session_artifact_root=tmp_path / "artifacts",
                sessions_dir=sessions_dir,
            )
            self.messages: list[dict[str, Any]] = []

        def run_turn(self, message: str) -> int:
            turns.append(message)
            return 0

        def close(self) -> None:
            return

    output = io.StringIO()
    restarted_queue = DurablePromptQueue(queue_path, clock=lambda: now[0])
    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **_kwargs: FakeSession(),
        prompt_queue=restarted_queue,
        prompt_queue_owner_id="new-bridge",
    )
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(configured_workspace),
                "mode": "review",
                "model": "test-model",
                "session_id": "new-session",
            },
            "create",
        )
    )
    bridge.process_line(
        _request(
            "session.resume",
            {
                "session_id": "new-session",
                "target_session_id": "old-session",
            },
            "resume",
        )
    )

    resume = _response(output, "resume")
    assert resume["ok"] is True
    assert resume["result"]["expired_prompts_recovered"] == 1
    recovered = restarted_queue.get(session_id="new-session", prompt_id=queued.prompt_id)
    assert recovered is not None
    assert recovered.state.value == "failed"
    assert recovered.error_code == "interrupted_indeterminate"
    assert turns == []
    assert "Automatic retry was stopped" in output.getvalue()
    bridge.close()


def test_new_bridge_observes_live_old_session_lease_before_draining_follow_up(
    configured_workspace: Path, tmp_path: Path
) -> None:
    queue_path = tmp_path / "queue.sqlite3"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    release_first = threading.Event()
    first_started = threading.Event()
    old_turns: list[str] = []
    new_turns: list[str] = []

    class FileStore:
        def __init__(self, session_id: str) -> None:
            self.sessions_dir = sessions_dir
            self.session_artifact_root = tmp_path / "artifacts" / session_id
            self.path = sessions_dir / f"{session_id}.jsonl"
            self._lock = threading.Lock()
            self.path.touch()

        def append(self, event_type: str, payload: dict[str, Any]) -> None:
            with self._lock, self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": event_type, "payload": payload}) + "\n")

    class OldSession:
        def __init__(self) -> None:
            self.store = FileStore("old-session")
            self.messages: list[dict[str, Any]] = []

        def run_turn(self, message: str) -> int:
            old_turns.append(message)
            first_started.set()
            assert release_first.wait(timeout=5)
            return 0

        def close(self) -> None:
            return

    class NewSession:
        def __init__(self) -> None:
            self.store = FileStore("new-session")
            self.messages: list[dict[str, Any]] = []

        def run_turn(self, message: str) -> int:
            new_turns.append(message)
            return 0

        def close(self) -> None:
            return

    old_output = io.StringIO()
    old_bridge = StdioBridge(
        stdout=old_output,
        create_session_fn=lambda **_kwargs: OldSession(),
        prompt_queue=DurablePromptQueue(queue_path),
        prompt_queue_owner_id="old-bridge",
    )
    old_bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(configured_workspace),
                "mode": "review",
                "model": "test-model",
                "session_id": "old-session",
            },
            "old-create",
        )
    )
    for request_id, message, key in (
        ("first", "first work", "send-first"),
        ("follow-up", "follow-up work", "send-follow-up"),
    ):
        old_bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": "old-session",
                    "message": message,
                    "idempotency_key": key,
                },
                request_id,
            )
        )
        if request_id == "first":
            assert first_started.wait(timeout=5)

    follow_up_id = _response(old_output, "follow-up")["result"]["prompt_id"]
    new_output = io.StringIO()
    restarted_queue = DurablePromptQueue(queue_path)
    new_bridge = StdioBridge(
        stdout=new_output,
        create_session_fn=lambda **_kwargs: NewSession(),
        prompt_queue=restarted_queue,
        prompt_queue_owner_id="new-bridge",
    )
    new_bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(configured_workspace),
                "mode": "review",
                "model": "test-model",
                "session_id": "new-session",
            },
            "new-create",
        )
    )
    new_bridge.process_line(
        _request(
            "session.resume",
            {
                "session_id": "new-session",
                "target_session_id": "old-session",
            },
            "resume",
        )
    )

    resume = _response(new_output, "resume")
    assert resume["ok"] is True
    assert resume["result"]["active_prompts_observed"] == 1
    assert [item.state.value for item in restarted_queue.list(session_id="new-session")] == [
        "running",
        "pending",
    ]
    assert new_turns == []

    release_first.set()
    _wait(
        new_output,
        lambda item: (
            item.get("type") == "info_emitted"
            and f"job_completed {follow_up_id}" in str(item.get("payload", {}).get("message"))
        ),
    )
    assert old_turns == ["first work"]
    assert new_turns == ["follow-up work"]
    assert [item.state.value for item in restarted_queue.list(session_id="new-session")] == [
        "completed",
        "completed",
    ]
    old_bridge.close()
    new_bridge.close()


def test_explicit_skill_is_per_turn_and_part_of_queue_identity(configured_workspace, tmp_path):
    from alysis_code.skills import discover_skills

    bundle = configured_workspace / ".alysis_skills" / "review"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review source changes\n---\nReview $ARGUMENTS and report findings.\n",
        encoding="utf-8",
    )
    registry = discover_skills(
        focus_path=configured_workspace, workspace_root=configured_workspace, cfg=AppConfig()
    ).skills
    output = io.StringIO()
    captured = []

    class FakeSession:
        skills_enabled = True
        skill_registry = registry

        def run_turn(self, message, *, ephemeral_user_messages=None):
            captured.append((message, list(ephemeral_user_messages or [])))
            return 0

        def close(self):
            pass

    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **kw: FakeSession(),
        prompt_queue=DurablePromptQueue(tmp_path / "skills.sqlite3"),
    )
    bridge.process_line(
        _request(
            "session.create", {"workspace": str(configured_workspace), "mode": "readonly"}, "create"
        )
    )
    session_id = _response(output, "create")["result"]["session_id"]
    selection = {"name": "review", "arguments": "these changes"}
    params = {
        "session_id": session_id,
        "message": "history plus user task",
        "skill": selection,
        "idempotency_key": "first",
    }
    bridge.process_line(_request("chat.send", params, "first"))
    job_id = _response(output, "first")["result"]["job_id"]
    _wait(
        output,
        lambda event: f"job_completed {job_id}" in str(event.get("payload", {}).get("message")),
    )
    activity = _wait(
        output, lambda event: event.get("payload", {}).get("operation") == "skill_invoke"
    )
    assert activity["payload"]["kind"] == "read"
    assert activity["payload"]["display_title"] == "Using review"
    assert captured[0][0] == "history plus user task"
    assert len(captured[0][1]) == 1
    assert "Review these changes and report findings." in captured[0][1][0]
    assert "history plus user task" not in captured[0][1][0]
    bridge.process_line(_request("chat.send", params, "retry"))
    assert _response(output, "retry")["result"]["created"] is False
    bridge.process_line(
        _request(
            "chat.send",
            {**params, "skill": {**selection, "arguments": "different task"}},
            "conflict",
        )
    )
    assert _response(output, "conflict")["ok"] is False
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "ordinary follow-up"}, "second")
    )
    job_id = _response(output, "second")["result"]["job_id"]
    _wait(
        output,
        lambda event: f"job_completed {job_id}" in str(event.get("payload", {}).get("message")),
    )
    assert captured == [("history plus user task", captured[0][1]), ("ordinary follow-up", [])]
    bridge.close()


@pytest.mark.parametrize(
    "value,enabled,sensitive,code",
    [
        ({"name": "missing", "arguments": "task"}, True, False, "skill_not_found"),
        ({"name": "review", "arguments": "task"}, False, False, "skills_unavailable"),
        ({"name": "review", "arguments": "task"}, True, True, "skills_unavailable"),
        ({"name": "review", "instructions": "injected"}, True, False, "invalid_skill"),
        ("review", True, False, "invalid_skill"),
    ],
)
def test_explicit_skill_validation_fails_closed(value, enabled, sensitive, code):
    from alysis_code.ide.protocol import ProtocolError
    from alysis_code.ide.skill_invocation import validate_skill_invocation

    session = SimpleNamespace(skills_enabled=enabled, skill_registry={})
    with pytest.raises(ProtocolError) as error:
        validate_skill_invocation(session, value, sensitive_media=sensitive)
    assert error.value.code == code


def test_commands_catalog_matches_cli_and_advertises_skills():
    from alysis_code.cli_impl.chat_slash_completer import get_chat_specs
    from alysis_code.ide.health import capabilities_payload
    from alysis_code.ide.management_protocol import _commands_list

    assert _commands_list({}, None)["commands"] == [
        {"name": spec.name, "usage": spec.usage, "description": spec.description}
        for spec in get_chat_specs()
    ]
    assert "commands.list" in capabilities_payload()["methods"]
    assert capabilities_payload()["features"]["explicit_skill_invocation"]["scope"] == "turn"
