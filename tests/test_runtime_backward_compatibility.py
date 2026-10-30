from __future__ import annotations

from pathlib import Path
from typing import Any

from alysis_code import agent_loop
from alysis_code.agent import _patchable
from alysis_code.agent_loop import (
    ToolDef,
    TurnExecutionState,
    build_tools,
    create_session,
)
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.subagents import SubagentDefinition
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult


class _Store:
    enabled = False
    session_id = "compat-session"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "session.jsonl"
        self.session_artifact_root = root / "artifacts"
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))

    def events_since(self, cursor: int):
        return [
            {"type": kind, "payload": payload} for kind, payload in self.events[max(0, cursor) :]
        ], len(self.events)

    def configure_web_fetch_trusted_domains(self, domains: Any) -> tuple[str, ...]:
        return tuple(domains or ())

    def runtime_artifact_path(self, category: str, filename: str) -> Path:
        path = self.session_artifact_root / category / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class _CompatSurface(NoopSurface):
    def request_approval(self, request):
        return ApprovalDecision(request.kind in {"shell_run", "verify_run"})


def _build_compat_tools(
    tmp_path: Path,
    *,
    cfg: AppConfig | None = None,
    subagents_enabled: bool = False,
    subagent_registry: dict[str, SubagentDefinition] | None = None,
) -> dict[str, ToolDef]:
    return build_tools(
        root=tmp_path,
        console=None,
        surface=_CompatSurface(),
        store=_Store(tmp_path),
        mode="auto",
        yes=True,
        cfg=cfg or AppConfig(model="test-model"),
        api_key="test-key",
        max_steps=3,
        subagents_enabled=subagents_enabled,
        subagent_registry=subagent_registry,
    )


def test_patchable_resolves_monkeypatches_from_agent_loop(monkeypatch) -> None:
    sentinel = object()
    fallback = object()

    monkeypatch.setattr(agent_loop, "fs_read", sentinel)

    assert _patchable("fs_read", fallback) is sentinel


def test_run_agent_uses_patchable_create_session_and_returns_int(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    class _FakeSession:
        def run_turn(  # type: ignore[no-untyped-def]
            self,
            instruction: str,
            *,
            image_paths=None,
            cancellation_token=None,
        ) -> int:
            calls["instruction"] = instruction
            calls["image_paths"] = image_paths
            calls["cancellation_token"] = cancellation_token
            return 7

        def close(self) -> None:
            calls["closed"] = True

    def fake_create_session(**kwargs: Any) -> _FakeSession:
        calls["create_session_kwargs"] = kwargs
        return _FakeSession()

    monkeypatch.setattr(agent_loop, "create_session", fake_create_session)

    code = agent_loop.run_agent(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        instruction="Do the task.",
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
    )

    assert isinstance(code, int)
    assert code == 7
    assert calls["instruction"] == "Do the task."
    assert calls["cancellation_token"] is None
    assert calls["closed"] is True
    assert calls["create_session_kwargs"]["crash_diagnostic_log_path"] is None


def test_run_agent_omits_empty_ephemeral_messages_for_legacy_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    class _LegacySession:
        def run_turn(
            self,
            instruction: str,
            *,
            image_paths: list[str] | None = None,
            cancellation_token: Any | None = None,
        ) -> int:
            calls["turn"] = (instruction, image_paths, cancellation_token)
            return 0

        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(agent_loop, "create_session", lambda **_kwargs: _LegacySession())

    code = agent_loop.run_agent(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        instruction="Do the task.",
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        ephemeral_system_messages=[],
        ephemeral_user_messages=(),
    )

    assert code == 0
    assert calls["turn"] == ("Do the task.", None, None)
    assert calls["closed"] is True


def test_shell_run_tool_uses_agent_loop_monkeypatch_and_keeps_legacy_keys(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    def fake_shell_run(**kwargs: Any) -> dict[str, Any]:
        calls.update(kwargs)
        return {
            "cmd": kwargs["cmd"],
            "effective_cmd": kwargs["cmd"],
            "cwd": str(tmp_path),
            "exit_code": 0,
            "stdout": "ok\n",
            "stderr": "",
            "truncated": False,
        }

    monkeypatch.setattr(agent_loop, "shell_run", fake_shell_run)
    tools = _build_compat_tools(tmp_path)

    result = tools["shell_run"].run({"cmd": "echo ok"})

    assert calls["cmd"] == "echo ok"
    for key in ("cmd", "effective_cmd", "cwd", "exit_code", "stdout", "stderr", "truncated"):
        assert key in result
    assert result["verification_evidence_category"] == "NOT_VERIFICATION"


def test_verify_run_tool_uses_agent_loop_monkeypatch_and_keeps_legacy_keys(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    def fake_run_task_verification(
        *,
        root: Path,
        commands: list[str],
        artifact_path: Path,
        cfg: AppConfig,
    ) -> VerifyRunResult:
        calls["root"] = root
        calls["commands"] = commands
        calls["cfg"] = cfg
        artifact_path.write_text("1 passed\n", encoding="utf-8")
        return VerifyRunResult(
            commands=commands,
            command_results=[
                VerifyCommandResult(
                    command=commands[0],
                    effective_command=commands[0],
                    exit_code=0,
                    output="1 passed\n",
                    stdout="1 passed\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(agent_loop, "run_task_verification", fake_run_task_verification)
    tools = _build_compat_tools(
        tmp_path,
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q"]),
    )

    result = tools["verify_run"].run({})

    assert calls["commands"] == ["pytest -q"]
    for key in ("commands", "all_passed", "summary", "artifact_path", "command_results"):
        assert key in result
    assert result["all_passed"] is True
    assert result["verification_evidence_allowed"] is True


def test_shell_service_tools_are_additive_to_legacy_tool_catalog(tmp_path: Path) -> None:
    tools = _build_compat_tools(tmp_path)

    for tool_name in ("shell_background", "shell_output", "shell_kill", "shell_list"):
        assert tool_name in tools
    for tool_name in (
        "shell_service_start",
        "workspace_preview_start",
        "shell_service_status",
        "shell_service_stop",
    ):
        assert tool_name in tools


def test_create_session_and_run_turn_legacy_call_return_int(tmp_path: Path) -> None:
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        verification_enabled=False,
        enable_compaction=False,
    )
    try:
        session.client = type(
            "Client",
            (),
            {
                "model": "test-model",
                "temperature": 0.2,
                "chat": lambda self, **_kwargs: LLMResponse(
                    content="This is an explanation only.",
                    tool_calls=[],
                    raw={},
                ),
            },
        )()

        code = session.run_turn("Explain the current parser behavior only.")
    finally:
        session.close()

    assert isinstance(code, int)


def test_turn_execution_state_payload_keeps_legacy_keys() -> None:
    payload = TurnExecutionState(execution_requested=True).as_payload()

    for key in (
        "execution_requested",
        "expected_verification_commands",
        "covered_verification_commands",
        "missing_verification_commands",
        "material_edit_count",
        "verification_attempt_count",
        "last_verification_passed",
        "completion_gate_repair_attempts",
    ):
        assert key in payload
    for key in (
        "acceptance_status_counts",
        "acceptance_problems",
        "acceptance_failure_summaries",
    ):
        assert key in payload


def test_verification_selection_refresh_helpers_keep_compatibility_exports() -> None:
    assert callable(agent_loop._refresh_interactive_turn_verification_selection)
    assert callable(agent_loop._refresh_execute_turn_verification_selection)


def test_subagent_result_keeps_existing_keys_and_uses_patchable_create_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    class _FakeSubStore:
        session_id = "child-session"

        def events_snapshot(self) -> list[dict[str, Any]]:
            return [{"type": "final", "payload": {"content": "child result"}}]

    class _FakeSubSession:
        store = _FakeSubStore()
        tools = {
            "fs_read": ToolDef(
                name="fs_read",
                description="read",
                parameters={"type": "object", "properties": {}, "required": []},
                run=lambda _args: {"ok": True},
            )
        }
        tool_list = [tools["fs_read"].as_openai_tool()]
        messages = [{"role": "assistant", "content": "child result"}]
        usage_summary = type("Usage", (), {"totals": lambda self: {}})()

        def run_turn(self, task: str) -> int:
            calls["task"] = task
            return 0

        def close(self) -> None:
            calls["child_closed"] = True

    def fake_create_session(**kwargs: Any) -> _FakeSubSession:
        calls["runtime_kind"] = kwargs["runtime_kind"]
        return _FakeSubSession()

    monkeypatch.setattr(agent_loop, "create_session", fake_create_session)
    tools = _build_compat_tools(
        tmp_path,
        subagents_enabled=True,
        subagent_registry={
            "compat": SubagentDefinition(
                name="compat",
                description="compat subagent",
                system_prompt="Inspect only.",
                mode="readonly",
            )
        },
    )

    result = tools["subagent_run"].run({"name": "compat", "task": "Inspect."})

    assert calls["runtime_kind"] == RuntimeKind.SUBAGENT
    assert calls["task"] == "Inspect."
    for key in ("result", "subagent", "subagent_session_id", "usage", "sandbox"):
        assert key in result
