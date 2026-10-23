"""IDE bridge persona surface: session.personas.list / session.persona.set.

Same discipline as test_ide_stdio_bridge.py: a fake agent session behind the
real bridge, real protocol dispatch, and the real chat-loop persona primitive
(`_apply_chat_persona`) with only the tool-surface rebuild faked out, exactly
like test_persona_modes.py exercises it.
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import alysis_code.personas as personas_mod
from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.stdio_bridge import StdioBridge


def _request(method: str, params: dict[str, Any] | None = None, request_id: str = "req") -> str:
    return json.dumps(
        {
            "protocol_version": "1",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
        sort_keys=True,
    )


def _json_lines(buffer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def _response_by_id(out: io.StringIO, request_id: str) -> dict[str, Any]:
    return [line for line in _json_lines(out) if line.get("id") == request_id][0]


def _send_bridge_request(
    bridge: StdioBridge,
    out: io.StringIO,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    rid = request_id or method
    bridge.process_line(_request(method, params or {}, request_id=rid) + "\n")
    return _response_by_id(out, rid)


def _events_of_type(out: io.StringIO, event_type: str) -> list[dict[str, Any]]:
    return [line for line in _json_lines(out) if line.get("type") == event_type]


def _wait_for_line(out: io.StringIO, predicate: Any, *, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in _json_lines(out):
            if predicate(line):
                return line
        time.sleep(0.01)
    raise AssertionError("timed out waiting for protocol line")


def _write_custom_persona(root: Path) -> None:
    directory = root / ".alysis_personas"
    directory.mkdir(exist_ok=True)
    (directory / "reviewer.md").write_text(
        "---\n"
        "name: reviewer\n"
        "description: Reviews changes before merge\n"
        "exec_mode: readonly\n"
        "model_role: review\n"
        "---\n"
        "Review the diff before proposing edits.\n",
        encoding="utf-8",
    )


def _create_persona_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "review",
    persona_modes_enabled_cfg: bool = True,
    run_turn: Any = None,
    default_persona: str = "code",
    log_path: Path | None = None,
) -> tuple[io.StringIO, StdioBridge, str, dict[str, Any]]:
    """A bridge session whose fake agent session carries the chat persona state.

    The real ``_apply_chat_persona`` runs; only the tool-surface rebuild
    (``_apply_chat_effective_mode``) is faked, mirroring test_persona_modes.py.
    """
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    monkeypatch.setattr(personas_mod, "canonical_user_config_dir", lambda: tmp_path / "no-user-dir")
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir(exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    def _load_config() -> AppConfig:
        cfg = AppConfig(model="test-model", subagents_enabled=False)
        cfg.persona_modes_enabled = persona_modes_enabled_cfg
        cfg.default_persona = default_persona
        return cfg

    monkeypatch.setattr(stdio_bridge, "load_config", _load_config)

    applied_modes: list[str] = []

    def _fake_apply_mode(*, session: Any, next_mode: str, persist_default_mode: bool) -> None:
        assert persist_default_mode is False
        session.mode = next_mode
        applied_modes.append(next_mode)

    monkeypatch.setattr(chat_loop, "_apply_chat_effective_mode", _fake_apply_mode)

    class FakeStore:
        session_artifact_root = artifact_root

        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []
            # With log_path the store keeps a session log on disk, so a later session
            # (a later turn of the same chat) can read the persona back from it.
            self.path = log_path
            self.sessions_dir = log_path.parent if log_path is not None else None

        def append(self, name: str, payload: dict[str, Any]) -> None:
            self.events.append((name, payload))
            if self.path is not None:
                with Path(self.path).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"type": name, "payload": payload}) + "\n")

    created_sessions: list[Any] = []

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.mode = kwargs["mode"]
            self.surface = kwargs["surface"]
            self.store = FakeStore()
            self.messages: list[dict[str, Any]] = []
            # Like create_session: the configured default names the persona, but
            # applying it (mode, write scope, model role) is the caller's job.
            self.persona = default_persona if persona_modes_enabled_cfg else "code"
            # What session.resume's history replay touches on a real session.
            self.pinned_prefix_len = 0
            self.conversation_compactor = SimpleNamespace(
                state=SimpleNamespace(pinned_prefix_len=0)
            )
            self.persona_restore_mode: str | None = None
            self.persona_restore_write_globs: list[str] | None = None
            self.allow_write_globs: list[str] | None = None
            self.persona_allow_write_globs: list[str] | None = None

        def run_turn(self, message: str) -> int:
            if run_turn is None:
                return 0
            return int(run_turn(self.surface, message) or 0)

        def close(self) -> None:
            return None

    def _create_session(**kwargs: Any) -> FakeSession:
        session = FakeSession(**kwargs)
        created_sessions.append(session)
        return session

    bridge = StdioBridge(stdout=out, create_session_fn=_create_session)
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": str(tmp_path), "mode": mode, "model": "test-model"},
        request_id="create",
    )
    assert create["ok"] is True
    session_id = create["result"]["session_id"]
    context = {
        "applied_modes": applied_modes,
        "created_sessions": created_sessions,
    }
    return out, bridge, session_id, context


def test_capabilities_advertise_persona_methods_and_event() -> None:
    payload = capabilities_payload()

    assert "session.personas.list" in payload["methods"]
    assert "session.persona.set" in payload["methods"]
    assert "persona_changed" in payload["events"]


def test_personas_list_returns_builtins_then_customs_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_custom_persona(tmp_path)
    out, bridge, session_id, _context = _create_persona_session(tmp_path, monkeypatch)

    listed = _send_bridge_request(bridge, out, "session.personas.list", {"session_id": session_id})

    assert listed["ok"] is True
    result = listed["result"]
    assert result["enabled"] is True
    assert result["active"] == "code"
    assert result["active_source"] == "config"
    names = [entry["name"] for entry in result["personas"]]
    assert names == ["code", "architect", "ask", "debug", "reviewer"]
    by_name = {entry["name"]: entry for entry in result["personas"]}
    assert by_name["architect"] == {
        "name": "architect",
        "description": "Planning and design; may write markdown plan documents only.",
        "default_exec_mode": "review",
        "model_role": "planner",
        "source_scope": "builtin",
        "allow_write_globs": ["*.md", "**/*.md"],
    }
    reviewer = by_name["reviewer"]
    assert reviewer["source_scope"] == "custom"
    assert reviewer["default_exec_mode"] == "readonly"
    assert reviewer["model_role"] == "review"
    assert reviewer["description"] == "Reviews changes before merge"
    # Prompt bodies never cross the IDE wire.
    for entry in result["personas"]:
        assert "overlay_prompt" not in entry
        assert "prompt_trust" not in entry


def test_personas_list_reports_disabled_kill_switch_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _context = _create_persona_session(
        tmp_path, monkeypatch, persona_modes_enabled_cfg=False
    )

    listed = _send_bridge_request(bridge, out, "session.personas.list", {"session_id": session_id})

    assert listed["ok"] is True
    assert listed["result"]["enabled"] is False
    assert listed["result"]["active"] == "code"
    assert listed["result"]["personas"] == []


def test_personas_list_unknown_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, _session_id, _context = _create_persona_session(tmp_path, monkeypatch)

    response = _send_bridge_request(
        bridge,
        out,
        "session.personas.list",
        {"session_id": "sess-does-not-exist"},
        request_id="list-missing",
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "session_not_found"


def test_persona_set_applies_clamp_and_emits_persona_changed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, context = _create_persona_session(tmp_path, monkeypatch)

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "architect"},
        request_id="set-architect",
    )

    assert response["ok"] is True
    assert response["result"]["persona"] == "architect"
    assert response["result"]["effective_mode"] == "review"
    assert response["result"]["model_role"] == "planner"
    assert response["result"]["changed"] is True

    events = _events_of_type(out, "persona_changed")
    assert len(events) == 1
    assert events[0]["payload"] == {
        "persona": "architect",
        "effective_mode": "review",
        "source": "user",
    }
    assert events[0]["session_id"] == session_id

    agent_session = context["created_sessions"][0]
    assert agent_session.persona == "architect"
    assert agent_session.persona_allow_write_globs == ["*.md", "**/*.md"]
    assert agent_session.persona_restore_mode == "review"

    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    assert status["result"]["persona"] == "architect"
    assert status["result"]["persona_source"] == "user"
    assert status["result"]["mode"] == "review"


def test_persona_set_same_persona_reports_changed_false_without_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _context = _create_persona_session(tmp_path, monkeypatch)

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "code"},
        request_id="set-same",
    )

    assert response["ok"] is True
    assert response["result"]["persona"] == "code"
    assert response["result"]["changed"] is False
    assert response["result"]["effective_mode"] == "review"
    assert _events_of_type(out, "persona_changed") == []


def test_persona_set_rejects_unknown_persona_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _context = _create_persona_session(tmp_path, monkeypatch)

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "wizard"},
        request_id="set-invalid",
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_persona"
    assert "code" in response["error"]["message"]
    assert "architect" in response["error"]["message"]
    assert _events_of_type(out, "persona_changed") == []


def test_persona_set_rejected_when_persona_modes_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _context = _create_persona_session(
        tmp_path, monkeypatch, persona_modes_enabled_cfg=False
    )

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "architect"},
        request_id="set-disabled",
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "persona_modes_disabled"
    assert response["error"]["message"] == "Persona modes are disabled."
    assert _events_of_type(out, "persona_changed") == []


def test_persona_set_rejected_while_session_job_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def _blocking_turn(_surface: Any, _message: str) -> int:
        entered.set()
        assert release.wait(timeout=5.0)
        return 0

    out, bridge, session_id, _context = _create_persona_session(
        tmp_path, monkeypatch, run_turn=_blocking_turn
    )
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "go"}, request_id="chat") + "\n"
    )
    assert entered.wait(timeout=5.0)
    try:
        response = _send_bridge_request(
            bridge,
            out,
            "session.persona.set",
            {"session_id": session_id, "persona": "architect"},
            request_id="set-busy",
        )
    finally:
        release.set()

    assert response["ok"] is False
    assert response["error"]["code"] == "persona_change_busy"
    assert response["error"]["message"] == "Cannot change persona while a task is running."
    assert _events_of_type(out, "persona_changed") == []

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and str(line.get("payload", {}).get("message", "")).startswith("job_completed ")
        ),
    )
    retried = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "architect"},
        request_id="set-after-job",
    )
    assert retried["ok"] is True
    assert retried["result"]["changed"] is True


def test_persona_set_readonly_base_mode_clamps_architect_to_readonly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, context = _create_persona_session(
        tmp_path, monkeypatch, mode="readonly"
    )

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "architect"},
        request_id="set-clamped",
    )

    assert response["ok"] is True
    assert response["result"]["persona"] == "architect"
    # The clamp lowers, never raises: architect's review default cannot lift a
    # readonly session out of readonly.
    assert response["result"]["effective_mode"] == "readonly"
    events = _events_of_type(out, "persona_changed")
    assert len(events) == 1
    assert events[0]["payload"]["effective_mode"] == "readonly"
    agent_session = context["created_sessions"][0]
    assert agent_session.mode == "readonly"
    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    assert status["result"]["mode"] == "readonly"
    assert status["result"]["persona"] == "architect"


def test_persona_set_supports_custom_personas_from_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_custom_persona(tmp_path)
    out, bridge, session_id, context = _create_persona_session(tmp_path, monkeypatch)

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "reviewer"},
        request_id="set-custom",
    )

    assert response["ok"] is True
    assert response["result"]["persona"] == "reviewer"
    assert response["result"]["effective_mode"] == "readonly"
    assert response["result"]["model_role"] == "review"
    assert response["result"]["changed"] is True
    events = _events_of_type(out, "persona_changed")
    assert len(events) == 1
    assert events[0]["payload"] == {
        "persona": "reviewer",
        "effective_mode": "readonly",
        "source": "user",
    }
    agent_session = context["created_sessions"][0]
    assert agent_session.persona == "reviewer"
    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    assert status["result"]["persona"] == "reviewer"
    assert status["result"]["persona_source"] == "user"


def test_session_status_reports_persona_defaults_before_any_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _context = _create_persona_session(tmp_path, monkeypatch)

    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})

    assert status["ok"] is True
    assert status["result"]["persona"] == "code"
    assert status["result"]["persona_source"] == "config"


def test_session_create_applies_the_default_persona_like_chat_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, context = _create_persona_session(
        tmp_path, monkeypatch, mode="review", default_persona="ask"
    )

    create = _response_by_id(out, "create")
    assert create["result"]["mode"] == "readonly", "the persona's narrower mode is in effect"
    agent_session = context["created_sessions"][0]
    assert agent_session.persona == "ask"
    assert agent_session.persona_restore_mode == "review"
    assert context["applied_modes"] == ["readonly"]
    events = _events_of_type(out, "persona_changed")
    assert [event["payload"] for event in events] == [
        {"persona": "ask", "effective_mode": "readonly", "source": "config"}
    ]

    listed = _send_bridge_request(bridge, out, "session.personas.list", {"session_id": session_id})
    assert listed["result"]["active"] == "ask"
    assert listed["result"]["active_source"] == "config"
    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    assert status["result"]["mode"] == "readonly"

    # Choosing the active persona again changes nothing, and now nothing needs to change.
    again = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "ask"},
        request_id="set-again",
    )
    assert again["result"]["changed"] is False
    assert again["result"]["effective_mode"] == "readonly"


def test_session_create_with_the_code_default_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _bridge, _session_id, context = _create_persona_session(
        tmp_path, monkeypatch, mode="review", log_path=tmp_path / "chat.jsonl"
    )

    assert _response_by_id(out, "create")["result"]["mode"] == "review"
    assert context["applied_modes"] == []
    assert _events_of_type(out, "persona_changed") == []
    store_events = context["created_sessions"][0].store.events
    assert [name for name, _payload in store_events if name == "persona_switch_applied"] == []


def test_a_session_that_continues_a_chat_log_restores_the_chat_persona(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "chat.jsonl"
    out, bridge, session_id, _context = _create_persona_session(
        tmp_path, monkeypatch, default_persona="architect", log_path=log_path
    )
    switched = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "ask"},
        request_id="set-ask",
    )
    assert switched["result"]["changed"] is True

    # The chat's next turn runs in a new bridge process that continues the same log.
    out2, bridge2, session_id2, context2 = _create_persona_session(
        tmp_path, monkeypatch, default_persona="architect", log_path=log_path
    )

    assert _response_by_id(out2, "create")["result"]["mode"] == "readonly"
    assert context2["created_sessions"][0].persona == "ask"
    events = _events_of_type(out2, "persona_changed")
    assert [event["payload"] for event in events] == [
        {"persona": "ask", "effective_mode": "readonly", "source": "resume"}
    ], "the chat's own persona, not the default applied over it"
    listed = _send_bridge_request(
        bridge2, out2, "session.personas.list", {"session_id": session_id2}
    )
    assert listed["result"]["active"] == "ask"
    assert listed["result"]["active_source"] == "resume"


def test_resuming_another_session_restores_that_session_persona(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        StdioBridge,
        "_change_ledger_for_session",
        lambda self, session: SimpleNamespace(adopt_session=lambda *args: None),
    )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    out, bridge, session_id, context = _create_persona_session(
        tmp_path, monkeypatch, log_path=sessions / "live.jsonl"
    )
    (sessions / "retained.jsonl").write_text(
        json.dumps({"type": "user_message", "payload": {"content": "explain the parser"}})
        + "\n"
        + json.dumps(
            {
                "type": "persona_switch_applied",
                "payload": {"persona": "ask", "effective_mode": "readonly", "source": "user"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    resume = _send_bridge_request(
        bridge,
        out,
        "session.resume",
        {"session_id": session_id, "target_session_id": "retained"},
        request_id="resume",
    )

    assert resume["ok"] is True
    assert context["created_sessions"][0].persona == "ask"
    events = _events_of_type(out, "persona_changed")
    assert [event["payload"] for event in events] == [
        {"persona": "ask", "effective_mode": "readonly", "source": "resume"}
    ]
    listed = _send_bridge_request(bridge, out, "session.personas.list", {"session_id": session_id})
    assert listed["result"]["active"] == "ask"
    assert listed["result"]["active_source"] == "resume"
    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    assert status["result"]["mode"] == "readonly"


def test_load_resumed_persona_reads_a_given_log(tmp_path: Path) -> None:
    other = tmp_path / "other.jsonl"
    other.write_text(
        json.dumps({"type": "persona_switch_applied", "payload": {"persona": "architect"}})
        + "\n"
        + json.dumps({"type": "persona_switch_applied", "payload": {"persona": "debug"}})
        + "\n",
        encoding="utf-8",
    )

    class _Session:
        cfg = AppConfig(model="test-model")
        store = None

    assert chat_loop._load_resumed_persona(_Session(), other) == "debug"
    assert chat_loop._load_resumed_persona(_Session()) is None
