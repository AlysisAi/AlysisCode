"""Opt-in provider reasoning events on the IDE bridge (`reasoning_started/delta/ended`)."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.turn.core import _surface_accepts_reasoning_trace
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.event_stream import EventContext, ProtocolEventSurface
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.stdio_bridge import StdioBridge
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.llm.protocols import ReasoningTraceCapability
from alysis_code.llm.types import ReasoningOutputKind
from alysis_code.surface.events import EVENT_REGISTRY, event_from_dict

REASONING_EVENTS = ("reasoning_started", "reasoning_delta", "reasoning_ended")


def _surface(*, opt_in: bool, level: str = "compact") -> tuple[ProtocolEventSurface, list[dict]]:
    emitted: list[dict[str, Any]] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1", job_id="job-1"),
        emit=emitted.append,
        reasoning_trace_events=opt_in,
    )
    surface.set_trace_level(level)
    return surface, emitted


def _reasoning(emitted: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    return [(e["type"], e["payload"]) for e in emitted if e["type"] in REASONING_EVENTS]


# --- surface -----------------------------------------------------------------------------------


def test_reasoning_is_off_without_host_opt_in() -> None:
    surface, emitted = _surface(opt_in=False)

    assert surface.reasoning_trace_events is False
    assert surface.reasoning_trace_enabled is False
    # The core attaches no sink, so Responses/Anthropic are not asked for summaries.
    assert _surface_accepts_reasoning_trace(surface) is False
    surface.on_reasoning_start("reasoning-1-1")
    surface.on_reasoning_token("hidden")
    surface.on_reasoning_end("reasoning-1-1")
    assert emitted == []


@pytest.mark.parametrize(
    ("level", "expected"),
    [("off", False), ("compact", True), ("full", True)],
)
def test_opted_in_surface_follows_the_trace_level(level: str, expected: bool) -> None:
    surface, _ = _surface(opt_in=True, level=level)

    assert surface.reasoning_trace_events is True
    assert surface.reasoning_trace_enabled is expected
    assert _surface_accepts_reasoning_trace(surface) is expected


def test_reasoning_block_streams_start_deltas_end_in_order() -> None:
    surface, emitted = _surface(opt_in=True)

    surface.on_reasoning_start("reasoning-1-1")
    surface.on_reasoning_token("Compare the options. ")
    surface.on_reasoning_token("Pick the smallest change.")
    surface.on_reasoning_end("reasoning-1-1")

    assert _reasoning(emitted) == [
        ("reasoning_started", {"block_id": "reasoning-1-1"}),
        ("reasoning_delta", {"block_id": "reasoning-1-1", "text": "Compare the options. "}),
        ("reasoning_delta", {"block_id": "reasoning-1-1", "text": "Pick the smallest change."}),
        ("reasoning_ended", {"block_id": "reasoning-1-1"}),
    ]
    sequences = [event["sequence"] for event in emitted]
    assert sequences == sorted(sequences)
    assert {event["job_id"] for event in emitted} == {"job-1"}
    # Every payload round-trips through the registered event types.
    for event in emitted:
        event_from_dict({"type": event["type"], **event["payload"]})


def test_reasoning_text_is_redacted_like_other_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "reasoning-secret-value-123")
    surface, emitted = _surface(opt_in=True)

    surface.on_reasoning_start("b1")
    surface.on_reasoning_token("use reasoning-secret-value-123 and Bearer abcdefghijklmnopqrstuvwx")
    surface.on_reasoning_end("b1")

    rendered = json.dumps(emitted)
    assert "reasoning-secret-value-123" not in rendered
    assert "abcdefghijklmnopqrstuvwx" not in rendered
    assert "<redacted>" in rendered


def test_stale_end_is_ignored_and_a_new_start_closes_the_open_block() -> None:
    surface, emitted = _surface(opt_in=True)

    surface.on_reasoning_start("first")
    surface.on_reasoning_token("one")
    surface.on_reasoning_end("stale")  # not the open block
    surface.on_reasoning_start("second")  # closes "first" before opening "second"
    surface.on_reasoning_token("two")
    surface.on_reasoning_end("second")
    surface.on_reasoning_end("second")  # already closed

    assert [(kind, payload["block_id"]) for kind, payload in _reasoning(emitted)] == [
        ("reasoning_started", "first"),
        ("reasoning_delta", "first"),
        ("reasoning_ended", "first"),
        ("reasoning_started", "second"),
        ("reasoning_delta", "second"),
        ("reasoning_ended", "second"),
    ]


def test_token_without_start_opens_a_block_and_empty_tokens_are_dropped() -> None:
    surface, emitted = _surface(opt_in=True)

    surface.on_reasoning_token("")
    surface.on_reasoning_token("legacy adapter text")
    surface.on_reasoning_end("")

    kinds = [kind for kind, _ in _reasoning(emitted)]
    assert kinds == ["reasoning_started", "reasoning_delta", "reasoning_ended"]
    block_ids = {payload["block_id"] for _, payload in _reasoning(emitted)}
    assert len(block_ids) == 1
    assert next(iter(block_ids)).startswith("reasoning-")


def test_trace_off_mid_block_stops_text_but_still_closes_the_block() -> None:
    surface, emitted = _surface(opt_in=True)

    surface.on_reasoning_start("b1")
    surface.on_reasoning_token("visible")
    surface.set_trace_level("off")
    surface.on_reasoning_token("not sent")
    surface.on_reasoning_end("b1")

    assert _reasoning(emitted) == [
        ("reasoning_started", {"block_id": "b1"}),
        ("reasoning_delta", {"block_id": "b1", "text": "visible"}),
        ("reasoning_ended", {"block_id": "b1"}),
    ]


# --- capabilities ------------------------------------------------------------------------------


def test_capabilities_advertise_the_opt_in_reasoning_events() -> None:
    payload = capabilities_payload()

    assert set(REASONING_EVENTS) <= set(payload["events"])
    assert set(REASONING_EVENTS) <= set(EVENT_REGISTRY)
    assert payload["features"]["reasoning_trace"] == {
        "version": 1,
        "opt_in": "session.create.reasoning_trace",
        "events": list(REASONING_EVENTS),
        "gated_by_trace_level": True,
        "disabled_at_trace_level": "off",
        "sensitive_sessions": False,
    }


# --- session.create opt-in ---------------------------------------------------------------------


def test_opt_in_helper_never_enables_computer_use_sessions() -> None:
    helper = stdio_bridge._session_reasoning_trace_events  # noqa: SLF001

    assert helper({}, sensitive_media=False, request_id="r") is False
    assert helper({"reasoning_trace": False}, sensitive_media=False, request_id="r") is False
    assert helper({"reasoning_trace": True}, sensitive_media=False, request_id="r") is True
    assert helper({"reasoning_trace": True}, sensitive_media=True, request_id="r") is False
    with pytest.raises(ProtocolError) as error:
        helper({"reasoning_trace": "yes"}, sensitive_media=False, request_id="r")
    assert error.value.code == "invalid_field"


def _request(method: str, params: dict[str, Any], request_id: str) -> str:
    return json.dumps(
        {"protocol_version": "1", "id": request_id, "method": method, "params": params}
    )


def _lines(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _response(out: io.StringIO, request_id: str) -> dict[str, Any]:
    return next(line for line in _lines(out) if line.get("id") == request_id)


def _wait_for(out: io.StringIO, predicate: Any, *, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in _lines(out):
            if predicate(line):
                return line
        time.sleep(0.01)
    raise AssertionError("timed out waiting for protocol line")


def _bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_turn: Any) -> tuple:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path.parent / f"{tmp_path.name}-data"))
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", subagents_enabled=False),
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir(exist_ok=True)

    class FakeStore:
        session_artifact_root = artifact_root

    class FakeSession:
        store = FakeStore()

        def __init__(self, surface: Any) -> None:
            self.surface = surface

        def run_turn(self, message: str) -> int:
            return int(run_turn(self.surface, message) or 0)

        def close(self) -> None:
            pass

    out = io.StringIO()
    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kw: FakeSession(kw["surface"]))
    return out, bridge


def _core_like_turn(surface: Any, _message: str) -> int:
    """What the agent core's reasoning sink does, gated exactly as the core gates it."""

    if _surface_accepts_reasoning_trace(surface):
        surface.on_reasoning_start("reasoning-1-1")
        surface.on_reasoning_token("Read the file first.")
        surface.on_reasoning_end("reasoning-1-1")
    surface.emit_message_end("Done.")
    return 0


@pytest.mark.parametrize("opt_in", [True, False])
def test_bridge_session_streams_reasoning_only_when_the_host_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opt_in: bool
) -> None:
    out, bridge = _bridge(tmp_path, monkeypatch, _core_like_turn)
    params: dict[str, Any] = {"workspace": os.fspath(tmp_path), "mode": "review"}
    if opt_in:
        params["reasoning_trace"] = True
    bridge.process_line(_request("session.create", params, "create") + "\n")
    created = _response(out, "create")
    assert created["ok"] is True, created
    session_id = created["result"]["session_id"]
    assert created["result"]["reasoning_trace"] is opt_in

    bridge.process_line(
        _request("session.trace.status", {"session_id": session_id}, "status") + "\n"
    )
    assert _response(out, "status")["result"]["reasoning_events"] is opt_in

    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "hi"}, "send") + "\n"
    )
    _wait_for(out, lambda line: line.get("type") == "message_end")
    events = [line for line in _lines(out) if line.get("type") in REASONING_EVENTS]
    if not opt_in:
        assert events == []
        return
    assert [event["type"] for event in events] == list(REASONING_EVENTS)
    assert events[1]["payload"] == {"block_id": "reasoning-1-1", "text": "Read the file first."}
    assert all(event["session_id"] == session_id for event in events)
    assert all(event["job_id"] for event in events)


def test_trace_level_off_stops_reasoning_for_an_opted_in_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, bridge = _bridge(tmp_path, monkeypatch, _core_like_turn)
    bridge.process_line(
        _request(
            "session.create",
            {"workspace": os.fspath(tmp_path), "mode": "review", "reasoning_trace": True},
            "create",
        )
        + "\n"
    )
    session_id = _response(out, "create")["result"]["session_id"]
    bridge.process_line(
        _request("session.trace.setLevel", {"session_id": session_id, "level": "off"}, "off") + "\n"
    )
    assert _response(out, "off")["result"]["level"] == "off"

    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "hi"}, "send") + "\n"
    )
    _wait_for(out, lambda line: line.get("type") == "message_end")
    assert [line for line in _lines(out) if line.get("type") in REASONING_EVENTS] == []


# --- the real agent core -----------------------------------------------------------------------


class _ReasoningClient:
    """Main-model fake that streams provider reasoning, then the reply."""

    model = "test-model"
    temperature = 0.2

    def __init__(self, capability: ReasoningTraceCapability) -> None:
        self.reasoning_trace_capability = capability
        self.summary_callback_supplied: bool | None = None

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        on_reasoning_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
        **_kwargs: Any,
    ) -> LLMResponse:
        _ = messages, tools, temperature
        self.summary_callback_supplied = callable(on_reasoning_delta)
        if on_reasoning_delta is not None:
            on_reasoning_delta("Check ")
            on_reasoning_delta("the request.")
        if stream and on_text_delta is not None:
            on_text_delta("Hello.")
        return LLMResponse(content="Hello.", tool_calls=[], raw={}, usage=None)


@pytest.mark.parametrize(
    "capability",
    [
        ReasoningTraceCapability(
            adapter="summary_route",
            output_kind=ReasoningOutputKind.SUMMARY,
            supports_streaming=True,
            supports_buffered=True,
            requestable=True,
        ),
        ReasoningTraceCapability(
            adapter="provider_reasoning_route",
            output_kind=ReasoningOutputKind.PROVIDER_REASONING,
            supports_streaming=True,
            supports_buffered=True,
        ),
    ],
    ids=["summary", "provider-reasoning"],
)
@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("opt_in", [True, False])
def test_agent_core_turn_reaches_the_wire_for_every_reasoning_kind(
    tmp_path: Path, capability: ReasoningTraceCapability, stream: bool, opt_in: bool
) -> None:
    surface, emitted = _surface(opt_in=opt_in)
    session = create_session(
        cfg=AppConfig(model="test-model", stream=stream),
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
        surface=surface,
    )
    client = _ReasoningClient(capability)
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Hi") == 0
    finally:
        session.close()

    assert client.summary_callback_supplied is opt_in
    reasoning = _reasoning(emitted)
    if not opt_in:
        assert reasoning == []
        return
    kinds = [kind for kind, _ in reasoning]
    assert kinds == ["reasoning_started", "reasoning_delta", "reasoning_delta", "reasoning_ended"]
    assert (
        "".join(p["text"] for k, p in reasoning if k == "reasoning_delta") == "Check the request."
    )
    assert len({payload["block_id"] for _, payload in reasoning}) == 1
    types = [event["type"] for event in emitted]
    # The block closes before the reply is final.
    assert types.index("reasoning_ended") < types.index("message_end")
