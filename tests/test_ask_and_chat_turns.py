"""`/ask` one-turn read-only override and `/chat` minimal no-tools turns.

PR 4 of the router-removal migration: explicit, deterministic replacements for
the router's per-turn advisory-posture inference (`/ask`) and its small-talk
short-circuit (`/chat`). Both are user-selected, never inferred.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.agent.turn_path import CHAT_ONLY_SYSTEM_PROMPT
from alysis_code.agent_loop import create_session
from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl.chat import loop as chat_loop_mod
from alysis_code.cli_impl.chat.state import (
    _ChatExecutionRequest,
)
from alysis_code.config import AppConfig, save_config
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.session_store import read_session_events


def _dispatch(input_text: str, *, session: Any, tmp_path: Path) -> Any:
    return chat_loop_mod._handle_chat_command_impl(
        cli_mod,
        input_text=input_text,
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(),
    )


def _dispatch_output(input_text: str, *, session: Any, tmp_path: Path) -> tuple[Any, str]:
    output = io.StringIO()
    result = chat_loop_mod._handle_chat_command_impl(
        cli_mod,
        input_text=input_text,
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=output, force_terminal=False),
    )
    return result, output.getvalue()


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


# ---------------------------------------------------------------------------
# /ask producer
# ---------------------------------------------------------------------------


def test_permissions_is_idle_command_and_mode_is_unknown(tmp_path: Path) -> None:
    session = SimpleNamespace(mode="review", cfg=AppConfig())

    permissions_result, permissions_output = _dispatch_output(
        "/permissions review",
        session=session,
        tmp_path=tmp_path,
    )
    mode_result, mode_output = _dispatch_output(
        "/mode review",
        session=session,
        tmp_path=tmp_path,
    )

    assert permissions_result == "handled"
    assert "Permissions already active: safe (review)" in permissions_output
    assert "Unknown command" not in permissions_output
    assert mode_result == "handled"
    assert "Unknown command: /mode" in mode_output
    assert "Did you mean" not in mode_output


def test_ask_returns_one_turn_readonly_request(tmp_path: Path) -> None:
    session = SimpleNamespace(mode="review")
    result = _dispatch("/ask what does the parser module do?", session=session, tmp_path=tmp_path)

    assert isinstance(result, _ChatExecutionRequest)
    assert result.instruction == "what does the parser module do?"
    assert result.mode_override == "readonly"
    assert result.restore_mode_after == "review"
    assert result.chat_only is False


def test_ask_in_readonly_mode_keeps_explicit_turn_scope(tmp_path: Path) -> None:
    session = SimpleNamespace(mode="readonly")
    result = _dispatch("/ask anything risky here?", session=session, tmp_path=tmp_path)

    assert isinstance(result, _ChatExecutionRequest)
    assert result.instruction == "anything risky here?"
    assert result.mode_override == "readonly"
    assert result.restore_mode_after == "readonly"


def test_ask_without_text_prints_usage(tmp_path: Path) -> None:
    session = SimpleNamespace(mode="review")
    assert _dispatch("/ask", session=session, tmp_path=tmp_path) == "handled"


def test_deferred_model_cache_note_only_appears_on_actual_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def apply_cfg(*, session: Any, cfg: AppConfig) -> None:
        session.cfg = cfg
        session.client.model = cfg.model

    monkeypatch.setattr(chat_loop_mod, "_apply_config_menu_changes_to_session", apply_cfg)

    def run(command: str, current: str) -> str:
        output = io.StringIO()
        session = SimpleNamespace(
            cfg=AppConfig(model=current),
            client=SimpleNamespace(model=current, route_identity=None),
            _alysis_applying_deferred_command=True,
        )
        result = chat_loop_mod._handle_chat_command_impl(
            cli_mod,
            input_text=command,
            root=tmp_path,
            session=session,
            pending_images=[],
            console=Console(file=output, force_terminal=False),
        )
        assert result == "handled"
        return output.getvalue()

    changed = run("/model next-model", "old-model")
    unchanged = run("/model same-model", "same-model")

    assert "provider cache resets - first call re-reads the prefix." in changed
    assert "provider cache resets - first call re-reads the prefix." not in unchanged


# ---------------------------------------------------------------------------
# /chat producer
# ---------------------------------------------------------------------------


def test_chat_command_is_retired(tmp_path: Path) -> None:
    # /chat retired in favor of the Ask persona (/persona ask); the notice is a
    # handled command, never an execution request. The chat_only run_turn
    # plumbing below stays accepted-and-ignored for one release.
    session = SimpleNamespace(mode="review")
    assert _dispatch("/chat hello there", session=session, tmp_path=tmp_path) == "handled"
    assert _dispatch("/chat", session=session, tmp_path=tmp_path) == "handled"


# ---------------------------------------------------------------------------
# chat_only turns in run_turn
# ---------------------------------------------------------------------------


class _CapturingChatClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
        **_kwargs: Any,
    ) -> LLMResponse:
        _ = stream, on_text_delta, temperature
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return LLMResponse(content=self.reply, tool_calls=[], raw={})


def _session(tmp_path: Path) -> Any:
    cfg = AppConfig(model="test-model")
    return create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
    )


def test_chat_only_turn_uses_minimal_prompt_and_no_tools(tmp_path: Path, monkeypatch) -> None:
    session = _session(tmp_path)
    client = _CapturingChatClient("Hi! How can I help?")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("hello", chat_only=True)
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["tools"] is None
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][0]["content"] == CHAT_ONLY_SYSTEM_PROMPT
    assert call["messages"][-1] == {"role": "user", "content": "hello"}
    finals = _event_payloads(log_path, "final")
    assert len(finals) == 1
    assert finals[0]["content"] == "Hi! How can I help?"
    assert _event_payloads(log_path, "route_decision") == []
    operations = {
        str(payload.get("operation") or "") for payload in _event_payloads(log_path, "llm_usage")
    }
    assert "routing_llm" not in operations


def test_chat_only_turns_keep_conversation_continuity(tmp_path: Path, monkeypatch) -> None:
    session = _session(tmp_path)
    client = _CapturingChatClient("Nice to meet you, Alex.")
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("hi, I'm Alex", chat_only=True) == 0
        assert session.run_turn("what's my name?", chat_only=True) == 0
    finally:
        session.close()

    assert len(client.calls) == 2
    second_request_text = "\n".join(
        str(message.get("content") or "") for message in client.calls[1]["messages"]
    )
    assert "hi, I'm Alex" in second_request_text
    assert "Nice to meet you, Alex." in second_request_text


def test_one_turn_mode_override_applies_and_restores(tmp_path: Path) -> None:
    # The exact calls the chat loop makes when consuming an /ask request:
    # readonly applied before the turn, previous mode restored afterwards.
    session = _session(tmp_path)
    try:
        assert session.mode == "review"
        chat_loop_mod._apply_chat_effective_mode(
            session=session, next_mode="readonly", persist_default_mode=False
        )
        assert session.mode == "readonly"
        assert "fs_write" not in set(session.tools)
        chat_loop_mod._apply_chat_effective_mode(
            session=session, next_mode="review", persist_default_mode=False
        )
        assert session.mode == "review"
        assert "fs_write" in set(session.tools)
    finally:
        session.close()


def test_tui_pending_fullaccess_keeps_ask_readonly_and_restores_new_base(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from alysis_code.cli_impl import tui as tui_pkg

    config_dir = tmp_path / "cfg"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(config_dir))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(data_dir))
    save_config(AppConfig(model="test-model", default_mode="readonly"))
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(tui_pkg, "is_tui_enabled", lambda: True)

    observed_modes: list[str] = []
    rebuild_modes: list[str] = []
    runtime: dict[str, Any] = {}

    class _Surface:
        def emit_mode_changed(self, _mode: str) -> None:
            return None

    class _Store:
        session_id = "tui-ask"

        @staticmethod
        def append(_event_type: str, _payload: dict[str, Any]) -> None:
            return None

    class _Session:
        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"].model_copy(deep=True)
            self.root = kwargs["root"]
            self.mode = kwargs["mode"]
            self.pending_permissions_mode = None
            self.surface = kwargs["surface"]
            self.store = _Store()
            self.client = SimpleNamespace(model="test-model", temperature=0.2)
            self.stream = True
            self.subagents_enabled = False
            self.usage_summary = None
            self.messages: list[dict[str, Any]] = []
            self.allow_write_globs = None
            self.persona_allow_write_globs = None
            self.persona_restore_mode = None
            self.persona_restore_write_globs = None
            self.persona = "code"
            self.persona_registry = None

        def run_turn(self, _instruction: str, **run_kwargs: Any) -> int:
            assert not any(key.startswith("_alysis_") for key in run_kwargs)
            observed_modes.append(self.mode)
            return 0

        @staticmethod
        def close() -> None:
            return None

    def _create_session(**kwargs: Any) -> _Session:
        session = _Session(**kwargs)
        runtime["session"] = session
        return session

    def _rebuild(*, session: Any, mode: str) -> None:
        _ = session
        rebuild_modes.append(mode)

    monkeypatch.setattr(cli_mod, "create_session", _create_session)
    monkeypatch.setattr(cli_mod, "_rebuild_session_tools_for_mode", _rebuild)
    monkeypatch.setattr(chat_loop_mod, "_rebuild_session_tools_for_mode", _rebuild, raising=False)
    monkeypatch.setattr(
        cli_mod,
        "refresh_session_environment_context_message",
        lambda _session: None,
    )
    monkeypatch.setattr(
        chat_loop_mod,
        "refresh_session_environment_context_message",
        lambda _session: None,
        raising=False,
    )

    def _run_tui(_state: Any, **kwargs: Any) -> tuple[str, list[Any]]:
        session = kwargs["session_builder"](_Surface())
        command_runner = kwargs["command_runner"]
        before_turn = kwargs["before_turn"]

        action, _output, _instruction, _run_kwargs = command_runner(
            session,
            "/permissions fullaccess",
            100,
        )
        assert action == "handled"
        assert session.mode == "readonly"
        assert session.pending_permissions_mode == "fullaccess"

        action, _output, instruction, run_kwargs = command_runner(
            session,
            "/ask inspect this safely",
            100,
        )
        assert action == "run"
        assert instruction == "inspect this safely"
        assert run_kwargs is not None
        cleanup = before_turn(session, run_kwargs)
        assert session.mode == "readonly"
        assert session.cfg.default_mode == "fullaccess"
        try:
            session.run_turn(instruction, **run_kwargs)
        finally:
            assert cleanup is not None
            cleanup()
        assert session.mode == "fullaccess"

        # A newly staged base selection becomes authoritative at the next real
        # turn; `/ask` still overlays readonly on top of it and restores the
        # newly activated base afterwards.
        action, _output, _instruction, _run_kwargs = command_runner(
            session,
            "/permissions auto",
            100,
        )
        assert action == "handled"
        assert session.mode == "fullaccess"
        assert session.pending_permissions_mode == "auto"

        action, _output, instruction, run_kwargs = command_runner(
            session,
            "/ask inspect the plan safely",
            100,
        )
        assert action == "run"
        assert run_kwargs is not None
        cleanup = before_turn(session, run_kwargs)
        assert session.mode == "readonly"
        assert session.pending_permissions_mode is None
        try:
            session.run_turn(instruction, **run_kwargs)
        finally:
            assert cleanup is not None
            cleanup()
        assert session.mode == "auto"
        return "/exit", []

    monkeypatch.setattr(tui_pkg, "run_tui", _run_tui)

    result = CliRunner().invoke(
        alysis_app,
        [
            "chat",
            "--path",
            os.fspath(tmp_path),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env={
            "ALYSIS_CONFIG_DIR": os.fspath(config_dir),
            "ALYSIS_DATA_DIR": os.fspath(data_dir),
        },
    )

    assert result.exit_code == 0, result.output
    assert observed_modes == ["readonly", "readonly"]
    assert runtime["session"].mode == "auto"
    assert rebuild_modes == [
        "fullaccess",
        "readonly",
        "fullaccess",
        "auto",
        "readonly",
        "auto",
    ]
