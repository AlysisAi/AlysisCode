from __future__ import annotations

import io
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

import alysis_code.agent_loop as agent_loop_mod
import alysis_code.verify_gate as verify_gate_mod
from alysis_code import cli as cli_mod
from alysis_code.agent.steering import SteerInbox
from alysis_code.agent.turn.core import _can_prelaunch_parallel_subagent_batch
from alysis_code.agent_loop import ToolDef
from alysis_code.cli_impl.tui.app import _poll_selected_subagent
from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.config import AppConfig, clone_cfg
from alysis_code.session_store import SessionStore
from alysis_code.subagents import built_in_subagents
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.surface.types import SubagentStartEvent, ToolEndEvent, ToolStartEvent
from alysis_code.usage_tracker import UsageSummary
from alysis_code.verify_gate import VerifyError


def _store(root: Path, *, session_id: str = "chat-rebuild-test") -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id=session_id,
        cwd=str(root),
        repo_root=str(root),
    )


class _FakeMcpBinding:
    def __init__(self, *, session_mode: str | None = None) -> None:
        self.tool_alias = "mcp__alpha__echo"
        self.tool_name = "echo"
        self.description = "Echo via MCP"
        self.parameters = {"type": "object", "properties": {}, "required": []}
        self.session_mode = session_mode

    def bind_session_mode(self, session_mode: str | None) -> _FakeMcpBinding:
        return _FakeMcpBinding(session_mode=str(session_mode or "").strip().lower() or None)

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool_alias": self.tool_alias,
            "session_mode": self.session_mode,
            "arguments": dict(arguments),
        }


class _DummyMcpManager:
    def __init__(self, *bindings: _FakeMcpBinding) -> None:
        self.tool_bindings = tuple(bindings)


class _FakeShellRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(
        self, *, root: Path, cwd: Path, cmd: str, timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(
            {
                "root": root,
                "cwd": cwd,
                "cmd": cmd,
                "timeout_s": timeout_s,
            }
        )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="runner ok\n",
            stderr="",
        )


def _host_verify_cfg(cfg: AppConfig) -> AppConfig:
    effective = clone_cfg(cfg)
    extra_fields = dict(effective.extra_fields)
    verify_sandbox = dict(extra_fields.get("verify_sandbox") or {})
    verify_sandbox.setdefault("mode", "off")
    extra_fields["verify_sandbox"] = verify_sandbox
    effective.extra_fields = extra_fields
    return effective


def _make_session(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    cfg = _host_verify_cfg(AppConfig(model="test-model", web_search_mode="off"))
    defaults: dict[str, Any] = {
        "cfg": cfg,
        "root": tmp_path,
        "mode": "review",
        "yes": True,
        "max_steps": 4,
        "console": Console(file=io.StringIO(), force_terminal=False),
        "surface": None,
        "store": _store(tmp_path),
        "api_key": "",
        "shell_runner": None,
        "no_log": True,
        "non_interactive": True,
        "one_shot_execution": False,
        "verification_enabled": True,
        "effective_verification_commands": [],
        "authoritative_verification_commands": None,
        "deny_write_prefixes": None,
        "allow_write_globs": None,
        "usage_summary": None,
        "usage_role": "main",
        "model_registry": None,
        "subagents_enabled": False,
        "subagent_depth": 0,
        "subagent_registry": {},
        "session_log_dir_override": None,
        "step_budget_runtime": None,
        "runtime_kind": "interactive_chat",
        "mcp_manager": None,
        "tools": {},
        "tool_list": [],
    }
    defaults.update(overrides)
    defaults["cfg"] = _host_verify_cfg(defaults["cfg"])
    return SimpleNamespace(**defaults)


@pytest.mark.parametrize("mode", ["review", "auto", "fullaccess"])
def test_rebuild_session_tools_preserves_mcp_bindings_in_write_capable_modes(
    tmp_path: Path,
    mode: str,
) -> None:
    session = _make_session(
        tmp_path,
        mcp_manager=_DummyMcpManager(_FakeMcpBinding()),
    )
    try:
        cli_mod._rebuild_session_tools_for_mode(session=session, mode=mode)

        assert "mcp__alpha__echo" in session.tools
    finally:
        session.store.close()


def test_rebuild_with_persona_scope_blocks_unbounded_execution_tools(
    tmp_path: Path,
) -> None:
    from alysis_code.agent.errors import AgentRuntimeError

    session = _make_session(
        tmp_path,
        mcp_manager=_DummyMcpManager(_FakeMcpBinding()),
        persona_allow_write_globs=["**/*.md"],
    )
    try:
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="review")

        assert "mcp__alpha__echo" not in session.tools
        with pytest.raises(AgentRuntimeError, match="persona write scope"):
            session.tools["shell_run"].run({"cmd": "touch source.py"})
        with pytest.raises(AgentRuntimeError, match="persona write scope"):
            session.tools["verify_run"].run({"commands": ["pytest -q"]})
    finally:
        session.store.close()


def test_rebuild_session_tools_preserves_verification_disabled_flag(tmp_path: Path) -> None:
    session = _make_session(tmp_path, verification_enabled=False)
    try:
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="review")

        assert "verify_run" not in session.tools
    finally:
        session.store.close()


def test_rebuild_session_tools_preserves_effective_verification_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def fake_run(cmd, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(verify_gate_mod.subprocess, "run", fake_run)

    class VerificationSurface(NoopSurface):
        def request_approval(self, request):
            return ApprovalDecision(request.kind == "verify_run")

    cfg = AppConfig(model="test-model", web_search_mode="off")
    cfg.verify_commands = ["ruff check ."]
    session = _make_session(
        tmp_path,
        cfg=cfg,
        effective_verification_commands=["pytest -q"],
        surface=VerificationSurface(),
        non_interactive=False,
    )
    try:
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="auto")
        result = session.tools["verify_run"].run({})

        # Verification commands execute as shell strings; the verify pipeline
        # may additionally shell out to git (argv lists) for its internal
        # workspace scans, which must not replace or add verify commands.
        executed_verify_commands = [cmd for cmd in calls if isinstance(cmd, str)]
        internal_calls = [cmd for cmd in calls if not isinstance(cmd, str)]
        assert executed_verify_commands == ["pytest -q"]
        assert all(cmd and cmd[0] == "git" for cmd in internal_calls)
        assert result["commands"] == ["pytest -q"]
    finally:
        session.store.close()


def test_rebuild_session_tools_preserves_authoritative_verification_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_loop_mod,
        "run_task_verification",
        lambda **_kwargs: pytest.fail("verify engine should not run for rejected overrides"),
    )

    cfg = AppConfig(model="test-model", web_search_mode="off")
    cfg.verify_commands = ["ruff check ."]
    session = _make_session(
        tmp_path,
        cfg=cfg,
        authoritative_verification_commands=["pytest -q"],
    )
    try:
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="review")

        with pytest.raises(
            VerifyError,
            match="Managed verification commands are locked to the authoritative Forge command set.",
        ):
            session.tools["verify_run"].run({"commands": ["ruff check ."]})
    finally:
        session.store.close()


def test_rebuild_session_tools_preserves_custom_shell_runner(tmp_path: Path) -> None:
    runner = _FakeShellRunner()
    session = _make_session(tmp_path, shell_runner=runner)
    try:
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="auto")
        result = session.tools["shell_run"].run({"cmd": "echo hi"})

        assert result["stdout"] == "runner ok\n"
        assert runner.calls
        assert runner.calls[0]["cmd"] == "echo hi"
        assert runner.calls[0]["root"] == tmp_path.resolve()
    finally:
        session.store.close()


def test_rebuild_session_tools_preserves_active_workdir_defaults(tmp_path: Path) -> None:
    runner = _FakeShellRunner()
    nested = tmp_path / "packages" / "app"
    nested.mkdir(parents=True, exist_ok=True)
    session = _make_session(
        tmp_path,
        shell_runner=runner,
        focus_dir=tmp_path,
        focus_relpath=".",
        workspace_kind="plain_dir",
        active_workdir_relpath="packages/app",
    )
    try:
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="auto")
        session.tools["shell_run"].run({"cmd": "echo hi"})

        assert runner.calls
        assert runner.calls[0]["cwd"] == nested.resolve()
    finally:
        session.store.close()


def test_rebuild_preserves_coordinator_while_subagent_tools_are_disabled(
    tmp_path: Path,
) -> None:
    session = _make_session(
        tmp_path,
        subagents_enabled=True,
        subagent_registry={},
    )
    coordinator = None
    try:
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="review")
        coordinator = session.subagent_coordinator
        scheduler = session.child_scheduler
        assert coordinator is not None
        assert scheduler is coordinator.scheduler

        session.subagents_enabled = False
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="review")
        assert "subagent_run" not in session.tools
        assert session.subagent_coordinator is coordinator
        assert session.child_scheduler is scheduler
        assert scheduler._closed is False  # noqa: SLF001

        session.subagents_enabled = True
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="review")
        assert "subagent_run" in session.tools
        assert session.subagent_coordinator is coordinator
        assert session.child_scheduler is scheduler
    finally:
        if coordinator is not None:
            coordinator.shutdown(cancel_pending=True)
        session.store.close()


def test_rebuilt_session_reuses_scheduler_and_tracks_deferred_failure_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = built_in_subagents()
    starts: list[SubagentStartEvent] = []
    surface = TuiSurface(
        TuiTranscript(),
        on_subagent_run_started=starts.append,
    )
    child_sequence = 0
    debugger_runs = 0
    sequence_lock = threading.Lock()

    class _ChildSession:
        def __init__(self, *, child_surface: Any, role: str, session_id: str) -> None:
            definition = registry[role]
            self.surface = child_surface
            self.store = SessionStore(
                enabled=False,
                sessions_dir=tmp_path / "child-sessions",
                session_id=session_id,
                cwd=str(tmp_path),
                repo_root=str(tmp_path),
            )
            self.tools = {
                name: ToolDef(
                    name=name,
                    description=f"runtime {name}",
                    parameters={"type": "object", "properties": {}, "required": []},
                    run=lambda _args: {"ok": True},
                )
                for name in definition.allow_tools
            }
            self.tool_list = [tool.as_openai_tool() for tool in self.tools.values()]
            self.messages: list[dict[str, Any]] = []
            self.usage_summary = UsageSummary()
            self.exit_code = 0
            if role == "debugger":
                nonlocal debugger_runs
                with sequence_lock:
                    debugger_runs += 1
                    if debugger_runs == 1:
                        self.exit_code = 1

        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            _ = cancellation_token
            call_id = f"call-{self.store.session_id}"
            self.store.append(
                "tool_call",
                {"name": "fs_read", "arguments": {"path": "README.md"}},
            )
            self.surface.on_tool_start(
                ToolStartEvent(
                    tool_call_id=call_id,
                    name="fs_read",
                    args={"path": "README.md"},
                    step=1,
                )
            )
            self.surface.on_tool_end(
                ToolEndEvent(
                    tool_call_id=call_id,
                    name="fs_read",
                    status="done",
                    elapsed_ms=1,
                    meta={},
                )
            )
            report = f"runtime report for {task}"
            self.messages.append({"role": "assistant", "content": report})
            self.store.append("assistant_message", {"content": report})
            self.store.append("final", {"content": report})
            return self.exit_code

        def close(self) -> None:
            self.store.close()

    def _create_child_session(**kwargs: Any) -> _ChildSession:
        nonlocal child_sequence
        role = str(kwargs["usage_role"]).rsplit(":", 1)[-1]
        with sequence_lock:
            child_sequence += 1
            session_id = f"runtime-child-{child_sequence}"
        return _ChildSession(
            child_surface=kwargs["surface"],
            role=role,
            session_id=session_id,
        )

    monkeypatch.setattr(agent_loop_mod, "create_session", _create_child_session)
    cfg = AppConfig(model="test-model", web_search_mode="off")
    cfg.subagent_orchestration.parallel_nonwriting_shared = False
    session = _make_session(
        tmp_path,
        cfg=cfg,
        surface=surface,
        mode="auto",
        api_key="test-key",
        subagents_enabled=True,
        subagent_registry=registry,
    )
    session.steer_inbox = SteerInbox()
    schedulers: list[Any] = []
    try:
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="auto")
        initial_launcher = session.tools["subagent_run"].run.__self__
        initial_scheduler = initial_launcher.child_scheduler
        assert initial_scheduler is not None
        session.child_scheduler = initial_scheduler
        schedulers.append(initial_scheduler)
        lifecycle_events: list[dict[str, Any]] = []
        lifecycle_listener = lifecycle_events.append
        initial_scheduler.set_lifecycle_listener(lifecycle_listener)

        cli_mod._rebuild_session_tools_for_mode(session=session, mode="auto")
        rebuilt_launcher = session.tools["subagent_run"].run.__self__
        rebuilt_scheduler = rebuilt_launcher.child_scheduler
        assert rebuilt_scheduler is not None
        assert rebuilt_scheduler is initial_scheduler
        assert session.child_scheduler is rebuilt_scheduler
        assert rebuilt_scheduler.lifecycle_listener is lifecycle_listener
        assert rebuilt_scheduler.parent_steer_inbox is session.steer_inbox
        schedulers.append(rebuilt_scheduler)

        calls = [
            SimpleNamespace(
                id="call-explorer",
                name="subagent_run",
                arguments={"name": "explorer", "task": "initial explorer"},
            ),
            SimpleNamespace(
                id="call-reviewer",
                name="subagent_run",
                arguments={"name": "code-reviewer", "task": "initial review"},
            ),
            SimpleNamespace(
                id="call-debugger",
                name="subagent_run",
                arguments={"name": "debugger", "task": "deferred debugger"},
            ),
        ]
        partition = _can_prelaunch_parallel_subagent_batch(
            tool_calls=calls,
            turn_tools=session.tools,
            subagent_registry=registry,
            parent_mode="auto",
            failed_tool_call_counts={},
            hook_dispatcher=None,
            subagent_policy_reason="repo_execution_turn",
            deadline_can_start=True,
            parallel_nonwriting_shared=False,
        )
        assert [call.id for call in partition.eligible] == [
            "call-explorer",
            "call-reviewer",
        ]
        assert [call.id for call in partition.deferred] == ["call-debugger"]

        initial_run_ids = session.child_scheduler.submit_parallel_batch(
            [dict(call.arguments) for call in partition.eligible]
        )
        initial_results = session.child_scheduler.collect(
            run_id=initial_run_ids,
            timeout_s=None,
        )
        assert not initial_results["pending_run_ids"]

        deferred_args = dict(partition.deferred[0].arguments)
        failed_result = session.tools["subagent_run"].run(deferred_args)
        retried_result = session.tools["subagent_run"].run(deferred_args)
        assert failed_result.get("error")
        assert "error" not in retried_result

        deferred_starts = [event for event in starts if event.name == "debugger"]
        assert len(deferred_starts) == 2
        retried_run_id = str(deferred_starts[-1].subagent_run_id or "")
        assert retried_run_id
        panel_state: dict[str, Any] = {
            "selected_run_id": retried_run_id,
            "run_order": [retried_run_id],
            "cursors": {retried_run_id: 0},
            "entries": {retried_run_id: []},
            "statuses": {},
            "lifecycles": {},
            "poll_failures": {},
            "last_poll": None,
        }
        failures: list[tuple[str, str]] = []

        assert _poll_selected_subagent(
            panel_state,
            session.child_scheduler,
            now=1.0,
            on_failure=lambda run_id, condition: failures.append((run_id, condition)),
        )

        assert failures == []
        assert panel_state["poll_failures"] == {}
        assert panel_state["statuses"][retried_run_id]["state"] == "joined"
        assert panel_state["statuses"][retried_run_id]["steps_completed"] == 1
        assert panel_state["entries"][retried_run_id][-1] == {
            "kind": "assistant",
            "summary": "runtime report for deferred debugger",
        }
        assert any(
            event["run_id"] == retried_run_id
            and event["state"] == "joined"
            and event["collected"] is True
            for event in lifecycle_events
        )
    finally:
        for scheduler in dict.fromkeys(schedulers):
            scheduler.shutdown(cancel_pending=True)
        session.store.close()


def test_runtime_tui_preserves_panel_when_tools_are_rebuilt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui.app import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    registry = built_in_subagents()
    child_started = [threading.Event(), threading.Event()]
    release_child = [threading.Event(), threading.Event()]
    child_sequence = 0
    runtime: dict[str, Any] = {}

    class _ChildSession:
        def __init__(self, *, child_surface: Any, index: int) -> None:
            self.surface = child_surface
            self.store = SessionStore(
                enabled=False,
                sessions_dir=tmp_path / "runtime-tui-child-sessions",
                session_id=f"runtime-tui-child-{index}",
                cwd=str(tmp_path),
                repo_root=str(tmp_path),
            )
            self.tools = {
                "fs_read": ToolDef(
                    name="fs_read",
                    description="runtime read",
                    parameters={"type": "object", "properties": {}, "required": []},
                    run=lambda _args: {"ok": True},
                )
            }
            self.tool_list = [tool.as_openai_tool() for tool in self.tools.values()]
            self.messages: list[dict[str, Any]] = []
            self.usage_summary = UsageSummary()
            self.index = index

        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            child_started[self.index].set()
            assert release_child[self.index].wait(timeout=3.0)
            report = f"runtime panel report for {task}"
            self.messages.append({"role": "assistant", "content": report})
            self.store.append("assistant_message", {"content": report})
            self.store.append("final", {"content": report})
            return 0

        def close(self) -> None:
            self.store.close()

    def _create_child_session(**kwargs: Any) -> _ChildSession:
        nonlocal child_sequence
        index = child_sequence
        child_sequence += 1
        return _ChildSession(child_surface=kwargs["surface"], index=index)

    monkeypatch.setattr(agent_loop_mod, "create_session", _create_child_session)
    original_application_run = Application.run

    def _capture_application(app: Application, *args: Any, **kwargs: Any) -> Any:
        runtime["app"] = app
        return original_application_run(app, *args, **kwargs)

    monkeypatch.setattr(Application, "run", _capture_application)

    def _build(surface: TuiSurface) -> Any:
        session = _make_session(
            tmp_path,
            cfg=AppConfig(model="test-model", web_search_mode="off"),
            surface=surface,
            mode="auto",
            api_key="test-key",
            subagents_enabled=True,
            subagent_registry=registry,
        )
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="auto")
        runtime["session"] = session
        runtime["schedulers"] = [session.child_scheduler]

        def _run_turn(_text: str, *, cancellation_token: Any | None = None) -> int:
            first = session.tools["subagent_spawn"].run(
                {"name": "explorer", "task": "before rebuild"}
            )
            runtime["first_run_id"] = first["run_id"]
            assert child_started[0].wait(timeout=2.0)

            cli_mod._rebuild_session_tools_for_mode(session=session, mode="auto")
            runtime["schedulers"].append(session.child_scheduler)
            runtime["rebuild_done"].set()
            assert release_child[0].wait(timeout=3.0)
            session.child_scheduler.collect(run_id=first["run_id"], timeout_s=2.0)

            second = session.tools["subagent_spawn"].run(
                {"name": "explorer", "task": "after rebuild"}
            )
            runtime["second_run_id"] = second["run_id"]
            assert child_started[1].wait(timeout=2.0)
            assert release_child[1].wait(timeout=3.0)
            session.child_scheduler.collect(run_id=second["run_id"], timeout_s=2.0)
            runtime["turn_done"].set()
            return 0

        session.run_turn = _run_turn
        return session

    runtime["rebuild_done"] = threading.Event()
    runtime["turn_done"] = threading.Event()
    with create_pipe_input() as pipe:

        def _send_keys() -> None:
            pipe.send_text("go\r")
            assert child_started[0].wait(timeout=2.0)
            pipe.send_text("\x1b[1;3C")
            time.sleep(0.05)
            panel = runtime["app"].layout.container.content.children[2]
            runtime["selected_before"] = panel.filter()
            assert runtime["rebuild_done"].wait(timeout=3.0)
            time.sleep(0.05)
            runtime["selected_after"] = panel.filter()
            release_child[0].set()
            assert child_started[1].wait(timeout=2.0)
            pipe.send_text("\x1b[1;3C")
            time.sleep(0.05)
            runtime["selected_new"] = panel.filter()
            release_child[1].set()
            assert runtime["turn_done"].wait(timeout=3.0)
            time.sleep(0.05)
            pipe.send_text("/exit\r")

        sender = threading.Thread(target=_send_keys)
        sender.start()
        _result, transcript = run_tui(
            TuiState(),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=_build,
        )
        sender.join(timeout=3.0)

    try:
        assert runtime["selected_before"] is True
        assert runtime["selected_after"] is True
        assert runtime["selected_new"] is True
        assert transcript.count(("info", "subagent history cleared: settings applied")) == 0
        assert not any("Subagent panel refresh failed" in text for _role, text in transcript)

        second_run_id = runtime["second_run_id"]
        panel_state: dict[str, Any] = {
            "selected_run_id": second_run_id,
            "run_order": [second_run_id],
            "cursors": {second_run_id: 0},
            "entries": {second_run_id: []},
            "statuses": {},
            "lifecycles": {},
            "poll_failures": {},
            "last_poll": None,
        }
        assert _poll_selected_subagent(
            panel_state,
            runtime["session"].child_scheduler,
            now=1.0,
        )
        assert panel_state["poll_failures"] == {}
        assert panel_state["statuses"][second_run_id]["state"] == "joined"
    finally:
        for scheduler in dict.fromkeys(runtime.get("schedulers", [])):
            scheduler.shutdown(cancel_pending=True)
        runtime["session"].store.close()
