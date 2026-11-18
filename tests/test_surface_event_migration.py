from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

import alysis_code.cli_impl.chat as chat_mod
from alysis_code.agent_loop import AgentSession
from alysis_code.config import AppConfig
from alysis_code.hooks.models import HookDispatchResult
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.model_registry import ModelMeta
from alysis_code.session_store import SessionStore
from alysis_code.usage_tracker import UsageSummary


class _RecordingEventSurface:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.assistant_messages: list[str] = []

    def on_status_update(self, status: object) -> None:
        _ = status

    def on_user_message(self, text: str) -> None:
        _ = text

    def on_progress_update(self, message: str) -> None:
        _ = message

    def on_assistant_token(self, delta: str) -> None:
        _ = delta

    def on_assistant_message_done(self, text: str) -> None:
        self.assistant_messages.append(text)

    def on_tool_start(self, event: object) -> None:
        _ = event

    def on_tool_output(self, event: object) -> None:
        _ = event

    def on_tool_end(self, event: object) -> None:
        _ = event

    def on_patch_generated(self, event: object) -> None:
        _ = event

    def on_warning(self, warning: str) -> None:
        self.events.append({"type": "legacy_warning", "message": warning})

    def on_error(self, err: str) -> None:
        self.events.append({"type": "legacy_error", "message": err})

    def emit_warning(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(
            {
                "type": "warning_emitted",
                "message": message,
                "worker_id": worker_id,
                "role": role,
            }
        )

    def emit_error(
        self,
        code: str,
        message: str,
        recoverable: bool,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(
            {
                "type": "error_raised",
                "code": code,
                "message": message,
                "recoverable": recoverable,
                "worker_id": worker_id,
                "role": role,
            }
        )

    def emit_info(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(
            {
                "type": "info_emitted",
                "message": message,
                "worker_id": worker_id,
                "role": role,
            }
        )

    def emit_mode_changed(self, mode: str) -> None:
        self.events.append({"type": "mode_changed", "mode": mode})


class _FakeRegistry:
    def get(self, model_name: str) -> ModelMeta:
        return ModelMeta(
            model_name=model_name,
            context_window_tokens=8192,
            max_output_tokens=2048,
            input_cost_per_token=None,
            output_cost_per_token=None,
            raw_metadata={},
            source="fallback",
        )


class _FinalClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta: Any = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        self.calls += 1
        return LLMResponse(content="done", tool_calls=[], raw={})


class _BaseHookDispatcher:
    def fire_turn_complete(self, **kwargs: Any) -> HookDispatchResult:
        _ = kwargs
        return HookDispatchResult()

    def fire_session_end(self, **kwargs: Any) -> HookDispatchResult:
        _ = kwargs
        return HookDispatchResult()


class _FailingPromptHookDispatcher(_BaseHookDispatcher):
    def fire_user_prompt_submit(self, **kwargs: Any) -> HookDispatchResult:
        _ = kwargs
        raise RuntimeError("hook exploded")


class _BlockingPromptHookDispatcher(_BaseHookDispatcher):
    def fire_user_prompt_submit(self, **kwargs: Any) -> HookDispatchResult:
        _ = kwargs
        return HookDispatchResult(blocked=True, reason="halt via policy")


class _NoticePromptHookDispatcher(_BaseHookDispatcher):
    def fire_user_prompt_submit(self, **kwargs: Any) -> HookDispatchResult:
        _ = kwargs
        return HookDispatchResult(system_notices=("notice only",))


def _make_session(
    *,
    root: Path,
    surface: _RecordingEventSurface,
    client: _FinalClient,
    hook_dispatcher: object,
) -> AgentSession:
    return AgentSession(
        cfg=AppConfig(model="test-model"),
        root=root,
        mode="auto",
        yes=True,
        stream=False,
        routing_mode="code_only",
        max_steps=2,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,  # type: ignore[arg-type]
        store=SessionStore(
            enabled=False,
            sessions_dir=root / "sessions",
            session_id="s1",
            cwd=str(root),
            repo_root=str(root),
        ),
        client=client,  # type: ignore[arg-type]
        model_registry=_FakeRegistry(),  # type: ignore[arg-type]
        usage_summary=UsageSummary(),
        usage_role="main",
        tool_output_offloader=None,
        conversation_compactor=None,
        tool_output_offload_enabled=False,
        conversation_summarization_enabled=False,
        compaction_profile="chat",
        tools={},
        tool_list=[],
        messages=[{"role": "system", "content": "system prompt"}],
        hook_dispatcher=hook_dispatcher,  # type: ignore[arg-type]
    )


def test_hook_warning_path_emits_warning_event(tmp_path: Path) -> None:
    surface = _RecordingEventSurface()
    client = _FinalClient()
    session = _make_session(
        root=tmp_path,
        surface=surface,
        client=client,
        hook_dispatcher=_FailingPromptHookDispatcher(),
    )
    try:
        exit_code = session.run_turn("continue after warning")
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 1
    assert {
        "type": "warning_emitted",
        "message": "Lifecycle hook dispatch failed: hook exploded",
        "worker_id": None,
        "role": None,
    } in surface.events


def test_hook_notice_path_does_not_emit_warning_event(tmp_path: Path) -> None:
    surface = _RecordingEventSurface()
    client = _FinalClient()
    session = _make_session(
        root=tmp_path,
        surface=surface,
        client=client,
        hook_dispatcher=_NoticePromptHookDispatcher(),
    )
    try:
        exit_code = session.run_turn("continue after notice")
    finally:
        session.close()

    assert exit_code == 0
    assert {"type": "legacy_warning", "message": "notice only"} in surface.events
    assert not any(event.get("type") == "warning_emitted" for event in surface.events)


def test_prompt_blocked_path_emits_error_event(tmp_path: Path) -> None:
    surface = _RecordingEventSurface()
    client = _FinalClient()
    session = _make_session(
        root=tmp_path,
        surface=surface,
        client=client,
        hook_dispatcher=_BlockingPromptHookDispatcher(),
    )
    try:
        exit_code = session.run_turn("blocked by hook")
    finally:
        session.close()

    assert exit_code == 1
    assert client.calls == 0
    assert {
        "type": "error_raised",
        "code": "hook_error",
        "message": "Prompt blocked by hook: halt via policy",
        "recoverable": True,
        "worker_id": None,
        "role": None,
    } in surface.events


def test_chat_mode_change_emits_mode_changed_event(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    surface = _RecordingEventSurface()
    session = SimpleNamespace(
        mode="review",
        cfg=SimpleNamespace(default_mode="review"),
        surface=surface,
    )

    monkeypatch.setattr(
        chat_mod,
        "_rebuild_session_tools_for_mode",
        lambda **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_mod,
        "refresh_session_environment_context_message",
        lambda _s: None,
        raising=False,
    )

    chat_mod._apply_chat_effective_mode(
        session=session,
        next_mode="auto",
        persist_default_mode=True,
    )

    assert session.mode == "auto"
    assert session.cfg.default_mode == "auto"
    assert {"type": "mode_changed", "mode": "auto"} in surface.events
