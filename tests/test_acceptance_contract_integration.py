from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult


@pytest.fixture(autouse=True)
def acceptance_contract_host_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """These inert-client tests exercise evidence, not native worker installation.

    Keep file effects in the test's temporary workspace. Native sandbox admission
    and installed-byte qualification are covered by their separate real gates.
    """
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    # The scripted verifier/command handlers below are controlled test ports.
    # Explicitly answer their approval cards; --yes is not a person decision.
    monkeypatch.setattr(
        NoopSurface,
        "request_approval",
        lambda self, request: ApprovalDecision(request.kind in {"verify_run", "shell_run"}),
    )


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        response = self._responses[self.calls]
        self.calls += 1
        return response


def _events(sessions_dir: Path, session_id: str) -> list[dict[str, Any]]:
    return list(read_session_events(sessions_dir / f"{session_id}.jsonl"))


def _latest_completion_gate_payload(
    sessions_dir: Path,
    session_id: str,
    *,
    event_type: str = "completion_gate_accepted_with_open_problems",
) -> dict[str, Any]:
    matching_events = [
        event for event in _events(sessions_dir, session_id) if event.get("type") == event_type
    ]
    assert matching_events
    return dict(matching_events[-1].get("payload") or {})


def test_one_shot_static_task_suppresses_generic_pytest(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>Demo</h1>\n", encoding="utf-8")
    cfg = AppConfig(
        model="test-model",
        routing_mode="code_only",
        verify_commands=["pytest -q"],
    )
    sessions_dir = tmp_path / "sessions"
    session_id = "acceptance-static-no-pytest"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    try:
        agent_loop_mod._refresh_execute_turn_verification_selection(
            session,
            instruction="Create output.txt for this static site.",
            route_execution_posture="execute",
        )
    finally:
        session.close()

    assert session.effective_verification_commands == []
    updates = [
        event
        for event in _events(sessions_dir, session_id)
        if event.get("type") == "verification_contract_updated"
    ]
    assert updates
    payload = dict(updates[-1].get("payload") or {})
    assert payload.get("verification_contract_type") == "unavailable"


def test_one_shot_python_repo_keeps_existing_repo_native_pytest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from src.app import value\n\ndef test_value():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run_task_verification(
        *,
        root: Path,
        commands: list[str],
        artifact_path: Path,
        cfg: AppConfig,
    ) -> VerifyRunResult:
        _ = root, cfg
        calls.append(commands)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("1 passed in 0.01s\n", encoding="utf-8")
        return VerifyRunResult(
            commands=commands,
            command_results=[
                VerifyCommandResult(
                    command=commands[0],
                    effective_command=commands[0],
                    exit_code=0,
                    output="1 passed in 0.01s\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(agent_loop_mod, "run_task_verification", fake_run_task_verification)

    cfg = AppConfig(
        model="test-model",
        routing_mode="code_only",
        verify_commands=["pytest -q"],
        # The mocked verifier represents a host run with observable pytest defaults.
        extra_fields={"verify_sandbox": {"mode": "off"}},
    )
    sessions_dir = tmp_path / "sessions"
    session_id = "acceptance-python-keeps-pytest"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "src/app.py", "content": "def value():\n    return 2\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc2", name="verify_run", arguments={})],
                raw={},
            ),
            LLMResponse(content="Updated src/app.py and pytest passes.", tool_calls=[], raw={}),
        ]
    )  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Update src/app.py and verify it.")
    finally:
        session.close()

    assert exit_code == 0
    assert calls == [["pytest -q"]]
    assert session.client.calls == 3
    assert not [
        event
        for event in _events(sessions_dir, session_id)
        if event.get("type") == "completion_gate_nudge"
        and (event.get("payload") or {}).get("stage") == "adversarial_finalize_review"
    ]


def test_exact_black_box_command_satisfies_one_shot_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "expected.txt").write_text("ok\n", encoding="utf-8")

    def fake_shell_run(
        *, root: Path, cmd: str, cwd: str | None = None, runner=None
    ) -> dict[str, Any]:
        _ = root, cwd, runner
        return {"cmd": cmd, "effective_cmd": cmd, "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(agent_loop_mod, "shell_run", fake_shell_run)

    cfg = AppConfig(
        model="test-model",
        routing_mode="code_only",
        verify_commands=["pytest -q"],
    )
    sessions_dir = tmp_path / "sessions"
    session_id = "acceptance-black-box"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "actual.txt", "content": "ok\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc2",
                        name="shell_run",
                        arguments={"cmd": "diff expected.txt actual.txt"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Created actual.txt and diff passed.", tool_calls=[], raw={}),
        ]
    )  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Create actual.txt and run `diff expected.txt actual.txt`.")
    finally:
        session.close()

    assert exit_code == 0
    finalized = [
        event
        for event in _events(sessions_dir, session_id)
        if event.get("type") == "turn_intent_finalized"
    ]
    assert finalized
    payload = dict(finalized[-1].get("payload") or {})
    assert payload.get("acceptance_problems") == []


def test_missing_required_output_surfaces_advisory_open_problems(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    sessions_dir = tmp_path / "sessions"
    session_id = "acceptance-missing-output"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
        verification_enabled=False,
    )
    session.client = _ScriptedClient(
        [
            LLMResponse(content="Created result.txt.", tool_calls=[], raw={}),
            LLMResponse(content="Created result.txt.", tool_calls=[], raw={}),
            LLMResponse(content="Created result.txt.", tool_calls=[], raw={}),
        ]
    )  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Create result.txt.")
    finally:
        session.close()

    # The completion gate is advisory-only: the turn completes (exit 0), but the
    # unmet acceptance criterion is surfaced as a gate problem, first as a
    # failed-gate advisory and then in the accepted-with-open-problems record.
    assert exit_code == 0
    failed_payload = _latest_completion_gate_payload(
        sessions_dir,
        session_id,
        event_type="one_shot_completion_gate_failed",
    )
    assert "acceptance_criteria_unverified" in set(failed_payload.get("problems") or [])
    accepted_payload = _latest_completion_gate_payload(sessions_dir, session_id)
    assert "acceptance_criteria_unverified" in set(accepted_payload.get("problems") or [])
