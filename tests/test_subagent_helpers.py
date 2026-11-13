from __future__ import annotations

import os
import subprocess
import threading
import time
import types
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from alysis_code import agent_loop
from alysis_code.agent.subagent_execution import (
    _subagent_helper_catalog_message,
)
from alysis_code.agent.tools_assembly import ToolDef, build_tools
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import DeadlineOperation, ExecutionDeadline
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import SessionStore
from alysis_code.subagents import SubagentDefinition, built_in_subagents
from alysis_code.usage_tracker import UsageRecord, UsageSummary


class _HelperSession:
    def __init__(self, *, session_id: str, usage_tokens: int = 5) -> None:
        self.store = types.SimpleNamespace(
            session_id=session_id,
            events_snapshot=lambda: [{"type": "final", "payload": {"content": "Helper report."}}],
        )
        self.tools = {
            "fs_read": ToolDef(
                name="fs_read",
                description="read",
                parameters={"type": "object", "properties": {}},
                run=lambda _args: {"ok": True},
            ),
            "shell_run": ToolDef(
                name="shell_run",
                description="diagnose",
                parameters={"type": "object", "properties": {}},
                run=lambda _args: {"ok": True},
            ),
            "verify_run": ToolDef(
                name="verify_run",
                description="verify",
                parameters={"type": "object", "properties": {}},
                run=lambda _args: {"ok": True},
            ),
        }
        self.tool_list = [tool.as_openai_tool() for tool in self.tools.values()]
        self.messages = [{"role": "assistant", "content": "Helper report."}]
        self.usage_summary = UsageSummary()
        self.usage_summary.add_record(
            UsageRecord(
                timestamp="2026-08-19T00:00:00+00:00",
                role="child:helper",
                requested_model="test-model",
                response_model="test-model",
                prompt_tokens=usage_tokens - 2,
                completion_tokens=2,
                total_tokens=usage_tokens,
                input_cost_per_token=None,
                output_cost_per_token=None,
                cost_usd=None,
                usage_source="api",
            )
        )
        self.workspace_touched_paths: set[str] = set()

    def run_turn(self, _task: str, *, cancellation_token: Any | None = None) -> int:
        _ = cancellation_token
        return 0

    def close(self) -> None:
        return None


def _usage_record(*, role: str, total_tokens: int) -> UsageRecord:
    return UsageRecord(
        timestamp="2026-08-19T00:00:00+00:00",
        role=role,
        requested_model="test-model",
        response_model="test-model",
        prompt_tokens=total_tokens - 2,
        completion_tokens=2,
        total_tokens=total_tokens,
        input_cost_per_token=None,
        output_cost_per_token=None,
        cost_usd=None,
        usage_source="api",
    )


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "app.txt").write_text("parent\n", encoding="utf-8")
    _git(root, "add", "app.txt")
    _git(
        root,
        "-c",
        "user.name=Alysis Code Tests",
        "-c",
        "user.email=tests@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-gpg-sign",
        "--no-verify",
        "-qm",
        "initial",
    )
    return root


class _ReadingHelperSession(_HelperSession):
    def __init__(self, *, root: Path, observed: list[str]) -> None:
        super().__init__(session_id=f"helper-{id(self)}", usage_tokens=5)
        self.root = root
        self.observed = observed

    def run_turn(self, _task: str, *, cancellation_token: Any | None = None) -> int:
        _ = cancellation_token
        content = (self.root / "app.txt").read_text(encoding="utf-8")
        self.observed.append(content)
        final = f"helper saw {content.strip()}"
        self.messages = [{"role": "assistant", "content": final}]
        self.store.events_snapshot = lambda: [{"type": "final", "payload": {"content": final}}]
        return 0


class _WriterSession:
    def __init__(
        self,
        *,
        root: Path,
        tools: dict[str, ToolDef],
        store: SessionStore,
        usage_summary: UsageSummary,
        edit_before_helper: bool,
    ) -> None:
        self.root = root
        self.tools = tools
        self.tool_list = [tool.as_openai_tool() for tool in tools.values()]
        self.store = store
        self.usage_summary = usage_summary
        self.edit_before_helper = edit_before_helper
        self.messages: list[dict[str, Any]] = []
        self.workspace_touched_paths: set[str] = set()
        self.helper_result: dict[str, Any] | None = None

    def run_turn(self, _task: str, *, cancellation_token: Any | None = None) -> int:
        _ = cancellation_token
        if self.edit_before_helper:
            (self.root / "app.txt").write_text("child\n", encoding="utf-8")
            self.workspace_touched_paths.add("app.txt")
        self.helper_result = self.tools["subagent_run"].run(
            {"name": "explorer", "task": "Read app.txt and report its exact content."}
        )
        final = f"writer received: {self.helper_result.get('result', '')}"
        self.messages.append({"role": "assistant", "content": final})
        self.store.append("final", {"content": final})
        return 0

    def close(self) -> None:
        self.store.close()


def _child_tools(
    *,
    tmp_path: Path,
    mode: str = "auto",
    depth: int = 1,
    helpers_enabled: bool = True,
    registry: dict[str, SubagentDefinition] | None = None,
    cfg: AppConfig | None = None,
    create_session_factory: Callable[..., Any] | None = None,
    execution_deadline: ExecutionDeadline | None = None,
    usage_summary: UsageSummary | None = None,
) -> tuple[dict[str, ToolDef], SessionStore]:
    store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path / "sessions",
        session_id=f"child-{mode}-{depth}",
        cwd=os.fspath(tmp_path),
        repo_root=os.fspath(tmp_path),
    )
    tools = build_tools(
        root=tmp_path,
        console=None,
        store=store,
        mode=mode,
        yes=True,
        cfg=cfg or AppConfig(model="test-model", web_search_mode="off"),
        api_key="test-key",
        max_steps=8,
        non_interactive=True,
        subagents_enabled=False,
        helper_subagents_enabled=helpers_enabled,
        subagent_depth=depth,
        subagent_registry=registry or built_in_subagents(),
        runtime_kind=RuntimeKind.SUBAGENT,
        create_session_factory=create_session_factory,
        execution_deadline=execution_deadline,
        usage_summary=usage_summary,
    )
    return tools, store


def test_write_capable_depth_one_child_gets_restricted_helper_tool(
    tmp_path: Path,
) -> None:
    registry = built_in_subagents()
    registry.update(
        {
            "custom-reader": SubagentDefinition(
                name="custom-reader",
                description="custom readonly helper",
                system_prompt="Read only.",
                mode="readonly",
                allow_workspace_writes=False,
            ),
            "manual-reader": SubagentDefinition(
                name="manual-reader",
                description="manual readonly role",
                system_prompt="Read only.",
                mode="readonly",
                allow_workspace_writes=False,
                routing_visibility="manual",
            ),
            "custom-writer": SubagentDefinition(
                name="custom-writer",
                description="custom writer",
                system_prompt="Write.",
                mode="auto",
                allow_workspace_writes=True,
            ),
        }
    )
    tools, store = _child_tools(tmp_path=tmp_path, registry=registry)
    try:
        helper_tool = tools["subagent_run"]
        schema = helper_tool.as_openai_tool()["function"]["parameters"]

        assert set(schema["properties"]) == {"name", "task", "max_steps"}
        assert schema["properties"]["name"]["enum"] == [
            "code-reviewer",
            "custom-reader",
            "debugger",
            "explorer",
            "verifier",
        ]
        assert not {
            "subagent_spawn",
            "subagent_status",
            "subagent_wait",
            "subagent_cancel",
            "subagent_apply",
            "subagent_discard",
        }.intersection(tools)
    finally:
        store.close()


def test_readonly_child_gets_no_helper_tool(tmp_path: Path) -> None:
    tools, store = _child_tools(tmp_path=tmp_path, mode="readonly")
    try:
        assert "subagent_run" not in tools
    finally:
        store.close()


def test_depth_two_session_gets_no_helper_tool(tmp_path: Path) -> None:
    tools, store = _child_tools(tmp_path=tmp_path, depth=2)
    try:
        assert "subagent_run" not in tools
    finally:
        store.close()


def test_helpers_disabled_in_config_removes_child_tool(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", web_search_mode="off")
    cfg.subagent_orchestration.helpers_enabled = False
    tools, store = _child_tools(tmp_path=tmp_path, cfg=cfg)
    try:
        assert "subagent_run" not in tools
    finally:
        store.close()


def test_helper_budget_step_and_deadline_bounds_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AppConfig(model="test-model", web_search_mode="off")
    cfg.subagent_orchestration.helper_max_total_per_child = 2
    cfg.subagent_orchestration.helper_max_steps = 3
    cfg.subagent_orchestration.helper_timeout_s = 120.0
    child_deadline = ExecutionDeadline.from_duration(8.0)
    captured: list[dict[str, Any]] = []

    def create_helper(**kwargs: Any) -> _HelperSession:
        captured.append(kwargs)
        return _HelperSession(session_id=f"helper-{len(captured)}")

    monkeypatch.setattr(agent_loop, "create_session", create_helper)
    child_usage = UsageSummary()
    tools, store = _child_tools(
        tmp_path=tmp_path,
        cfg=cfg,
        create_session_factory=create_helper,
        execution_deadline=child_deadline,
        usage_summary=child_usage,
    )
    try:
        helper = tools["subagent_run"]
        first = helper.run({"name": "explorer", "task": "Inspect alpha.", "max_steps": 99})
        second = helper.run({"name": "debugger", "task": "Inspect beta."})
        refused = helper.run({"name": "verifier", "task": "Inspect gamma."})

        assert first.get("result") == "Helper report.", first
        assert second.get("result") == "Helper report.", second
        assert refused["error"] == "helper budget exhausted: 2 of 2 used"
        assert refused["error_code"] == "helper_budget_exhausted"
        assert len(captured) == 2
        assert [kwargs["max_steps"] for kwargs in captured] == [3, 3]
        assert all(
            0 < kwargs["execution_deadline"].remaining_seconds() <= 8.0 for kwargs in captured
        )
        assert refused["helper_runs"]["count"] == 2
        assert refused["helper_runs"]["names"] == ["explorer", "debugger"]
        assert refused["helper_runs"]["steps"] == 0
        expected_usage = {
            "prompt_tokens": 6,
            "completion_tokens": 4,
            "total_tokens": 10,
            "api_usage_calls": 2,
            "estimate_usage_calls": 0,
        }
        assert all(
            refused["helper_runs"]["usage_totals"][key] == value
            for key, value in expected_usage.items()
        )
        assert child_usage.totals()["total_tokens"] == 10
    finally:
        store.close()


def test_default_helper_has_no_step_or_wall_clock_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AppConfig(model="test-model", web_search_mode="off")
    captured: dict[str, Any] = {}

    def create_helper(**kwargs: Any) -> _HelperSession:
        captured.update(kwargs)
        return _HelperSession(session_id="helper-unlimited")

    monkeypatch.setattr(agent_loop, "create_session", create_helper)
    tools, store = _child_tools(
        tmp_path=tmp_path,
        cfg=cfg,
        create_session_factory=create_helper,
    )
    try:
        result = tools["subagent_run"].run({"name": "explorer", "task": "Inspect the repository."})
    finally:
        store.close()

    assert result.get("result") == "Helper report.", result
    assert captured["max_steps"] is None
    assert captured["execution_deadline"].enabled is False
    assert captured["execution_deadline"].remaining_seconds() is None
    assert cfg.subagent_orchestration.helper_timeout_s is None
    assert cfg.subagent_orchestration.helper_max_steps is None


def test_explicit_helper_timeout_applies_without_parent_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AppConfig(
        model="test-model",
        web_search_mode="off",
        subagent_orchestration={"helper_timeout_s": 30.0},
    )
    captured: dict[str, Any] = {}

    def create_helper(**kwargs: Any) -> _HelperSession:
        captured.update(kwargs)
        return _HelperSession(session_id="helper-with-timeout")

    monkeypatch.setattr(agent_loop, "create_session", create_helper)
    tools, store = _child_tools(
        tmp_path=tmp_path,
        cfg=cfg,
        create_session_factory=create_helper,
    )
    try:
        result = tools["subagent_run"].run({"name": "explorer", "task": "Inspect the repository."})
    finally:
        store.close()

    assert result.get("result") == "Helper report.", result
    child_deadline = captured["execution_deadline"]
    assert child_deadline.enabled is True
    assert child_deadline.configured_duration_seconds == 30.0
    remaining_seconds = child_deadline.remaining_seconds()
    assert remaining_seconds is not None
    assert 0 < remaining_seconds <= 30.0


def test_nested_helper_refuses_when_two_observed_calls_do_not_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    child_deadline = ExecutionDeadline.from_duration(300.0, clock=lambda: now)
    for duration in (80.0, 90.0, 200.0):
        child_deadline.observe_duration(DeadlineOperation.MAIN_LLM, duration)
    now = 129.0
    cfg = AppConfig(model="test-model", web_search_mode="off")
    cfg.subagent_orchestration.helper_timeout_s = 300.0

    def create_helper(**_kwargs: Any) -> _HelperSession:
        raise AssertionError("deadline-blocked helper must not launch")

    monkeypatch.setattr(agent_loop, "create_session", create_helper)
    tools, store = _child_tools(
        tmp_path=tmp_path,
        cfg=cfg,
        create_session_factory=create_helper,
        execution_deadline=child_deadline,
    )
    try:
        result = tools["subagent_run"].run({"name": "explorer", "task": "Inspect the repository."})
    finally:
        store.close()

    assert result["error_code"] == "subagent_deadline_prevented_launch"
    assert result["deadline_prevented_launch"] is True
    assert result["parent_call_estimate_seconds"] == 90.0
    assert result["nested_minimum_remaining_seconds"] == 180.0
    assert result["remaining_seconds"] == 171.0
    assert result["parent_call_estimate"]["estimate_strategy"] == (
        "max_of_recent_operation_medians"
    )
    assert result["parent_call_estimate"]["operations"]["main_llm"]["estimate_window_seconds"] == [
        80.0,
        90.0,
        200.0,
    ]


@pytest.mark.parametrize(
    "values",
    [
        {"helper_max_total_per_child": -1},
        {"helper_timeout_s": 0},
        {"helper_timeout_s": float("inf")},
        {"helper_max_steps": 0},
    ],
)
def test_helper_config_rejects_invalid_bounds(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        AppConfig(subagent_orchestration=values)


def test_writer_prompts_and_child_catalog_explain_advisory_helpers() -> None:
    registry = built_in_subagents()

    for writer in ("general", "frontend-engineer"):
        prompt = registry[writer].system_prompt
        assert "bounded read-only helpers" in prompt
        assert "their reports are advisory" in prompt
        assert "do not transfer your responsibility for assessing the actual candidate" in prompt
        assert "Reuse attributable, still-current helper checks" in prompt
        assert "without claiming you ran them yourself" in prompt

    catalog = _subagent_helper_catalog_message(
        registry=registry,
        names=("explorer", "code-reviewer", "verifier", "debugger"),
        remaining=2,
    )
    lines = catalog.splitlines()
    assert len(lines) == 3
    assert lines[0] == "Helper subagents (remaining budget: 2):"
    assert all(name in lines[1] for name in ("explorer", "code-reviewer", "verifier", "debugger"))
    assert "advisory" in lines[2]


@pytest.mark.parametrize(
    ("workspace_view", "edit_before_helper", "expected_content"),
    [
        ("isolated", True, "child\n"),
        ("shared", False, "parent\n"),
    ],
)
def test_writer_helper_uses_child_workspace_and_rolls_up_usage_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace_view: str,
    edit_before_helper: bool,
    expected_content: str,
) -> None:
    root = _repo(tmp_path)
    cfg = AppConfig(model="test-model", web_search_mode="off")
    registry = built_in_subagents()
    observed: list[str] = []
    writers: list[_WriterSession] = []
    child_usage_summaries: list[UsageSummary] = []

    def create_nested_session(**kwargs: Any) -> Any:
        child_root = Path(kwargs["root"])
        depth = int(kwargs["subagent_depth"])
        if depth == 2:
            assert kwargs["subagents_enabled"] is False
            assert kwargs["helper_subagents_enabled"] is False
            return _ReadingHelperSession(root=child_root, observed=observed)

        assert depth == 1
        child_usage = UsageSummary()
        child_usage.add_record(_usage_record(role="writer", total_tokens=10))
        child_usage_summaries.append(child_usage)
        child_store = SessionStore(
            enabled=False,
            sessions_dir=tmp_path / "child-sessions",
            session_id=f"writer-{len(writers) + 1}",
            cwd=os.fspath(child_root),
            repo_root=os.fspath(child_root),
        )
        child_tools = build_tools(
            root=child_root,
            console=None,
            surface=kwargs["surface"],
            store=child_store,
            mode=kwargs["mode"],
            yes=kwargs["yes"],
            cfg=kwargs["cfg"],
            api_key="test-key",
            max_steps=kwargs["max_steps"],
            usage_role=kwargs["usage_role"],
            usage_summary=child_usage,
            non_interactive=kwargs["non_interactive"],
            subagents_enabled=False,
            helper_subagents_enabled=kwargs["helper_subagents_enabled"],
            subagent_depth=depth,
            subagent_registry=registry,
            runtime_kind=RuntimeKind.SUBAGENT,
            create_session_factory=create_nested_session,
            execution_deadline=kwargs["execution_deadline"],
        )
        writer = _WriterSession(
            root=child_root,
            tools=child_tools,
            store=child_store,
            usage_summary=child_usage,
            edit_before_helper=edit_before_helper,
        )
        writers.append(writer)
        return writer

    monkeypatch.setattr(agent_loop, "create_session", create_nested_session)
    parent_store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path / "parent-sessions",
        session_id="parent",
        cwd=os.fspath(root),
        repo_root=os.fspath(root),
    )
    parent_usage = UsageSummary()
    tools = build_tools(
        root=root,
        console=None,
        store=parent_store,
        mode="auto",
        yes=True,
        cfg=cfg,
        api_key="test-key",
        max_steps=8,
        usage_summary=parent_usage,
        non_interactive=True,
        subagents_enabled=True,
        subagent_registry=registry,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        create_session_factory=create_nested_session,
    )
    launcher = tools["subagent_run"].run.__self__
    try:
        result = tools["subagent_run"].run(
            {
                "name": "general",
                "task": "Inspect app.txt with a helper.",
                "workspace_view": workspace_view,
            }
        )

        assert observed == [expected_content]
        assert writers[0].helper_result is not None
        assert writers[0].helper_result["result"] == (f"helper saw {expected_content.strip()}")
        assert result["helper_runs"]["count"] == 1
        assert result["helper_runs"]["names"] == ["explorer"]
        assert result["helper_runs"]["usage_totals"]["total_tokens"] == 5
        assert result["usage"]["total_tokens"] == 15
        assert child_usage_summaries[0].totals()["total_tokens"] == 15
        assert parent_usage.totals()["total_tokens"] == 15
        assert len(parent_usage.records()) == 2
        catalog = next(
            str(message["content"])
            for message in writers[0].messages
            if str(message.get("content") or "").startswith("Helper subagents (")
        )
        assert len(catalog.splitlines()) == 3
        child_trace = []
        for event in writers[0].store.events_snapshot():
            event_type = str(event.get("type") or "")
            payload = event.get("payload")
            if event_type == "subagent_state" and isinstance(payload, dict):
                child_trace.append(f"subagent_state:{payload.get('state')}")
            elif event_type in {
                "subagent_start",
                "subagent_tool_catalog",
                "subagent_end",
                "final",
            }:
                child_trace.append(event_type)
        assert child_trace == [
            "subagent_state:spawned",
            "subagent_state:running",
            "subagent_start",
            "subagent_tool_catalog",
            "subagent_end",
            "subagent_state:joined",
            "final",
        ]
        end_events = [
            event["payload"]
            for event in parent_store.events_snapshot()
            if event.get("type") == "subagent_end"
        ]
        assert end_events[-1]["helper_runs"] == result["helper_runs"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "parent\n"

        if workspace_view == "isolated":
            assert result["patch_summary"]["files"] == ["app.txt"]
            tools["subagent_discard"].run({"run_id": result["run_id"]})
    finally:
        if launcher.child_scheduler is not None:
            launcher.child_scheduler.shutdown(cancel_pending=True)
        parent_store.close()


def test_helper_calls_are_serialized_per_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def create_helper(**_kwargs: Any) -> _HelperSession:
        session = _HelperSession(session_id=f"helper-{time.monotonic_ns()}")

        def run_turn(
            _self: _HelperSession,
            _task: str,
            *,
            cancellation_token: Any | None = None,
        ) -> int:
            nonlocal active, max_active
            _ = cancellation_token
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return 0
            finally:
                with state_lock:
                    active -= 1

        session.run_turn = types.MethodType(run_turn, session)
        return session

    monkeypatch.setattr(agent_loop, "create_session", create_helper)
    tools, store = _child_tools(
        tmp_path=tmp_path,
        create_session_factory=create_helper,
    )
    helper = tools["subagent_run"]

    def call(name: str) -> dict[str, Any]:
        return helper.run({"name": name, "task": f"Consult {name}."})

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(call, ("explorer", "debugger")))

        assert [result["result"] for result in results] == [
            "Helper report.",
            "Helper report.",
        ]
        assert max_active == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "runtime_kind",
    [RuntimeKind.FORGE_EXEC],
)
def test_forge_workers_do_not_enable_child_helpers(
    tmp_path: Path,
    runtime_kind: RuntimeKind,
) -> None:
    store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path / "runtime-sessions",
        session_id=f"runtime-{runtime_kind.value}",
        cwd=os.fspath(tmp_path),
        repo_root=os.fspath(tmp_path),
    )
    tools = build_tools(
        root=tmp_path,
        console=None,
        store=store,
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        api_key="test-key",
        max_steps=8,
        non_interactive=True,
        subagents_enabled=True,
        subagent_registry=built_in_subagents(),
        runtime_kind=runtime_kind,
    )
    launcher = tools["subagent_run"].run.__self__
    try:
        assert launcher.helpers_enabled_for_children is False
    finally:
        if launcher.child_scheduler is not None:
            launcher.child_scheduler.shutdown(cancel_pending=True)
        store.close()
