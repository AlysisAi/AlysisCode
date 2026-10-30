from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

import alysis_code.hooks.dispatcher as hooks_dispatcher_mod
from alysis_code.agent_loop import AgentSession, ToolDef, create_session
from alysis_code.config import AppConfig
from alysis_code.hooks import (
    HOOK_AUDIT_ARTIFACT_PARTS,
    HookDispatcher,
    HookDispatchResult,
    load_resolved_hooks_config,
    project_hooks_config_path,
    read_hook_audit_events,
    trust_project_hooks_config,
)
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.model_registry import ModelMeta
from alysis_code.session_store import SessionStore, read_session_events
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.usage_tracker import UsageSummary


class _RecordingSurface:
    def __init__(self) -> None:
        self.user_messages: list[str] = []
        self.assistant_done: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def on_status_update(self, status) -> None:  # type: ignore[no-untyped-def]
        _ = status

    def on_user_message(self, text: str) -> None:
        self.user_messages.append(text)

    def on_assistant_token(self, delta: str) -> None:
        _ = delta

    def on_assistant_message_done(self, text: str) -> None:
        self.assistant_done.append(text)

    def on_tool_start(self, event) -> None:  # type: ignore[no-untyped-def]
        _ = event

    def on_tool_output(self, event) -> None:  # type: ignore[no-untyped-def]
        _ = event

    def on_tool_end(self, event) -> None:  # type: ignore[no-untyped-def]
        _ = event

    def on_patch_generated(self, event) -> None:  # type: ignore[no-untyped-def]
        _ = event

    def on_warning(self, warning: str) -> None:
        self.warnings.append(warning)

    def on_error(self, err: str) -> None:
        self.errors.append(err)


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


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.call_records: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = stream, on_text_delta, temperature
        self.call_records.append({"messages": list(messages), "tools": tools})
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _store(root: Path, *, enabled: bool = False) -> SessionStore:
    return SessionStore(
        enabled=enabled,
        sessions_dir=root / "sessions",
        session_id="s1",
        cwd=str(root),
        repo_root=str(root),
    )


def _build_hook_dispatcher(
    *,
    workspace_root: Path,
    session_id: str = "s1",
) -> HookDispatcher:
    resolved = load_resolved_hooks_config(workspace_root)
    return HookDispatcher(
        config=resolved,
        workspace_root=workspace_root,
        repo_root=workspace_root,
        session_id=session_id,
        mode="auto",
        runtime_kind="interactive_chat",
    )


def _trust_project_hooks(workspace_root: Path) -> None:
    trust_project_hooks_config(
        workspace_root=workspace_root,
        config_path=project_hooks_config_path(workspace_root),
    )


def _make_session(
    *,
    root: Path,
    client: _ScriptedClient,
    surface: _RecordingSurface,
    tool: ToolDef | None = None,
    hook_dispatcher: HookDispatcher | None = None,
    store: SessionStore | None = None,
) -> AgentSession:
    tools = {} if tool is None else {tool.name: tool}
    tool_list = [] if tool is None else [tool.as_openai_tool()]
    return AgentSession(
        cfg=AppConfig(model="test-model"),
        root=root,
        mode="auto",
        yes=True,
        stream=False,
        routing_mode="code_only",
        max_steps=4,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,  # type: ignore[arg-type]
        store=store or _store(root),
        client=client,  # type: ignore[arg-type]
        model_registry=_FakeRegistry(),  # type: ignore[arg-type]
        usage_summary=UsageSummary(),
        usage_role="main",
        tool_output_offloader=None,
        conversation_compactor=None,
        tool_output_offload_enabled=False,
        conversation_summarization_enabled=False,
        compaction_profile="chat",
        tools=tools,
        tool_list=tool_list,
        messages=[{"role": "system", "content": "system prompt"}],
        hook_dispatcher=hook_dispatcher,
    )


def test_create_session_applies_session_start_hook_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "session-start-hook"}]}]
            }
        },
    )
    _trust_project_hooks(workspace)

    observed_events: list[str] = []
    observed_payloads: list[dict[str, Any]] = []

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        observed_events.append(str(payload["hook_event_name"]))
        observed_payloads.append(payload)
        assert command == "session-start-hook"
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps({"additionalSystemMessages": ["Hook session context"]}),
            stderr="",
        )

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=workspace,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="test-key",
        surface=NoopSurface(),
        session_source="resume",
        session_source_metadata={"from_session_id": "sess-prev", "loaded_message_count": 2},
    )
    try:
        assert observed_events == ["SessionStart"]
        assert observed_payloads[0]["session_source"] == "resume"
        assert observed_payloads[0]["session_source_metadata"] == {
            "from_session_id": "sess-prev",
            "loaded_message_count": 2,
        }
        assert session.hook_dispatcher is not None
        assert any(
            msg.get("role") == "system" and msg.get("content") == "Hook session context"
            for msg in session.messages
        )
        assert session.pinned_prefix_len == len(session.messages)
    finally:
        session.close()


def test_create_session_passes_resume_session_source_to_session_start_hook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "session-start-hook"}]}]
            }
        },
    )
    _trust_project_hooks(workspace)

    observed_payloads: list[dict[str, Any]] = []

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        observed_payloads.append(payload)
        assert command == "session-start-hook"
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=workspace,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="test-key",
        surface=NoopSurface(),
        session_log_dir_override=tmp_path / "sessions",
        session_source="resume",
    )
    try:
        assert observed_payloads
        assert observed_payloads[0]["hook_event_name"] == "SessionStart"
        assert observed_payloads[0]["session_source"] == "resume"
        session_start_events = [
            event
            for event in read_session_events(session.store.path)
            if event.get("type") == "session_start"
        ]
        assert session_start_events
        assert session_start_events[0]["payload"]["session_source"] == "resume"
    finally:
        session.close()


def test_create_session_fires_session_end_hook_on_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "SessionEnd": [{"hooks": [{"type": "command", "command": "session-end-hook"}]}]
            }
        },
    )
    _trust_project_hooks(workspace)

    observed_payloads: list[dict[str, Any]] = []

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        observed_payloads.append(payload)
        assert command == "session-end-hook"
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=workspace,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="test-key",
        surface=NoopSurface(),
        session_source="resume",
        session_source_metadata={"from_session_id": "sess-prev"},
    )
    session.close(reason="user_exit")

    assert observed_payloads
    assert observed_payloads[0]["hook_event_name"] == "SessionEnd"
    assert observed_payloads[0]["reason"] == "user_exit"
    assert observed_payloads[0]["session_source"] == "resume"
    assert observed_payloads[0]["session_source_metadata"] == {"from_session_id": "sess-prev"}


def test_close_fires_session_end_hook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(workspace),
        {
            "hooks": {
                "SessionEnd": [{"hooks": [{"type": "command", "command": "session-end-hook"}]}]
            }
        },
    )
    _trust_project_hooks(workspace)

    observed_payloads: list[dict[str, Any]] = []

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        observed_payloads.append(payload)
        assert command == "session-end-hook"
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=workspace,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="test-key",
        surface=NoopSurface(),
    )

    session.close(reason="manual_shutdown")

    assert observed_payloads
    payload = observed_payloads[-1]
    assert payload["hook_event_name"] == "SessionEnd"
    assert payload["reason"] == "manual_shutdown"
    assert payload["session_source"] == "startup"
    assert payload["active_workdir_relpath"] == "."


def _session_end_retry_fixture(tmp_path, monkeypatch, effect):
    """Real close/dispatch path with inert external cleanup and hook effects."""
    surface = _RecordingSurface()
    session = _make_session(root=tmp_path, client=_ScriptedClient([]), surface=surface)
    calls = []
    cleanup = []

    def dispatch(**kwargs):
        calls.append(kwargs["payload"])
        effect(session)
        return HookDispatchResult()

    session.hook_dispatcher = SimpleNamespace(fire_session_end=dispatch)
    session.mcp_manager = SimpleNamespace(close=lambda: cleanup.append("mcp"))
    monkeypatch.setattr(session.store, "close", lambda: cleanup.append("store"))
    return session, calls, cleanup, surface


def test_session_end_is_not_replayed_after_mcp_cleanup_failure(tmp_path, monkeypatch):
    session, calls, cleanup, _ = _session_end_retry_fixture(
        tmp_path, monkeypatch, lambda session: None
    )

    def close_mcp():
        cleanup.append("mcp")
        if cleanup.count("mcp") == 1:
            raise RuntimeError("cleanup incomplete")

    session.mcp_manager.close = close_mcp
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        session.close(reason="user_exit")
    session.close(reason="cleanup_retry")

    assert [call["reason"] for call in calls] == ["user_exit"]
    assert cleanup == ["mcp", "store", "mcp", "store"]


@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError])
def test_session_end_uncertain_effect_is_not_replayed(tmp_path, monkeypatch, failure):
    effects = []

    def uncertain_hook(session):
        effects.append("external effect may have happened")
        raise failure("uncertain hook result")

    session, calls, cleanup, surface = _session_end_retry_fixture(
        tmp_path, monkeypatch, uncertain_hook
    )
    session.close()
    session.close()

    assert len(effects) == len(calls) == 1
    assert any("uncertain hook result" in warning for warning in surface.warnings)
    assert cleanup == ["mcp", "store", "mcp", "store"]


def test_session_end_reentrant_close_does_not_dispatch_twice(tmp_path, monkeypatch):
    def reenter(session):
        session.close(reason="reentrant_cleanup")

    session, calls, cleanup, _ = _session_end_retry_fixture(tmp_path, monkeypatch, reenter)
    session.close(reason="outer_close")

    assert [call["reason"] for call in calls] == ["outer_close"]
    assert cleanup == ["mcp", "store", "mcp", "store"]


def test_session_end_concurrent_close_latches_before_blocking_hook(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def blocked_hook(session):
        entered.set()
        if not release.wait(timeout=2):
            raise TimeoutError("fixture did not release hook")

    session, calls, cleanup, surface = _session_end_retry_fixture(
        tmp_path, monkeypatch, blocked_hook
    )

    def close(reason):
        try:
            session.close(reason=reason)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=close, args=("first",), daemon=True)
    second = threading.Thread(target=close, args=("second",), daemon=True)
    first.start()
    try:
        assert entered.wait(timeout=1)
        second.start()
        second.join(timeout=1)
        assert not second.is_alive(), "the latch lock must not cover hook execution"
        assert [call["reason"] for call in calls] == ["first"]
    finally:
        release.set()
        first.join(timeout=2)
        if second.ident is not None:
            second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert not surface.warnings
    assert cleanup.count("mcp") == cleanup.count("store") == 2


def test_session_end_attempt_does_not_hide_later_store_failure(tmp_path, monkeypatch):
    session, calls, cleanup, _ = _session_end_retry_fixture(
        tmp_path, monkeypatch, lambda session: None
    )

    def close_store():
        cleanup.append("store")
        if cleanup.count("store") == 1:
            raise OSError("store cleanup incomplete")

    monkeypatch.setattr(session.store, "close", close_store)
    with pytest.raises(OSError, match="store cleanup incomplete"):
        session.close()
    session.close()
    assert len(calls) == 1
    assert cleanup == ["mcp", "store", "mcp", "store"]


def test_session_end_without_hooks_preserves_cleanup_retry(tmp_path, monkeypatch):
    session, calls, cleanup, _ = _session_end_retry_fixture(
        tmp_path, monkeypatch, lambda session: None
    )
    session.hook_dispatcher = None
    session.close()
    session.close()
    assert calls == []
    assert cleanup == ["mcp", "store", "mcp", "store"]


def test_session_end_latch_is_per_instance(tmp_path, monkeypatch):
    sessions = [
        _session_end_retry_fixture(tmp_path / name, monkeypatch, lambda session: None)
        for name in ("first", "second")
    ]
    for session, calls, cleanup, _ in sessions:
        session.close()
        session.close()
        assert len(calls) == 1
        assert cleanup == ["mcp", "store", "mcp", "store"]


def test_session_end_context_failure_consumes_attempt(tmp_path, monkeypatch):
    session, calls, cleanup, _ = _session_end_retry_fixture(
        tmp_path, monkeypatch, lambda session: None
    )
    original = session._hook_runtime_context
    failure = RuntimeError("context unavailable before hook")
    monkeypatch.setattr(session, "_hook_runtime_context", lambda: (_ for _ in ()).throw(failure))
    with pytest.raises(RuntimeError) as observed:
        session.close()
    assert observed.value is failure
    monkeypatch.setattr(session, "_hook_runtime_context", original)
    session.close()
    assert calls == []
    assert cleanup == ["store", "mcp", "store"]


def _session_end_bridge_fixture(tmp_path, agent):
    from alysis_code.ide.artifacts import ArtifactStore
    from alysis_code.ide.event_stream import EventContext, EventSequencer, ProtocolEventSurface
    from alysis_code.ide.prompt_queue import DurablePromptQueue
    from alysis_code.ide.stdio_bridge import BridgeJob, BridgeSession, StdioBridge

    output = io.StringIO()
    queue = DurablePromptQueue(tmp_path / "close.sqlite3")
    bridge = StdioBridge(stdout=output, prompt_queue=queue)
    session = BridgeSession(
        session_id="ordinary-close",
        root=tmp_path,
        mode="auto",
        surface=ProtocolEventSurface(
            context=EventContext(session_id="ordinary-close"),
            emit=bridge._record_and_write_event,
            sequencer=EventSequencer(),
        ),
        agent_session=agent,
        artifact_store=ArtifactStore([]),
        sensitive_media=False,
    )
    job = BridgeJob(
        job_id="retained-terminal",
        session_id=session.session_id,
        created_at="now",
        status="completed",
        exit_code=0,
        worker_never_started=True,
    )
    session.last_job = job
    bridge._sessions[session.session_id] = session
    bridge._jobs[job.job_id] = job

    def cancel(request_id):
        bridge.process_line(
            json.dumps(
                {
                    "protocol_version": "1",
                    "id": request_id,
                    "method": "session.cancel",
                    "params": {"session_id": session.session_id},
                }
            )
            + "\n"
        )
        return next(
            json.loads(line)
            for line in output.getvalue().splitlines()
            if json.loads(line).get("id") == request_id
        )

    return bridge, session, job, cancel


@pytest.mark.parametrize("failed_cleanup", ["mcp", "store"])
@pytest.mark.parametrize("uncertain_hook", [False, True])
def test_session_end_bridge_cleanup_failure_stays_blocked(
    tmp_path, monkeypatch, failed_cleanup, uncertain_hook
):
    """Current main's real legacy cancellation refuses retry; never restore old success."""
    effects = []

    def hook_effect(session):
        effects.append("possible hook effect")
        if uncertain_hook:
            raise TimeoutError("inert hook uncertainty")

    agent, calls, cleanup, _ = _session_end_retry_fixture(tmp_path, monkeypatch, hook_effect)
    failure = OSError("inert late cleanup failure")

    def close_component(component):
        cleanup.append(component)
        if component == failed_cleanup:
            raise failure

    agent.mcp_manager.close = lambda: close_component("mcp")
    monkeypatch.setattr(agent.store, "close", lambda: close_component("store"))
    bridge, session, job, cancel = _session_end_bridge_fixture(tmp_path, agent)
    try:
        failed = cancel("first")
        assert failed["error"]["code"] == "browser_cleanup_incomplete"
        assert session.cancellation_uncertain is True
        assert session.cancellation_transitions == 1
        assert not session.closed and session.close_when_idle
        assert bridge._sessions[session.session_id] is session
        assert bridge._jobs[job.job_id] is job
        assert cancel("retry")["error"]["code"] == "session_busy"
        assert len(effects) == len(calls) == 1
        assert cleanup == ["mcp", "store"]
    finally:
        # Fixture-only queue handle retirement; do not retry failed agent cleanup.
        bridge._prompt_queue.release_owner(bridge._prompt_queue_owner_id)


@pytest.mark.parametrize("failed_cleanup", ["mcp", "store"])
def test_session_end_async_bridge_preserves_original_close_error(
    tmp_path, monkeypatch, failed_cleanup
):
    entered = threading.Event()
    release = threading.Event()

    def hook_effect(session):
        entered.set()
        assert release.wait(2), "fixture hook was not released"

    agent, calls, cleanup, _ = _session_end_retry_fixture(tmp_path, monkeypatch, hook_effect)
    failure = OSError("original late close failure")

    def close_component(component):
        cleanup.append(component)
        if component == failed_cleanup:
            raise failure

    agent.mcp_manager.close = lambda: close_component("mcp")
    monkeypatch.setattr(agent.store, "close", lambda: close_component("store"))
    bridge, session, job, cancel = _session_end_bridge_fixture(tmp_path, agent)
    session.close_when_idle = True
    worker = None
    try:
        bridge._schedule_session_close_after_job(session, job)
        assert entered.wait(1)
        worker = session.close_worker
        assert worker is not None
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        original = session.close_error
        assert original is not None and original.__cause__ is failure
        assert session.close_attempted is True
        assert session.close_worker is None and not session.closed
        bridge._schedule_session_close_after_job(session, job)
        assert session.close_worker is None
        assert session.close_error is original
        assert cancel("retry")["error"]["code"] == "session_busy"
        assert len(calls) == 1 and cleanup == ["mcp", "store"]
    finally:
        release.set()
        if worker is not None:
            worker.join(2)
        bridge._prompt_queue.release_owner(bridge._prompt_queue_owner_id)


def test_run_turn_prompt_hook_rewrites_prompt_and_fires_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "prompt-hook"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "stop-hook"}]}],
            }
        },
    )
    _trust_project_hooks(tmp_path)

    stop_payloads: list[dict[str, Any]] = []

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        event_name = payload["hook_event_name"]
        if event_name == "UserPromptSubmit":
            assert command == "prompt-hook"
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "modifiedPrompt": "rewritten prompt",
                        "additionalSystemMessages": ["Hook turn context"],
                    }
                ),
                stderr="",
            )
        if event_name == "TurnComplete":
            assert command == "stop-hook"
            assert payload["legacy_hook_event_name"] == "Stop"
            stop_payloads.append(payload)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
        raise AssertionError(f"Unexpected hook event: {event_name}")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    client = _ScriptedClient([LLMResponse(content="done", tool_calls=[], raw={})])
    surface = _RecordingSurface()
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("original prompt")
    finally:
        session.close()

    assert exit_code == 0
    assert surface.user_messages == ["rewritten prompt"]
    assert surface.assistant_done[-1] == "done"
    request_messages = client.call_records[0]["messages"]
    assert any(
        msg.get("role") == "system" and msg.get("content") == "Hook turn context"
        for msg in request_messages
    )
    assert stop_payloads and stop_payloads[-1]["reason"] == "completed"
    assert stop_payloads[-1]["hook_event_name"] == "TurnComplete"
    assert stop_payloads[-1]["final_text"] == "done"


def test_run_turn_pre_and_post_tool_hooks_modify_tool_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": "pre-tool-hook"}],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": "post-tool-hook"}],
                    }
                ],
            }
        },
    )
    _trust_project_hooks(tmp_path)

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        event_name = payload["hook_event_name"]
        if event_name == "PreToolUse":
            assert command == "pre-tool-hook"
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "modifiedInput": {"msg": "mutated"},
                        "additionalSystemMessages": ["Pre hook note"],
                    }
                ),
                stderr="",
            )
        if event_name == "PostToolUse":
            assert command == "post-tool-hook"
            assert payload["tool_input"] == {"msg": "mutated"}
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps({"additionalSystemMessages": ["Post hook note"]}),
                stderr="",
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    observed_tool_inputs: list[dict[str, Any]] = []
    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda args: observed_tool_inputs.append(dict(args)) or {"ok": args["msg"]},
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo_tool", arguments={"msg": "original"})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    surface = _RecordingSurface()
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        tool=tool,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("run the tool")
    finally:
        session.close()

    assert exit_code == 0
    assert observed_tool_inputs == [{"msg": "mutated"}]
    follow_up_messages = client.call_records[1]["messages"]
    assert any(
        msg.get("role") == "system" and msg.get("content") == "Pre hook note"
        for msg in follow_up_messages
    )
    assert any(
        msg.get("role") == "system" and msg.get("content") == "Post hook note"
        for msg in follow_up_messages
    )
    assert any(
        msg.get("role") == "tool" and '"ok":"mutated"' in str(msg.get("content") or "")
        for msg in follow_up_messages
    )


def test_run_turn_pre_tool_hook_can_block_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": "block-tool-hook"}],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        if payload["hook_event_name"] == "PreToolUse":
            return subprocess.CompletedProcess(
                args=command,
                returncode=2,
                stdout="",
                stderr="blocked by policy",
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    invoked = {"value": False}
    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda _args: invoked.__setitem__("value", True) or {"ok": True},
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo_tool", arguments={"msg": "original"})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    surface = _RecordingSurface()
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        tool=tool,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("run the blocked tool")
    finally:
        session.close()

    assert exit_code == 0
    assert invoked["value"] is False
    follow_up_messages = client.call_records[1]["messages"]
    assert any(
        msg.get("role") == "tool"
        and "Blocked by hook: blocked by policy" in str(msg.get("content"))
        for msg in follow_up_messages
    )


def test_pre_tool_hook_runtime_kinds_filter_skips_mismatched_hook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "pre-tool-hook",
                                "runtimeKinds": ["swarm_worker"],
                            }
                        ],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    invoked_commands: list[str] = []

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        invoked_commands.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    observed_tool_inputs: list[dict[str, Any]] = []
    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda args: observed_tool_inputs.append(dict(args)) or {"ok": args["msg"]},
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo_tool", arguments={"msg": "original"})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingSurface(),
        tool=tool,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("run the tool")
    finally:
        session.close()

    assert exit_code == 0
    assert invoked_commands == []
    assert observed_tool_inputs == [{"msg": "original"}]


def test_matching_hooks_execute_in_priority_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "low-priority-hook",
                                "priority": -10,
                            },
                            {
                                "type": "command",
                                "command": "high-priority-hook",
                                "priority": 10,
                            },
                        ],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    observed_commands: list[str] = []

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        if payload["hook_event_name"] == "PreToolUse":
            observed_commands.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda args: {"ok": args["msg"]},
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo_tool", arguments={"msg": "original"})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingSurface(),
        tool=tool,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("run the tool")
    finally:
        session.close()

    assert exit_code == 0
    assert observed_commands == ["high-priority-hook", "low-priority-hook"]


def test_failure_policy_block_turns_hook_runtime_failure_into_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "failing-hook",
                                "failurePolicy": "block",
                            }
                        ],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        if payload["hook_event_name"] == "PreToolUse":
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr="boom",
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    invoked = {"value": False}
    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda _args: invoked.__setitem__("value", True) or {"ok": True},
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo_tool", arguments={"msg": "original"})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    surface = _RecordingSurface()
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        tool=tool,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("run the blocked tool")
    finally:
        session.close()

    assert exit_code == 0
    assert invoked["value"] is False
    follow_up_messages = client.call_records[1]["messages"]
    assert any(
        msg.get("role") == "tool" and "Blocked by hook: boom" in str(msg.get("content"))
        for msg in follow_up_messages
    )


def test_run_turn_writes_redacted_hook_audit_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "prompt-hook --token sk-secret-token-value",
                                "id": "prompt.guard",
                            }
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "stop-hook",
                                "id": "turn.stop",
                            }
                        ]
                    }
                ],
            }
        },
    )
    _trust_project_hooks(tmp_path)

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        if payload["hook_event_name"] == "UserPromptSubmit":
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps({"modifiedPrompt": "rewritten prompt"}),
                stderr="",
            )
        if payload["hook_event_name"] == "TurnComplete":
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
        raise AssertionError(f"Unexpected hook event: {payload['hook_event_name']}")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    store = _store(tmp_path, enabled=True)
    hook_dispatcher = HookDispatcher(
        config=load_resolved_hooks_config(tmp_path),
        workspace_root=tmp_path,
        repo_root=tmp_path,
        session_id="s1",
        mode="auto",
        runtime_kind="interactive_chat",
        audit_callback=lambda payload: store.append_artifact_jsonl(
            *HOOK_AUDIT_ARTIFACT_PARTS,
            payload=payload,
        ),
    )
    client = _ScriptedClient([LLMResponse(content="done", tool_calls=[], raw={})])
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingSurface(),
        hook_dispatcher=hook_dispatcher,
        store=store,
    )
    try:
        exit_code = session.run_turn("original prompt with sk-secret-token-value")
    finally:
        session.close()
        store.close()

    assert exit_code == 0
    artifact_path = store.runtime_artifact_path(*HOOK_AUDIT_ARTIFACT_PARTS)
    events = list(read_hook_audit_events(artifact_path))
    assert [event["event_name"] for event in events] == ["UserPromptSubmit", "TurnComplete"]
    assert all("prompt" not in event for event in events)
    assert all("tool_input" not in event for event in events)
    assert events[0]["hook_id"] == "prompt.guard"
    assert events[0]["modified_prompt"] is True
    assert events[0]["modified_prompt_chars"] == len("rewritten prompt")
    assert events[0]["command_preview"] == "prompt-hook --token [REDACTED]"


def test_pre_tool_hook_decision_allow_short_circuits_remaining(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "allow-hook",
                                "priority": 10,
                            },
                            {
                                "type": "command",
                                "command": "skipped-hook",
                                "priority": 0,
                            },
                        ],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    observed_commands: list[str] = []

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed_commands.append(command)
        if command == "allow-hook":
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps({"decision": "allow"}),
                stderr="",
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda args: {"ok": True},
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingSurface(),
        tool=tool,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("use the tool")
    finally:
        session.close()

    assert exit_code == 0
    assert "allow-hook" in observed_commands
    assert "skipped-hook" not in observed_commands


def test_user_prompt_submit_hook_continue_false_blocks_with_stop_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "halt-hook"}]}]}},
    )
    _trust_project_hooks(tmp_path)

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command == "halt-hook"
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps({"continue": False, "stopReason": "halt via policy"}),
            stderr="",
        )

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    client = _ScriptedClient([LLMResponse(content="unreached", tool_calls=[], raw={})])
    surface = _RecordingSurface()
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("do the thing")
    finally:
        session.close()

    assert exit_code == 1
    assert client.calls == 0
    assert any("halt via policy" in err for err in surface.errors)


def test_pre_tool_hook_system_message_routes_to_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": "notice-hook"}],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command == "notice-hook"
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps({"systemMessage": "demo banner"}),
            stderr="",
        )

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda args: {"ok": True},
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    surface = _RecordingSurface()
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        tool=tool,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("run it")
    finally:
        session.close()

    assert exit_code == 0
    assert "demo banner" in surface.warnings


def test_pre_tool_hook_permission_decision_deny_blocks_with_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": "perm-hook"}],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command == "perm-hook"
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(
                {
                    "hookSpecificOutput": {
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "policy forbids echo_tool",
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    tool_call_count = 0

    def tool_run(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal tool_call_count
        tool_call_count += 1
        return {"ok": True}

    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=tool_run,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingSurface(),
        tool=tool,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("try the tool")
    finally:
        session.close()

    assert exit_code == 0
    assert tool_call_count == 0
    tool_messages = [msg for msg in session.messages if msg.get("role") == "tool"]
    assert tool_messages
    assert "policy forbids echo_tool" in tool_messages[-1]["content"]


def test_pre_tool_block_fires_notification_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": "blocking-hook"}],
                    }
                ],
                "Notification": [{"hooks": [{"type": "command", "command": "notify-hook"}]}],
            }
        },
    )
    _trust_project_hooks(tmp_path)

    observed_events: list[str] = []
    observed_payloads: list[dict[str, Any]] = []

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        observed_events.append(payload["hook_event_name"])
        observed_payloads.append(payload)
        if command == "blocking-hook":
            return subprocess.CompletedProcess(
                args=command,
                returncode=2,
                stdout="",
                stderr="blocked",
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    tool = ToolDef(
        name="echo_tool",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run=lambda args: {"ok": True},
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingSurface(),
        tool=tool,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("trigger a block")
    finally:
        session.close()

    assert exit_code == 0
    assert "Notification" in observed_events
    notification_payload = next(
        p for p in observed_payloads if p["hook_event_name"] == "Notification"
    )
    assert notification_payload["cause"] == "pre_tool_use_blocked"
    assert notification_payload["level"] == "warning"
    assert notification_payload["tool_name"] == "echo_tool"


def test_subagent_tool_result_fires_subagent_stop_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "SubagentStop": [{"hooks": [{"type": "command", "command": "subagent-stop-hook"}]}]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    observed_payloads: list[dict[str, Any]] = []

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        observed_payloads.append(payload)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    def fake_subagent_run(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "subagent": "explorer",
            "subagent_session_id": "sub-001",
            "result": "findings",
            "exit_code": 0,
            "usage": {},
        }

    subagent_tool = ToolDef(
        name="run_subagent",
        description="run a subagent",
        parameters={"type": "object", "properties": {}, "required": []},
        run=fake_subagent_run,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="run_subagent", arguments={})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=_RecordingSurface(),
        tool=subagent_tool,
        hook_dispatcher=_build_hook_dispatcher(workspace_root=tmp_path),
    )
    try:
        exit_code = session.run_turn("launch subagent")
    finally:
        session.close()

    assert exit_code == 0
    subagent_events = [p for p in observed_payloads if p["hook_event_name"] == "SubagentStop"]
    assert subagent_events
    payload = subagent_events[0]
    assert payload["tool_name"] == "run_subagent"
    assert payload["subagent_name"] == "explorer"
    assert payload["subagent_session_id"] == "sub-001"
    assert payload["status"] == "success"
    assert payload["exit_code"] == 0


def _env_probe_command() -> str:
    return (
        f'{_shell_quote(sys.executable)} -c "import os,json,sys; '
        "print(json.dumps({'has_openai': 'OPENAI_API_KEY' in os.environ, "
        "'my_flag': os.environ.get('MY_FLAG'), "
        "'has_path': 'PATH' in os.environ, "
        "'has_home': 'HOME' in os.environ, "
        "'has_random': 'RANDOM_VAR' in os.environ, "
        "'has_alysis': 'ALYSIS_SESSION_ID' in os.environ}))\""
    )


def test_env_sandbox_safe_mode_strips_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-xxx")
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "TurnComplete": [{"hooks": [{"type": "command", "command": _env_probe_command()}]}]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    captured: list[dict[str, Any]] = []

    real_run = subprocess.run

    def capture_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = real_run(command, **kwargs)
        try:
            parsed = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            parsed = {}
        captured.append(parsed)
        return result

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", capture_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    dispatcher.fire_turn_complete(
        cwd=tmp_path,
        active_workdir_relpath=".",
        payload={"reason": "completed"},
    )

    assert captured
    assert captured[0]["has_openai"] is False
    assert captured[0]["has_path"] is True
    assert captured[0]["has_alysis"] is True


def test_env_sandbox_env_allow_restores_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-xxx")
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "TurnComplete": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": _env_probe_command(),
                                "envAllow": ["OPENAI_API_KEY"],
                            }
                        ]
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    captured: list[dict[str, Any]] = []
    real_run = subprocess.run

    def capture_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = real_run(command, **kwargs)
        try:
            captured.append(json.loads(result.stdout.strip()))
        except json.JSONDecodeError:
            captured.append({})
        return result

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", capture_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    dispatcher.fire_turn_complete(
        cwd=tmp_path,
        active_workdir_relpath=".",
        payload={"reason": "completed"},
    )

    assert captured
    assert captured[0]["has_openai"] is True


def test_env_sandbox_explicit_mode_keeps_only_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-xxx")
    monkeypatch.setenv("RANDOM_VAR", "noise")
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "TurnComplete": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": _env_probe_command(),
                                "envPassthrough": "explicit",
                            }
                        ]
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    captured: list[dict[str, Any]] = []
    real_run = subprocess.run

    def capture_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = real_run(command, **kwargs)
        try:
            captured.append(json.loads(result.stdout.strip()))
        except json.JSONDecodeError:
            captured.append({})
        return result

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", capture_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    dispatcher.fire_turn_complete(
        cwd=tmp_path,
        active_workdir_relpath=".",
        payload={"reason": "completed"},
    )

    assert captured
    assert captured[0]["has_openai"] is False
    assert captured[0]["has_random"] is False
    assert captured[0]["has_path"] is True
    assert captured[0]["has_alysis"] is True


def test_env_sandbox_env_dict_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "TurnComplete": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": _env_probe_command(),
                                "env": {"MY_FLAG": "1"},
                            }
                        ]
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    captured: list[dict[str, Any]] = []
    real_run = subprocess.run

    def capture_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = real_run(command, **kwargs)
        try:
            captured.append(json.loads(result.stdout.strip()))
        except json.JSONDecodeError:
            captured.append({})
        return result

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", capture_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    dispatcher.fire_turn_complete(
        cwd=tmp_path,
        active_workdir_relpath=".",
        payload={"reason": "completed"},
    )

    assert captured
    assert captured[0]["my_flag"] == "1"


def test_env_sandbox_all_mode_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-xxx")
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "TurnComplete": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": _env_probe_command(),
                                "envPassthrough": "all",
                            }
                        ]
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    captured: list[dict[str, Any]] = []
    real_run = subprocess.run

    def capture_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = real_run(command, **kwargs)
        try:
            captured.append(json.loads(result.stdout.strip()))
        except json.JSONDecodeError:
            captured.append({})
        return result

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", capture_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    dispatcher.fire_turn_complete(
        cwd=tmp_path,
        active_workdir_relpath=".",
        payload={"reason": "completed"},
    )

    assert captured
    assert captured[0]["has_openai"] is True


def _payload_inspect_command() -> str:
    return (
        f'{_shell_quote(sys.executable)} -c "import json,sys; '
        "d=json.load(sys.stdin); "
        "ti=d.get('tool_input',{}); "
        "c=ti.get('content'); "
        "print(json.dumps({"
        "'is_dict': isinstance(c,dict), "
        "'len': len(c) if isinstance(c,str) else None, "
        "'marker_keys': sorted(c.keys()) if isinstance(c,dict) else None, "
        "'marker_bytes': c.get('bytes') if isinstance(c,dict) else None, "
        "'marker_preview': c.get('preview') if isinstance(c,dict) else None, "
        "'marker_sha256': c.get('sha256') if isinstance(c,dict) else None"
        '}))"'
    )


def _shell_quote(value: str) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def test_payload_truncation_oversized_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": _payload_inspect_command()}],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    captured: list[dict[str, Any]] = []
    real_run = subprocess.run

    def capture_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = real_run(command, **kwargs)
        try:
            captured.append(json.loads(result.stdout.strip()))
        except json.JSONDecodeError:
            captured.append({})
        return result

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", capture_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    big = "A" * (2 * 1024 * 1024)
    dispatcher.fire_post_tool_use(
        tool_name="echo_tool",
        tool_input={"content": big},
        tool_response={"ok": True},
        cwd=tmp_path,
        active_workdir_relpath=".",
        step=1,
    )

    assert captured
    assert captured[0]["is_dict"] is True
    assert captured[0]["marker_keys"] == [
        "__truncated",
        "bytes",
        "preview",
        "sha256",
    ]


def test_payload_truncation_respects_opt_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [
                            {
                                "type": "command",
                                "command": _payload_inspect_command(),
                                "receivesFullPayload": True,
                            }
                        ],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    captured: list[dict[str, Any]] = []
    real_run = subprocess.run

    def capture_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = real_run(command, **kwargs)
        try:
            captured.append(json.loads(result.stdout.strip()))
        except json.JSONDecodeError:
            captured.append({})
        return result

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", capture_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    big = "A" * (2 * 1024 * 1024)
    dispatcher.fire_post_tool_use(
        tool_name="echo_tool",
        tool_input={"content": big},
        tool_response={"ok": True},
        cwd=tmp_path,
        active_workdir_relpath=".",
        step=1,
    )

    assert captured
    assert captured[0]["is_dict"] is False
    assert captured[0]["len"] == len(big)


def test_payload_truncation_marker_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": _payload_inspect_command()}],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    captured: list[dict[str, Any]] = []
    real_run = subprocess.run

    def capture_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = real_run(command, **kwargs)
        try:
            captured.append(json.loads(result.stdout.strip()))
        except json.JSONDecodeError:
            captured.append({})
        return result

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", capture_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    big = "A" * (2 * 1024 * 1024)
    dispatcher.fire_post_tool_use(
        tool_name="echo_tool",
        tool_input={"content": big},
        tool_response={"ok": True},
        cwd=tmp_path,
        active_workdir_relpath=".",
        step=1,
    )

    assert captured
    marker = captured[0]
    assert isinstance(marker["marker_bytes"], int)
    assert marker["marker_bytes"] == len(big)
    assert isinstance(marker["marker_preview"], str)
    assert len(marker["marker_preview"]) <= 256
    assert isinstance(marker["marker_sha256"], str)
    assert len(marker["marker_sha256"]) == 64
    assert all(ch in "0123456789abcdef" for ch in marker["marker_sha256"])


def test_payload_truncation_audit_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": _payload_inspect_command()}],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    real_run = subprocess.run

    def capture_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return real_run(command, **kwargs)

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", capture_run)

    audit_events: list[dict[str, Any]] = []
    dispatcher = HookDispatcher(
        config=load_resolved_hooks_config(tmp_path),
        workspace_root=tmp_path,
        repo_root=tmp_path,
        session_id="s1",
        mode="auto",
        runtime_kind="interactive_chat",
        audit_callback=lambda payload: audit_events.append(payload),
    )
    big = "A" * (2 * 1024 * 1024)
    dispatcher.fire_post_tool_use(
        tool_name="echo_tool",
        tool_input={"content": big},
        tool_response={"ok": True},
        cwd=tmp_path,
        active_workdir_relpath=".",
        step=1,
    )

    assert audit_events
    assert audit_events[0].get("payload_truncated") is True
    assert audit_events[0].get("payload_bytes", 0) > 2 * 1024 * 1024


def _build_parallel_hook_dispatcher(
    *,
    workspace_root: Path,
    parallel_enabled: bool,
    max_parallel_workers: int = 4,
) -> HookDispatcher:
    return HookDispatcher(
        config=load_resolved_hooks_config(workspace_root),
        workspace_root=workspace_root,
        repo_root=workspace_root,
        session_id="s1",
        mode="auto",
        runtime_kind="interactive_chat",
        parallel_enabled=parallel_enabled,
        max_parallel_workers=max_parallel_workers,
    )


def test_parallel_execution_non_blocking_speedup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "TurnComplete": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "fake-hook-a",
                                "id": "fake-a",
                            },
                            {
                                "type": "command",
                                "command": "fake-hook-b",
                                "id": "fake-b",
                            },
                            {
                                "type": "command",
                                "command": "fake-hook-c",
                                "id": "fake-c",
                            },
                        ],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    expected_commands = {"fake-hook-a", "fake-hook-b", "fake-hook-c"}
    barrier = threading.Barrier(len(expected_commands), timeout=5.0)
    lock = threading.Lock()
    active_count = 0
    max_active_count = 0
    started_commands: list[str] = []

    def fake_run(
        command: str,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal active_count, max_active_count
        assert command in expected_commands
        assert kwargs["shell"] is True
        assert kwargs["cwd"] == os.fspath(tmp_path)
        assert kwargs["capture_output"] is True
        assert isinstance(kwargs["input"], str)

        with lock:
            started_commands.append(command)
            active_count += 1
            max_active_count = max(max_active_count, active_count)
        try:
            barrier.wait()
        except threading.BrokenBarrierError as exc:
            raise AssertionError(
                "parallel hook dispatch did not start all hooks concurrently"
            ) from exc
        finally:
            with lock:
                active_count -= 1
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    dispatcher = _build_parallel_hook_dispatcher(
        workspace_root=tmp_path,
        parallel_enabled=True,
        max_parallel_workers=4,
    )
    result = dispatcher.fire_turn_complete(
        cwd=tmp_path,
        active_workdir_relpath=".",
        payload={"reason": "completed"},
    )

    assert result.blocked is False
    assert set(started_commands) == expected_commands
    assert max_active_count == len(expected_commands)


def test_parallel_execution_disabled_is_serial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "TurnComplete": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "fake-hook-a",
                                "id": "fake-a",
                            },
                            {
                                "type": "command",
                                "command": "fake-hook-b",
                                "id": "fake-b",
                            },
                            {
                                "type": "command",
                                "command": "fake-hook-c",
                                "id": "fake-c",
                            },
                        ],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    expected_commands = ["fake-hook-a", "fake-hook-b", "fake-hook-c"]
    caller_thread_id = threading.get_ident()
    observed: list[tuple[str, int]] = []

    def fake_run(
        command: str,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        assert command in expected_commands
        assert kwargs["shell"] is True
        assert kwargs["cwd"] == os.fspath(tmp_path)
        assert kwargs["capture_output"] is True
        assert isinstance(kwargs["input"], str)
        observed.append((command, threading.get_ident()))
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    dispatcher = _build_parallel_hook_dispatcher(
        workspace_root=tmp_path,
        parallel_enabled=False,
        max_parallel_workers=4,
    )
    result = dispatcher.fire_turn_complete(
        cwd=tmp_path,
        active_workdir_relpath=".",
        payload={"reason": "completed"},
    )

    assert result.blocked is False
    assert [command for command, _thread_id in observed] == expected_commands
    assert {thread_id for _command, thread_id in observed} == {caller_thread_id}


def test_parallel_execution_preserves_merge_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    # Hook priorities 30/20/10 force deterministic config order: msg-0, msg-1, msg-2.
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "TurnComplete": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "emit-0",
                                "id": "emit-0",
                                "priority": 30,
                            },
                            {
                                "type": "command",
                                "command": "emit-1",
                                "id": "emit-1",
                                "priority": 20,
                            },
                            {
                                "type": "command",
                                "command": "emit-2",
                                "id": "emit-2",
                                "priority": 10,
                            },
                        ],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    expected_commands = {"emit-0", "emit-1", "emit-2"}
    barrier = threading.Barrier(len(expected_commands), timeout=5.0)
    emit_1_done = threading.Event()
    emit_2_done = threading.Event()
    completion_order: list[str] = []
    lock = threading.Lock()

    def fake_run(
        command: str,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        assert command in expected_commands
        assert kwargs["shell"] is True
        assert kwargs["cwd"] == os.fspath(tmp_path)
        assert kwargs["capture_output"] is True
        assert isinstance(kwargs["input"], str)
        try:
            barrier.wait()
        except threading.BrokenBarrierError as exc:
            raise AssertionError(
                "parallel hook dispatch did not start all hooks concurrently"
            ) from exc

        if command == "emit-2":
            assert emit_1_done.wait(timeout=5.0)
        elif command == "emit-0":
            assert emit_2_done.wait(timeout=5.0)

        messages = {
            "emit-0": "msg-0",
            "emit-1": "msg-1",
            "emit-2": "msg-2",
        }
        with lock:
            completion_order.append(command)
        if command == "emit-1":
            emit_1_done.set()
        elif command == "emit-2":
            emit_2_done.set()
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps({"additionalSystemMessages": [messages[command]]}),
            stderr="",
        )

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    dispatcher = _build_parallel_hook_dispatcher(
        workspace_root=tmp_path,
        parallel_enabled=True,
        max_parallel_workers=4,
    )
    result = dispatcher.fire_turn_complete(
        cwd=tmp_path,
        active_workdir_relpath=".",
        payload={"reason": "completed"},
    )

    assert completion_order == ["emit-1", "emit-2", "emit-0"]
    assert result.additional_system_messages == ("msg-0", "msg-1", "msg-2")


def test_blocking_events_remain_serial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    counter_path = tmp_path / "hook_counter.txt"
    # hook2 appends 'x' to counter_path whenever it runs. If PreToolUse went parallel,
    # hook2 would execute alongside hook1 even after hook1 denies, and the file would
    # contain at least one 'x'. If PreToolUse stays serial with short-circuit on deny,
    # hook2 never runs and the file is missing/empty.
    increment_cmd = (
        f'{sys.executable} -c "import pathlib; '
        f"pathlib.Path({str(os.fspath(counter_path))!r}).open('a').write('x')"
        '"'
    )
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "deny-hook",
                                "id": "deny",
                                "priority": 10,
                            },
                            {
                                "type": "command",
                                "command": increment_cmd,
                                "id": "increment",
                                "priority": 0,
                            },
                        ],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    real_run = subprocess.run

    def routed_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command == "deny-hook":
            return subprocess.CompletedProcess(
                args=command,
                returncode=2,
                stdout=json.dumps({"decision": "deny", "reason": "no"}),
                stderr="",
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", routed_run)

    dispatcher = _build_parallel_hook_dispatcher(
        workspace_root=tmp_path,
        parallel_enabled=True,
        max_parallel_workers=4,
    )
    result = dispatcher.fire_pre_tool_use(
        tool_name="echo_tool",
        tool_input={"msg": "hi"},
        cwd=tmp_path,
        active_workdir_relpath=".",
        step=1,
    )

    assert result.blocked is True
    # hook2 must NOT have executed — PreToolUse stayed serial and short-circuited.
    contents = counter_path.read_text(encoding="utf-8") if counter_path.exists() else ""
    assert contents == "", f"increment hook unexpectedly ran; counter file contents: {contents!r}"


def test_ask_decision_pretooluse_propagates_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": "ask-hook"}],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command == "ask-hook"
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(
                {
                    "hookSpecificOutput": {
                        "permissionDecision": "ask",
                        "permissionDecisionReason": "needs review",
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    result = dispatcher.fire_pre_tool_use(
        tool_name="echo_tool",
        tool_input={"msg": "hi"},
        cwd=tmp_path,
        active_workdir_relpath=".",
        step=1,
    )

    assert result.ask_requested is True
    assert result.ask_reason == "needs review"
    assert result.blocked is False
    assert result.permission_decision == "ask"


def test_ask_decision_short_circuits_remaining_pretooluse_hooks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    sentinel_path = tmp_path / "sentinel.txt"
    # hook2 writes "RAN" to the sentinel whenever it executes. If the ask
    # short-circuit works, hook2 never runs and the sentinel stays absent.
    sentinel_cmd = (
        f'{sys.executable} -c "import pathlib; '
        f"pathlib.Path({str(os.fspath(sentinel_path))!r}).write_text('RAN')"
        '"'
    )
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "ask-hook",
                                "id": "ask",
                                "priority": 10,
                            },
                            {
                                "type": "command",
                                "command": sentinel_cmd,
                                "id": "sentinel",
                                "priority": 0,
                            },
                        ],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    real_run = subprocess.run

    def routed_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command == "ask-hook":
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "hookSpecificOutput": {
                            "permissionDecision": "ask",
                            "permissionDecisionReason": "please confirm",
                        }
                    }
                ),
                stderr="",
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", routed_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    result = dispatcher.fire_pre_tool_use(
        tool_name="echo_tool",
        tool_input={"msg": "hi"},
        cwd=tmp_path,
        active_workdir_relpath=".",
        step=1,
    )

    assert result.ask_requested is True
    assert result.ask_reason == "please confirm"
    # sentinel hook must not have executed.
    assert not sentinel_path.exists(), (
        f"sentinel hook unexpectedly ran; sentinel contents: "
        f"{sentinel_path.read_text(encoding='utf-8')!r}"
    )


def test_ask_decision_ignored_on_non_blocking_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_json(
        project_hooks_config_path(tmp_path),
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "echo_tool",
                        "hooks": [{"type": "command", "command": "ask-hook"}],
                    }
                ]
            }
        },
    )
    _trust_project_hooks(tmp_path)

    def fake_run(command: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command == "ask-hook"
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(
                {
                    "hookSpecificOutput": {
                        "permissionDecision": "ask",
                        "permissionDecisionReason": "should be ignored here",
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(hooks_dispatcher_mod.subprocess, "run", fake_run)

    dispatcher = _build_hook_dispatcher(workspace_root=tmp_path)
    result = dispatcher.fire_post_tool_use(
        tool_name="echo_tool",
        tool_input={"msg": "hi"},
        tool_response={"ok": True},
        cwd=tmp_path,
        active_workdir_relpath=".",
        step=1,
    )

    # PostToolUse is non-blocking; ask only wires through on PreToolUse.
    assert result.ask_requested is False
    assert result.ask_reason == ""
