"""Declared verification completes directly; narrower runs cannot replace it."""

from __future__ import annotations

import copy
import os
import sys
from collections import deque
from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent.turn.core as turn_core
from alysis_code.agent.verification_commands import _matching_effective_verification_commands
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface

_INFERRED_COMMAND = "python -m unittest discover"
_SELECTED_COMMAND = "python3 -m unittest discover -s tests -v"
_DECLARED_COMMAND = "python3 -m unittest discover"
_FIXED_SOURCE = "def add(a, b):\n    return a + b\n"


@pytest.fixture(autouse=True)
def _local_python_launcher(monkeypatch):
    monkeypatch.setenv(
        "PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    monkeypatch.setattr(
        NoopSurface,
        "request_approval",
        lambda self, request: ApprovalDecision(request.kind in {"verify_run", "shell_run"}),
    )


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = deque(responses)
        self.calls = 0

    def chat(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        assert self.responses, "unexpected model call after the verification script completed"
        return self.responses.popleft()


def _run(tool_name: str, call_id: str, command: str) -> LLMResponse:
    arguments = {"commands": [command]} if tool_name == "verify_run" else {"cmd": command}
    return LLMResponse(
        content="", tool_calls=[ToolCall(id=call_id, name=tool_name, arguments=arguments)], raw={}
    )


@pytest.mark.parametrize("one_shot", [True, False], ids=["one-shot", "chat"])
@pytest.mark.parametrize("tool_name", ["verify_run", "shell_run"])
@pytest.mark.parametrize(
    "extra_test_directory", [False, True], ids=["declared-command", "narrower-command"]
)
def test_declared_verification_finishes_without_redundant_runs_and_narrower_does_not_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    one_shot: bool,
    tool_name: str,
    extra_test_directory: bool,
) -> None:
    for name in (
        "ALYSIS_VERIFY_SANDBOX_MODE",
        "ALYSIS_VERIFY_SANDBOX_MODE",
        "ALYSIS_SHELL_SANDBOX_MODE",
        "ALYSIS_SHELL_SANDBOX_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'scope-fixture'\nversion = '0.1.0'\n")
    (root / "logic.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_logic.py").write_text(
        "import unittest\nfrom logic import add\n\n"
        "class AddTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n"
    )
    if extra_test_directory:
        (root / "integration").mkdir()
        (root / "integration" / "__init__.py").write_text("")
        (root / "integration" / "test_integration.py").write_text(
            "import unittest\nfrom logic import add\n\n"
            "class IntegrationTests(unittest.TestCase):\n"
            "    def test_add_negative(self):\n"
            "        self.assertEqual(add(-2, 3), 1)\n"
        )

    # Changing the Python launcher preserves the declared selection. Adding a
    # discovery start directory is not evidence of equivalent execution.
    assert _matching_effective_verification_commands(
        observed_command=_DECLARED_COMMAND,
        effective_verification_commands=[_INFERRED_COMMAND],
    ) == {_INFERRED_COMMAND}
    assert not _matching_effective_verification_commands(
        observed_command=_SELECTED_COMMAND,
        effective_verification_commands=[_INFERRED_COMMAND],
    )
    snapshots: list[dict[str, Any]] = []
    original_record = turn_core._record_tool_effect

    def observe_record(**kwargs: Any) -> None:
        original_record(**kwargs)
        snapshots.append(
            {
                "tool": kwargs["tool_name"],
                "arguments": copy.deepcopy(kwargs["arguments"]),
                "result": copy.deepcopy(kwargs["result"]),
                "state": copy.deepcopy(kwargs["state"].as_payload()),
            }
        )

    monkeypatch.setattr(turn_core, "_record_tool_effect", observe_record)
    final = LLMResponse(
        content="Fixed add in logic.py. The local tests passed.", tool_calls=[], raw={}
    )
    command = _SELECTED_COMMAND if extra_test_directory else _DECLARED_COMMAND
    responses = [
        _run(tool_name, "baseline", command),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="fix",
                    name="fs_write",
                    arguments={"path": "logic.py", "content": _FIXED_SOURCE},
                )
            ],
            raw={},
        ),
        _run(tool_name, "selected-after-fix", command),
        final,
    ]
    if extra_test_directory and one_shot:
        responses.extend([_run(tool_name, "declared-after-nudge", _DECLARED_COMMAND), final])
    client = _ScriptedClient(responses)
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="code_only",
            extra_fields={
                "verify_sandbox": {"mode": "off"},
                "shell_sandbox": {"mode": "off", "clear_env": False},
            },
        ),
        root=root,
        verify_cmd=[_INFERRED_COMMAND],
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
    session.client = client  # type: ignore[assignment]
    try:
        assert session.effective_verification_commands == [_INFERRED_COMMAND]
        assert session.verification_selection_source == "cli.verify_cmd"
        assert session.verification_contract_type == "explicit_override"
        exit_code = session.run_turn("Fix add in logic.py so addition returns the correct sum.")
        assert session.effective_verification_commands == [_INFERRED_COMMAND]
        assert session.verification_selection_source == "cli.verify_cmd"
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert (root / "logic.py").read_text() == _FIXED_SOURCE
    assert not client.responses
    needs_declared_retry = extra_test_directory and one_shot
    assert client.calls == (6 if needs_declared_retry else 4)
    runs = [item for item in snapshots if item["tool"] == tool_name]
    assert len(runs) == (3 if needs_declared_retry else 2)
    before = runs[0]
    baseline_result = (
        before["result"]["command_results"][0] if tool_name == "verify_run" else before["result"]
    )
    assert baseline_result["exit_code"] == 1
    assert before["state"]["accepted_verification_evidence"] == []
    assert before["state"]["last_verification_passed"] is False
    assert next(iter(before["state"]["test_baselines"].values()))["report"]["failed"] == 1

    selected_state = runs[1]["state"]
    if extra_test_directory:
        assert selected_state["covered_verification_commands"] == []
        assert selected_state["missing_verification_commands"] == [_INFERRED_COMMAND]
        assert selected_state["accepted_verification_evidence"] == []
        assert selected_state["supplemental_verification_evidence"]
    else:
        assert selected_state["covered_verification_commands"] == [_INFERRED_COMMAND]
        assert selected_state["missing_verification_commands"] == []
        assert selected_state["accepted_verification_evidence"][-1][
            "covered_verification_commands"
        ] == [_INFERRED_COMMAND]

    if extra_test_directory and not one_shot:
        # Existing chat finalization permits an incomplete verification state.
        # It must not turn that boundary into credit for an unexecuted command.
        # The same selected command did pass after the fix; its old failure
        # must not be confused with the still-missing required selection.
        assert runs[-1]["state"]["last_verification_passed"] is True
        assert runs[-1]["state"]["missing_verification_commands"] == [_INFERRED_COMMAND]
        assert runs[-1]["state"]["accepted_verification_evidence"] == []
    else:
        assert runs[-1]["state"]["last_verification_passed"] is True
        assert runs[-1]["state"]["missing_verification_commands"] == []
        assert runs[-1]["state"]["accepted_verification_evidence"][-1][
            "covered_verification_commands"
        ] == [_INFERRED_COMMAND]
    events = list(read_session_events(log_path))
    assert not any(event["type"] == "regression_baseline_pre_edit_nudge" for event in events)
    nudges = [
        event["payload"]
        for event in events
        if event["type"] in {"completion_gate_nudge", "one_shot_completion_gate_nudge"}
    ]
    if needs_declared_retry:
        assert len(nudges) == 1
        assert "verification_incomplete" in nudges[0]["problems"]
        assert "verification_failed" not in nudges[0]["problems"]
    else:
        assert nudges == []
    assert len([event for event in events if event["type"] == "final"]) == 1


@pytest.mark.parametrize("tool_name", ["verify_run", "shell_run"])
@pytest.mark.parametrize("explicit", [False, True], ids=["inferred", "explicit"])
def test_unittest_discovery_hook_failure_is_not_erased_by_narrower_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str, explicit: bool
) -> None:
    for name in (
        "ALYSIS_VERIFY_SANDBOX_MODE",
        "ALYSIS_VERIFY_SANDBOX_MODE",
        "ALYSIS_SHELL_SANDBOX_MODE",
        "ALYSIS_SHELL_SANDBOX_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'hook-fixture'\nversion = '0.1.0'\n")
    (root / "tests" / "__init__.py").write_text(
        "from . import extra, test_visible\n\n"
        "def load_tests(loader, standard_tests, pattern):\n"
        "    suite = loader.loadTestsFromModule(test_visible)\n"
        "    suite.addTests(loader.loadTestsFromModule(extra))\n"
        "    return suite\n"
    )
    (root / "tests" / "test_visible.py").write_text(
        "import unittest\n\n"
        "class Visible(unittest.TestCase):\n"
        "    def test_visible(self):\n"
        "        self.assertEqual(2 + 2, 4)\n"
    )
    (root / "tests" / "extra.py").write_text(
        "import unittest\n\n"
        "class Extra(unittest.TestCase):\n"
        "    def test_hook_only(self):\n"
        "        self.fail('failure added by package discovery hook')\n"
    )
    snapshots: list[dict[str, Any]] = []
    original_record = turn_core._record_tool_effect

    def observe_record(**kwargs: Any) -> None:
        original_record(**kwargs)
        snapshots.append(
            {
                "tool": kwargs["tool_name"],
                "result": copy.deepcopy(kwargs["result"]),
                "state": copy.deepcopy(kwargs["state"].as_payload()),
            }
        )

    monkeypatch.setattr(turn_core, "_record_tool_effect", observe_record)
    client = _ScriptedClient(
        [
            _run(tool_name, "declared-discovery", _DECLARED_COMMAND),
            _run(tool_name, "narrower-discovery", _SELECTED_COMMAND),
            LLMResponse(
                content=(
                    "Declared discovery failed in tests.extra. "
                    "The narrower tests-directory run passed and does not resolve that failure."
                ),
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="code_only",
            extra_fields={
                "verify_sandbox": {"mode": "off"},
                "shell_sandbox": {"mode": "off", "clear_env": False},
            },
        ),
        root=root,
        verify_cmd=[_INFERRED_COMMAND] if explicit else None,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        surface=NoopSurface(),
        subagents_enabled=False,
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.effective_verification_commands == [_INFERRED_COMMAND]
        assert session.verification_selection_source == (
            "cli.verify_cmd" if explicit else "repo_scan.likely_test_commands"
        )
        assert (
            session.run_turn("Run the declared and narrower test selections; report without edits.")
            == 0
        )
    finally:
        session.close()

    assert client.calls == 3
    assert not client.responses
    runs = [item for item in snapshots if item["tool"] == tool_name]
    assert len(runs) == 2
    results = [
        item["result"]["command_results"][0] if tool_name == "verify_run" else item["result"]
        for item in runs
    ]
    assert [item["exit_code"] for item in results] == [1, 0]
    reports = [list(item["state"]["test_baselines"].values()) for item in runs]
    assert reports[0][0]["report"]["failed"] == 1
    assert reports[0][0]["report"]["counts_known"] is True
    assert reports[0][0]["report"]["ids_complete"] is True
    assert any("test_hook_only" in node for node in reports[0][0]["report"]["failed_ids"])
    assert len(reports[1]) == 2
    assert reports[1][1]["report"]["failed"] == 0
    final_state = runs[-1]["state"]
    assert final_state["last_verification_passed"] is False
    if explicit:
        assert final_state["accepted_verification_evidence"] == []
    else:
        assert len(final_state["accepted_verification_evidence"]) == 1
    assert final_state["covered_verification_commands"] == []
    assert final_state["missing_verification_commands"] == ([_INFERRED_COMMAND] if explicit else [])
    assert final_state["failed_verification_commands"] == ([_INFERRED_COMMAND] if explicit else [])
    assert final_state["unresolved_observed_verification_failures"] == (0 if explicit else 1)
    assert (
        final_state[
            "supplemental_verification_evidence" if explicit else "accepted_verification_evidence"
        ][-1]["covered_verification_commands"]
        == []
    )
