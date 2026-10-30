"""Adversarial finalize review: one bounded pass for unverified clean claims.

Scored-run near-misses cluster on small edits to existing files whose gate is
otherwise clear but lacks fresh independent evidence. Freshly verified turns,
turns that only create new files, and turns with the feature disabled finish
without an extra model response.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent.verification import TurnExecutionState
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult

_VERIFY_OK_COMMAND = "pytest tests/test_cli.py -q"


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
        tool_choice: Any | None = None,
        stream: bool = False,
        on_text_delta: Any | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, tool_choice, stream, on_text_delta, temperature
        if self.calls >= len(self._responses):
            raise AssertionError("scripted response exhausted")
        response = self._responses[self.calls]
        self.calls += 1
        return response


@pytest.fixture(autouse=True)
def _fake_verify_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_task_verification(
        *,
        root: Path,
        commands: list[str],
        artifact_path: Path,
        cfg: AppConfig,
    ) -> VerifyRunResult:
        _ = root, cfg
        command_results = [
            VerifyCommandResult(command=command, exit_code=0, output="ok\n", real_execution=True)
            for command in commands
        ]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("ok\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(commands),
            command_results=command_results,
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(agent_loop_mod, "run_task_verification", fake_run_task_verification)
    monkeypatch.setattr(
        NoopSurface,
        "request_approval",
        lambda self, request: ApprovalDecision(request.kind == "verify_run"),
    )


def _init_git_repo_with_commit(repo: Path) -> None:
    repo.mkdir(exist_ok=True)
    for args in (
        ["init"],
        ["config", "user.name", "Test User"],
        ["config", "user.email", "test@example.com"],
    ):
        subprocess.run(
            ["git", "-C", os.fspath(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    (repo / "util.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", os.fspath(repo), "add", "util.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
    )


def _events_of(sessions_dir: Path, session_id: str) -> list[dict[str, Any]]:
    return list(read_session_events(sessions_dir / f"{session_id}.jsonl"))


def _payloads(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event["payload"] for event in events if event.get("type") == event_type]


def _run_turn(
    tmp_path: Path,
    *,
    session_id: str,
    responses: list[LLMResponse],
) -> tuple[int, list[dict[str, Any]], _ScriptedClient]:
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="code_only",
            verify_commands=[_VERIFY_OK_COMMAND],
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    client = _ScriptedClient(responses)
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Fix the add() bug in util.py.")
    finally:
        session.close()
    return exit_code, _events_of(sessions_dir, session_id), client


def _bug_fix_script(*, finals: int) -> list[LLMResponse]:
    script = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    name="fs_write",
                    arguments={
                        "path": "util.py",
                        "content": "def add(a, b):\n    return a + b\n",
                    },
                )
            ],
            raw={},
        ),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc2",
                    name="verify_run",
                    arguments={"commands": [_VERIFY_OK_COMMAND]},
                )
            ],
            raw={},
        ),
    ]
    script.extend(
        LLMResponse(content="Fixed add() and verified it.", tool_calls=[], raw={})
        for _ in range(finals)
    )
    return script


def test_adversarial_review_skips_existing_code_with_fresh_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_ADVERSARIAL_FINALIZE", raising=False)
    _init_git_repo_with_commit(tmp_path)

    exit_code, events, client = _run_turn(
        tmp_path,
        session_id="adversarial-fresh-verification",
        responses=_bug_fix_script(finals=1),
    )

    assert exit_code == 0
    nudges = [
        payload
        for payload in _payloads(events, "completion_gate_nudge")
        if payload.get("stage") == "adversarial_finalize_review"
    ]
    assert nudges == []
    assert client.calls == 3


def test_adversarial_review_fires_once_without_fresh_independent_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_ADVERSARIAL_FINALIZE", raising=False)
    original_record = TurnExecutionState.record_verification_evidence

    def record_without_independent_acceptance(
        self: TurnExecutionState,
        evidence: Any,
        *,
        accepted: bool,
        observed_exit_code: int | None = None,
        observed_output: bool = False,
    ) -> None:
        _ = accepted
        original_record(
            self,
            evidence,
            accepted=False,
            observed_exit_code=observed_exit_code,
            observed_output=observed_output,
        )

    monkeypatch.setattr(
        TurnExecutionState,
        "record_verification_evidence",
        record_without_independent_acceptance,
    )
    _init_git_repo_with_commit(tmp_path)

    exit_code, events, client = _run_turn(
        tmp_path,
        session_id="adversarial-no-independent-verification",
        responses=_bug_fix_script(finals=2),
    )

    assert exit_code == 0
    nudges = [
        payload
        for payload in _payloads(events, "completion_gate_nudge")
        if payload.get("stage") == "adversarial_finalize_review"
    ]
    assert len(nudges) == 1
    assert nudges[0]["touched_code_paths"] == ["util.py"]
    assert client.calls == 4


def test_adversarial_review_kill_switch_restores_previous_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_ADVERSARIAL_FINALIZE", "off")
    _init_git_repo_with_commit(tmp_path)

    exit_code, events, client = _run_turn(
        tmp_path,
        session_id="adversarial-off",
        responses=_bug_fix_script(finals=1),
    )

    assert exit_code == 0
    assert not [
        payload
        for payload in _payloads(events, "completion_gate_nudge")
        if payload.get("stage") == "adversarial_finalize_review"
    ]
    assert client.calls == 3


def test_adversarial_review_skips_turns_that_only_create_new_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_ADVERSARIAL_FINALIZE", raising=False)
    _init_git_repo_with_commit(tmp_path)
    script = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    name="fs_write",
                    arguments={"path": "notes.py", "content": "VALUE = 1\n"},
                )
            ],
            raw={},
        ),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc2",
                    name="verify_run",
                    arguments={"commands": [_VERIFY_OK_COMMAND]},
                )
            ],
            raw={},
        ),
        LLMResponse(content="Created notes.py and verified.", tool_calls=[], raw={}),
    ]

    exit_code, events, client = _run_turn(
        tmp_path,
        session_id="adversarial-created-only",
        responses=script,
    )

    assert exit_code == 0
    assert not [
        payload
        for payload in _payloads(events, "completion_gate_nudge")
        if payload.get("stage") == "adversarial_finalize_review"
    ]
    assert client.calls == 3
