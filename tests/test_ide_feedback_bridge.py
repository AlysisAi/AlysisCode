from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from alysis_code import alysis_cloud
from alysis_code import feedback_delivery as delivery
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.health import SUPPORTED_METHODS, capabilities_payload
from alysis_code.ide.prompt_queue import DurablePromptQueue
from alysis_code.ide.stdio_bridge import (
    MAX_PREPARED_FEEDBACK,
    SENSITIVE_MEDIA_ALLOWED_SESSION_METHODS,
    StdioBridge,
)

SIGNED_IN = {"alysis": "slk_feedback_local_fixture_only"}
REAL_CLIENT = httpx.Client


def _request(method: str, params: dict[str, Any], request_id: str) -> str:
    return (
        json.dumps({"protocol_version": "1", "id": request_id, "method": method, "params": params})
        + "\n"
    )


def _response(output: io.StringIO, request_id: str) -> dict[str, Any]:
    lines = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
    return next(item for item in lines if item.get("id") == request_id)


def _call(bridge: StdioBridge, output: io.StringIO, method: str, params: dict[str, Any]) -> Any:
    request_id = f"{method}-{len(output.getvalue())}"
    bridge.process_line(_request(method, params, request_id))
    return _response(output, request_id)


class _Session:
    def __init__(self, tmp_path: Path) -> None:
        self.store = SimpleNamespace(session_artifact_root=tmp_path / "artifacts")
        self.messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Fix the failing test"},
            {"role": "assistant", "content": "I ran the tests and they froze."},
        ]

    def run_turn(self, message: str) -> int:
        return 0

    def close(self) -> None:
        return


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))
    monkeypatch.setattr(alysis_cloud, "feedback_url", lambda: "https://feedback.invalid/feedback")
    monkeypatch.setattr(delivery, "load_persisted_profile_keys", lambda: {})
    return root


@pytest.fixture
def bridge(tmp_path: Path, workspace: Path):
    output = io.StringIO()
    session = _Session(tmp_path)
    instance = StdioBridge(
        stdout=output,
        create_session_fn=lambda **_kwargs: session,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    created = _call(
        instance,
        output,
        "session.create",
        {
            "workspace": os.fspath(workspace),
            "mode": "review",
            "model": "test-model",
            "session_id": "feedback-session",
        },
    )
    assert "result" in created, created
    try:
        yield instance, output, session
    finally:
        instance.close()


def _server(monkeypatch: pytest.MonkeyPatch, sent: list[httpx.Request], status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if status != 200:
            return httpx.Response(status, text="server-detail=secret")
        report_id = json.loads(request.content)["report_id"]
        return httpx.Response(
            200,
            json={"accepted": True, "report_id": report_id, "created_at": "2026-10-09T09:00:00Z"},
        )

    monkeypatch.setattr(
        delivery.httpx,
        "Client",
        lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs),
    )


def test_feedback_methods_are_advertised() -> None:
    assert {"feedback.prepare", "feedback.send"} <= set(SUPPORTED_METHODS)
    feature = capabilities_payload()["features"]["feedback_delivery"]
    assert feature["methods"] == ["feedback.prepare", "feedback.send"]
    assert feature["anonymous_without_sign_in"] is True
    assert feature["diagnostics_opt_in"] is True
    # Computer Use sessions cannot hand their conversation to a report.
    assert "feedback.prepare" not in SENSITIVE_MEDIA_ALLOWED_SESSION_METHODS


def test_prepare_with_logs_previews_the_exact_bytes_and_send_delivers_them_anonymously(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, output, _session = bridge
    prepared = _call(
        instance,
        output,
        "feedback.prepare",
        {
            "session_id": "feedback-session",
            "feedback": "It froze after the first test",
            "include_diagnostics": True,
        },
    )["result"]
    assert prepared["include_diagnostics"] is True and prepared["signed_in"] is False
    report = prepared["report"]
    assert report["report_id"] == prepared["report_id"]
    assert report["description"] == "It froze after the first test"
    assert [message["content"] for message in report["diagnostics"]["messages"]] == [
        "Fix the failing test",
        "I ran the tests and they froze.",
    ]

    sent: list[httpx.Request] = []
    _server(monkeypatch, sent)
    receipt = _call(instance, output, "feedback.send", {"report_id": prepared["report_id"]})
    assert receipt["result"] == {
        "report_id": prepared["report_id"],
        "created_at": "2026-10-09T09:00:00Z",
        "anonymous": True,
    }
    assert len(sent) == 1 and "authorization" not in sent[0].headers
    assert json.loads(sent[0].content) == report


def test_secrets_never_reach_the_host_or_the_server(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, output, session = bridge
    session.messages.append({"role": "user", "content": "use password=synthetic123 here"})
    prepared = _call(
        instance,
        output,
        "feedback.prepare",
        {
            "session_id": "feedback-session",
            "feedback": "token=abc123secret broke it",
            "include_diagnostics": True,
        },
    )["result"]
    shown = json.dumps(prepared)
    assert "synthetic123" not in shown and "abc123secret" not in shown
    sent: list[httpx.Request] = []
    _server(monkeypatch, sent)
    _call(instance, output, "feedback.send", {"report_id": prepared["report_id"]})
    assert b"synthetic123" not in sent[0].content and b"abc123secret" not in sent[0].content
    # What the host showed is what was sent, including the bridge's own redaction marks.
    assert json.loads(sent[0].content) == prepared["report"]


def test_without_logs_the_conversation_never_leaves_and_signed_in_reports_carry_the_key(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, output, _session = bridge
    monkeypatch.setattr(delivery, "load_persisted_profile_keys", lambda: SIGNED_IN)
    prepared = _call(
        instance,
        output,
        "feedback.prepare",
        {"session_id": "feedback-session", "feedback": "Just a note"},
    )["result"]
    assert prepared["signed_in"] is True
    assert prepared["report"]["diagnostics"] is None
    assert "failing test" not in json.dumps(prepared)
    sent: list[httpx.Request] = []
    _server(monkeypatch, sent)
    result = _call(instance, output, "feedback.send", {"report_id": prepared["report_id"]})
    assert result["result"]["anonymous"] is False
    assert sent[0].headers["authorization"] == "Bearer " + SIGNED_IN["alysis"]


def test_a_report_without_a_chat_needs_a_workspace(bridge, workspace: Path) -> None:
    instance, output, _session = bridge
    missing = _call(instance, output, "feedback.prepare", {"feedback": "From the menu"})
    assert missing["error"]["code"] == "missing_field"
    prepared = _call(
        instance,
        output,
        "feedback.prepare",
        {
            "workspace": os.fspath(workspace),
            "feedback": "From the menu",
            "include_diagnostics": True,
        },
    )["result"]
    diagnostics = prepared["report"]["diagnostics"]
    assert diagnostics["messages"] == [] and diagnostics["total_messages"] == 0


def test_a_chat_that_is_not_open_is_read_from_its_retained_log(
    bridge, workspace: Path, tmp_path: Path
) -> None:
    instance, output, _session = bridge
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    events = [
        {"type": "session_start", "payload": {"mode": "auto"}},
        {"type": "user_message", "payload": {"content": "Summarise the CSV files"}},
        {"type": "assistant_message", "payload": {"content": "The second file failed to load."}},
    ]
    (sessions / "closed-chat.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    base = {
        "session_id": "closed-chat",
        "workspace": os.fspath(workspace),
        "session_dir": os.fspath(sessions),
        "feedback": "The report was empty",
    }
    with_logs = _call(instance, output, "feedback.prepare", {**base, "include_diagnostics": True})[
        "result"
    ]
    assert [m["content"] for m in with_logs["report"]["diagnostics"]["messages"]] == [
        "Summarise the CSV files",
        "The second file failed to load.",
    ]
    without_logs = _call(instance, output, "feedback.prepare", base)["result"]
    assert without_logs["report"]["diagnostics"] is None
    missing = _call(
        instance,
        output,
        "feedback.prepare",
        {**base, "session_id": "no-such-chat", "include_diagnostics": True},
    )
    assert missing["error"]["code"] == "session_not_found"
    assert not list(sessions.glob("*.meta.json")), "reading a chat never writes to it"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"session_id": "feedback-session", "feedback": "  "}, "feedback_invalid"),
        ({"session_id": "feedback-session", "feedback": "x" * 8001}, "feedback_invalid"),
        ({"session_id": "feedback-session"}, "invalid_field"),
        (
            {"session_id": "feedback-session", "feedback": "x", "include_diagnostics": "yes"},
            "invalid_field",
        ),
        ({"session_id": "no-such-session", "feedback": "x"}, "missing_field"),
    ],
)
def test_prepare_rejects_bad_input_with_the_engine_message(bridge, params, code) -> None:
    instance, output, _session = bridge
    error = _call(instance, output, "feedback.prepare", params)["error"]
    assert error["code"] == code
    if code == "feedback_invalid":
        assert "description" in error["message"] or "8,000" in error["message"]


def test_failed_delivery_is_safe_to_retry_with_the_same_bytes(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, output, _session = bridge
    prepared = _call(
        instance,
        output,
        "feedback.prepare",
        {"session_id": "feedback-session", "feedback": "Retry me"},
    )["result"]
    sent: list[httpx.Request] = []
    _server(monkeypatch, sent, status=503)
    failed = _call(instance, output, "feedback.send", {"report_id": prepared["report_id"]})
    assert failed["error"]["code"] == "feedback_delivery_failed"
    assert "secret" not in failed["error"]["message"]
    _server(monkeypatch, sent)
    retried = _call(instance, output, "feedback.send", {"report_id": prepared["report_id"]})
    assert retried["result"]["report_id"] == prepared["report_id"]
    assert sent[0].content == sent[1].content


def test_unknown_and_evicted_reports_must_be_prepared_again(bridge) -> None:
    instance, output, _session = bridge
    unknown = _call(instance, output, "feedback.send", {"report_id": "not-prepared"})
    assert unknown["error"]["code"] == "feedback_not_prepared"
    ids = [
        _call(
            instance,
            output,
            "feedback.prepare",
            {"session_id": "feedback-session", "feedback": f"report {index}"},
        )["result"]["report_id"]
        for index in range(MAX_PREPARED_FEEDBACK + 1)
    ]
    evicted = _call(instance, output, "feedback.send", {"report_id": ids[0]})
    assert evicted["error"]["code"] == "feedback_not_prepared"


@pytest.mark.parametrize(
    ("status", "keys", "code"),
    [
        (429, {}, "feedback_rate_limited"),
        (401, {}, "feedback_sign_in_required"),
        (401, SIGNED_IN, "feedback_sign_in_expired"),
        (409, {}, "feedback_conflict"),
        (413, {}, "feedback_too_large"),
        (404, {}, "feedback_unavailable"),
        (500, {}, "feedback_delivery_failed"),
    ],
)
def test_a_refusal_names_its_reason_for_the_host(
    bridge, monkeypatch: pytest.MonkeyPatch, status: int, keys: dict[str, str], code: str
) -> None:
    instance, output, _session = bridge
    monkeypatch.setattr(delivery, "load_persisted_profile_keys", lambda: keys)
    prepared = _call(
        instance,
        output,
        "feedback.prepare",
        {"session_id": "feedback-session", "feedback": "Refused"},
    )["result"]
    sent: list[httpx.Request] = []
    _server(monkeypatch, sent, status=status)
    error = _call(instance, output, "feedback.send", {"report_id": prepared["report_id"]})["error"]
    assert error["code"] == code
    assert "server-detail" not in error["message"]
