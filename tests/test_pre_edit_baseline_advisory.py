"""Exercise baseline advisories through actual session tools and local test runs."""

from __future__ import annotations

import copy
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent.turn.core as turn_core
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface

_EXECUTED_ARGV = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
_EXECUTED_COMMAND = (
    subprocess.list2cmdline(_EXECUTED_ARGV) if os.name == "nt" else shlex.join(_EXECUTED_ARGV)
)
_KNOWN_COMMAND = _EXECUTED_COMMAND
_INITIAL_SOURCE = "def add(a, b):\n    return a - b\n"
_INTERMEDIATE_SOURCE = "def add(a, b):\n    return b - a\n"
_FIXED_SOURCE = "def add(a, b):\n    return a + b\n"


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = iter(responses)

    def chat(self, **kwargs: Any) -> LLMResponse:
        response = next(self.responses, None)
        if response is not None:
            return response
        # A forced final has no tools. Never invent more work if the script ends.
        assert not kwargs.get("tools"), "scripted tool-enabled responses exhausted"
        return LLMResponse(content="The local test run is recorded.", tool_calls=[], raw={})


def _response(tool: ToolCall) -> LLMResponse:
    return LLMResponse(content="", tool_calls=[tool], raw={})


def _write(call_id: str, source: str) -> LLMResponse:
    return _response(
        ToolCall(
            id=call_id,
            name="fs_write",
            arguments={"path": "logic.py", "content": source},
        )
    )


def _run(call_id: str, tool_name: str) -> LLMResponse:
    arguments = (
        {"commands": [_EXECUTED_COMMAND]}
        if tool_name == "verify_run"
        else {"cmd": _EXECUTED_COMMAND}
    )
    return _response(ToolCall(id=call_id, name=tool_name, arguments=arguments))


@pytest.mark.parametrize("one_shot", [True, False], ids=["one-shot", "chat"])
@pytest.mark.parametrize("tool_name", ["verify_run", "shell_run"])
@pytest.mark.parametrize("baseline_kind", ["failing", "missing", "incomplete", "post-edit"])
def test_first_edit_advisory_uses_pre_edit_test_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    one_shot: bool,
    tool_name: str,
    baseline_kind: str,
) -> None:
    # The only model is scripted; tools execute the tiny local fixture normally.
    for name in (
        "ALYSIS_VERIFY_SANDBOX_MODE",
        "ALYSIS_VERIFY_SANDBOX_MODE",
        "ALYSIS_SHELL_SANDBOX_MODE",
        "ALYSIS_SHELL_SANDBOX_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALYSIS_REGRESSION_BASELINE", "on")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr(
        NoopSurface,
        "request_approval",
        lambda self, request: ApprovalDecision(request.kind in {"verify_run", "shell_run"}),
    )
    root = tmp_path / "repo"
    root.mkdir()
    (root / "logic.py").write_text(_INITIAL_SOURCE)
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("")
    # The incomplete case exits during its failing test, before a runner summary
    # or failure ID exists. After the code fix the same unchanged test completes.
    incomplete_failure = (
        "        if add(2, 3) != 5:\n            os._exit(1)\n"
        if baseline_kind == "incomplete"
        else ""
    )
    (root / "tests" / "test_logic.py").write_text(
        "import os\nimport unittest\nfrom logic import add\n\n"
        "class AddTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        + incomplete_failure
        + "        self.assertEqual(add(2, 3), 5)\n"
    )
    snapshots: list[dict[str, Any]] = []
    original_record = turn_core._record_tool_effect

    def observe_record(**kwargs: Any) -> None:
        original_record(**kwargs)
        snapshots.append(
            {
                "tool": kwargs["tool_name"],
                "state": copy.deepcopy(kwargs["state"].as_payload()),
                "result": copy.deepcopy(kwargs["result"]),
            }
        )

    monkeypatch.setattr(turn_core, "_record_tool_effect", observe_record)
    responses = []
    if baseline_kind in {"failing", "incomplete"}:
        responses.append(_run("before-edit", tool_name))
    responses.append(_write("first-edit", _INTERMEDIATE_SOURCE))
    if baseline_kind == "post-edit":
        responses.append(_run("after-first-edit", tool_name))
    responses.extend(
        [
            _write("second-edit", _FIXED_SOURCE),
            _run("after-fix", tool_name),
            LLMResponse(content="Updated logic.py; the local test passes.", tool_calls=[], raw={}),
        ]
    )
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="code_only",
            verify_commands=[_KNOWN_COMMAND],
            extra_fields={
                "verify_sandbox": {"mode": "off"},
                "shell_sandbox": {"mode": "off", "clear_env": False},
            },
        ),
        root=root,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=one_shot,
        session_log_dir_override=tmp_path / "sessions",
        surface=NoopSurface(),
        subagents_enabled=False,
    )
    session.client = _ScriptedClient(responses)  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Fix add in logic.py so add(2, 3) returns 5.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert (root / "logic.py").read_text() == _FIXED_SOURCE
    edits = [item for item in snapshots if item["tool"] == "fs_write"]
    assert len(edits) == 2
    assert [item["state"]["verification_relevant_edit_generation"] for item in edits] == [1, 2]
    events = list(read_session_events(log_path))
    nudges = [
        event["payload"]
        for event in events
        if event["type"] == "regression_baseline_pre_edit_nudge"
    ]
    if baseline_kind == "failing":
        assert nudges == []
    else:
        # The advisory follows the first successful edit and never blocks either
        # write or repeats when a later run cannot supply a pre-edit baseline.
        assert len(nudges) == 1
        assert nudges[0]["tool_call_id"] == "first-edit"
        assert nudges[0]["known_verification_commands"] == [_KNOWN_COMMAND]

    captures = [event["payload"] for event in events if event["type"] == "test_baseline_captured"]
    test_runs = [item for item in snapshots if item["tool"] == tool_name]
    if baseline_kind == "failing":
        before = test_runs[0]
        state = before["state"]
        assert state["verification_relevant_edit_generation"] == 0
        assert state["last_verification_passed"] is False
        assert state["accepted_verification_evidence"] == []
        assert state["rejected_verification_evidence"]
        evidence = state["rejected_verification_evidence"][-1]
        assert evidence["real_execution"] is None
        run_result = before["result"]
        command_result = (
            run_result["command_results"][0] if tool_name == "verify_run" else run_result
        )
        assert command_result["exit_code"] == 1
        baseline = next(iter(state["test_baselines"].values()))
        assert baseline["command"] == _EXECUTED_COMMAND
        assert baseline["edit_generation"] == 0
        assert baseline["report"]["counts_known"] is True
        assert baseline["report"]["ids_complete"] is True
        assert baseline["report"]["failed"] == 1
        assert captures[0]["kind"] == "baseline"
    else:
        assert not any(event["kind"] == "baseline" for event in captures)
        assert all(not item["state"]["test_baselines"] for item in snapshots)
        if baseline_kind == "incomplete":
            assert captures[0]["kind"] == "baseline_unusable"
            assert captures[0]["report"]["counts_known"] is False
        if baseline_kind == "post-edit":
            assert captures[0]["kind"] == "post_edit"
            assert captures[0]["generation"] == 1
            assert captures[0]["report"]["failed"] == 1
    assert test_runs[-1]["state"]["last_verification_passed"] is True
