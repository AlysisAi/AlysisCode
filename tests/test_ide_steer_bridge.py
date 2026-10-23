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

from alysis_code.agent.steering import steer_inbox_for
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.health import SUPPORTED_METHODS, capabilities_payload
from alysis_code.ide.prompt_queue import DurablePromptQueue
from alysis_code.ide.stdio_bridge import SENSITIVE_MEDIA_ALLOWED_SESSION_METHODS, StdioBridge


def _request(method: str, params: dict[str, Any], request_id: str) -> str:
    return (
        json.dumps({"protocol_version": "1", "id": request_id, "method": method, "params": params})
        + "\n"
    )


def _lines(output: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]


def _response(output: io.StringIO, request_id: str) -> dict[str, Any]:
    return next(item for item in _lines(output) if item.get("id") == request_id)


def _wait_job_completed(output: io.StringIO, job_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in _lines(output):
            message = str(item.get("payload", {}).get("message"))
            if item.get("type") == "info_emitted" and f"job_completed {job_id}" in message:
                return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not complete")


@pytest.fixture
def configured_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    return workspace


def _bridge(output: io.StringIO, tmp_path: Path, session: Any, workspace: Path) -> StdioBridge:
    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **_kwargs: session,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(workspace),
                "mode": "review",
                "model": "test-model",
                "session_id": "steer-session",
            },
            "create",
        )
    )
    return bridge


class _BlockingSession:
    """Runs one turn that waits to be released, then reads steering like a step boundary."""

    def __init__(self, tmp_path: Path, *, read_steering: bool) -> None:
        self.store = SimpleNamespace(session_artifact_root=tmp_path / "artifacts")
        self.started = threading.Event()
        self.release = threading.Event()
        self.read_steering = read_steering
        self.read: list[str] = []

    def run_turn(self, message: str) -> int:
        self.started.set()
        assert self.release.wait(timeout=5)
        if self.read_steering:
            inbox = steer_inbox_for(self)
            self.read.extend(inbox.drain() if inbox is not None else [])
        return 0

    def close(self) -> None:
        return


def test_chat_steer_reaches_the_running_turn_and_is_refused_when_idle(
    configured_workspace: Path, tmp_path: Path
) -> None:
    output = io.StringIO()
    session = _BlockingSession(tmp_path, read_steering=True)
    bridge = _bridge(output, tmp_path, session, configured_workspace)
    try:
        bridge.process_line(
            _request("chat.steer", {"session_id": "steer-session", "message": "early"}, "idle")
        )
        assert _response(output, "idle")["error"]["code"] == "session_idle"

        bridge.process_line(
            _request(
                "chat.send",
                {"session_id": "steer-session", "message": "first", "idempotency_key": "one"},
                "send",
            )
        )
        assert session.started.wait(timeout=5)
        job_id = _response(output, "send")["result"]["job_id"]

        bridge.process_line(
            _request(
                "chat.steer",
                {"session_id": "steer-session", "message": "  use tabs, not spaces  "},
                "steer",
            )
        )
        steered = _response(output, "steer")["result"]
        assert steered == {
            "session_id": "steer-session",
            "job_id": job_id,
            "accepted": True,
            "pending": 1,
            "dropped": 0,
            "truncated": False,
        }

        # Taking during the turn leaves the turn's own message with it.
        bridge.process_line(_request("chat.steer.take", {"session_id": "steer-session"}, "during"))
        assert _response(output, "during")["result"]["messages"] == []

        session.release.set()
        _wait_job_completed(output, job_id)
        assert session.read == ["use tabs, not spaces"]

        bridge.process_line(_request("chat.steer.take", {"session_id": "steer-session"}, "take"))
        assert _response(output, "take")["result"] == {
            "session_id": "steer-session",
            "messages": [],
        }
    finally:
        session.release.set()
        bridge.close()


def test_chat_steer_take_returns_what_the_turn_did_not_read(
    configured_workspace: Path, tmp_path: Path
) -> None:
    output = io.StringIO()
    session = _BlockingSession(tmp_path, read_steering=False)
    bridge = _bridge(output, tmp_path, session, configured_workspace)
    try:
        bridge.process_line(
            _request(
                "chat.send",
                {"session_id": "steer-session", "message": "first", "idempotency_key": "one"},
                "send",
            )
        )
        assert session.started.wait(timeout=5)
        job_id = _response(output, "send")["result"]["job_id"]
        for index, text in enumerate(["late one", "late two"]):
            bridge.process_line(
                _request(
                    "chat.steer", {"session_id": "steer-session", "message": text}, f"s{index}"
                )
            )
        assert _response(output, "s1")["result"]["pending"] == 2

        session.release.set()
        _wait_job_completed(output, job_id)
        bridge.process_line(_request("chat.steer.take", {"session_id": "steer-session"}, "take"))
        assert _response(output, "take")["result"]["messages"] == ["late one", "late two"]
        bridge.process_line(_request("chat.steer.take", {"session_id": "steer-session"}, "again"))
        assert _response(output, "again")["result"]["messages"] == []
    finally:
        session.release.set()
        bridge.close()


def test_chat_steer_rejects_an_empty_message(configured_workspace: Path, tmp_path: Path) -> None:
    output = io.StringIO()
    session = _BlockingSession(tmp_path, read_steering=True)
    bridge = _bridge(output, tmp_path, session, configured_workspace)
    try:
        bridge.process_line(
            _request(
                "chat.send",
                {"session_id": "steer-session", "message": "first", "idempotency_key": "one"},
                "send",
            )
        )
        assert session.started.wait(timeout=5)
        bridge.process_line(
            _request("chat.steer", {"session_id": "steer-session", "message": "   "}, "empty")
        )
        assert _response(output, "empty")["error"]["code"] == "missing_field"
        assert steer_inbox_for(session) is None or steer_inbox_for(session).pending_count() == 0
    finally:
        session.release.set()
        bridge.close()


def test_steering_is_advertised_and_closed_to_computer_use_sessions() -> None:
    assert {"chat.steer", "chat.steer.take"} <= set(SUPPORTED_METHODS)
    steering = capabilities_payload()["features"]["steering"]
    assert steering["supported"] is True
    assert steering["methods"] == ["chat.steer", "chat.steer.take"]
    assert steering["max_pending"] == 16
    # Sensitive (Computer Use) sessions accept only an allowlist; steering is not on it.
    assert "chat.steer" not in SENSITIVE_MEDIA_ALLOWED_SESSION_METHODS
    assert "chat.steer.take" not in SENSITIVE_MEDIA_ALLOWED_SESSION_METHODS


class _QueuedTurnsSession(_BlockingSession):
    """The first turn waits and never reads steering; later turns record what they read."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path, read_steering=False)
        self.turns: list[str] = []
        self.read_by_later_turns: list[str] = []

    def run_turn(self, message: str) -> int:
        self.turns.append(message)
        if len(self.turns) == 1:
            return super().run_turn(message)
        inbox = steer_inbox_for(self)
        self.read_by_later_turns.extend(inbox.drain() if inbox is not None else [])
        return 0


def test_unread_steering_never_reaches_the_next_queued_turn(
    configured_workspace: Path, tmp_path: Path
) -> None:
    output = io.StringIO()
    session = _QueuedTurnsSession(tmp_path)
    bridge = _bridge(output, tmp_path, session, configured_workspace)
    try:
        bridge.process_line(
            _request(
                "chat.send",
                {"session_id": "steer-session", "message": "first", "idempotency_key": "one"},
                "send-1",
            )
        )
        assert session.started.wait(timeout=5)
        bridge.process_line(
            _request(
                "chat.send",
                {"session_id": "steer-session", "message": "second", "idempotency_key": "two"},
                "send-2",
            )
        )
        bridge.process_line(
            _request("chat.steer", {"session_id": "steer-session", "message": "late"}, "steer")
        )
        assert _response(output, "steer")["result"]["accepted"] is True
        second_job = _response(output, "send-2")["result"]["job_id"]
        session.release.set()
        _wait_job_completed(output, second_job)

        assert session.turns == ["first", "second"]
        assert session.read_by_later_turns == []
        bridge.process_line(_request("chat.steer.take", {"session_id": "steer-session"}, "take"))
        assert _response(output, "take")["result"]["messages"] == ["late"]
    finally:
        session.release.set()
        bridge.close()


def test_session_clear_discards_unread_steering(configured_workspace: Path, tmp_path: Path) -> None:
    output = io.StringIO()
    session = _BlockingSession(tmp_path, read_steering=False)
    session.messages = []
    bridge = _bridge(output, tmp_path, session, configured_workspace)
    try:
        bridge.process_line(
            _request(
                "chat.send",
                {"session_id": "steer-session", "message": "first", "idempotency_key": "one"},
                "send",
            )
        )
        assert session.started.wait(timeout=5)
        job_id = _response(output, "send")["result"]["job_id"]
        bridge.process_line(
            _request("chat.steer", {"session_id": "steer-session", "message": "late"}, "steer")
        )
        session.release.set()
        _wait_job_completed(output, job_id)
        bridge.process_line(_request("session.clear", {"session_id": "steer-session"}, "clear"))
        assert _response(output, "clear")["ok"] is True
        bridge.process_line(_request("chat.steer.take", {"session_id": "steer-session"}, "take"))
        assert _response(output, "take")["result"]["messages"] == []
    finally:
        session.release.set()
        bridge.close()


def test_chat_steer_reports_what_each_call_dropped_or_shortened(
    configured_workspace: Path, tmp_path: Path
) -> None:
    output = io.StringIO()
    session = _BlockingSession(tmp_path, read_steering=False)
    bridge = _bridge(output, tmp_path, session, configured_workspace)
    try:
        bridge.process_line(
            _request(
                "chat.send",
                {"session_id": "steer-session", "message": "first", "idempotency_key": "one"},
                "send",
            )
        )
        assert session.started.wait(timeout=5)
        for index in range(17):
            bridge.process_line(
                _request(
                    "chat.steer",
                    {"session_id": "steer-session", "message": f"note {index}"},
                    f"s{index}",
                )
            )
        assert [_response(output, f"s{index}")["result"]["dropped"] for index in range(17)] == [
            0
        ] * 16 + [1]
        bridge.process_line(
            _request("chat.steer", {"session_id": "steer-session", "message": "x" * 5000}, "long")
        )
        long_result = _response(output, "long")["result"]
        assert long_result["truncated"] is True
        assert long_result["dropped"] == 1
    finally:
        session.release.set()
        bridge.close()
