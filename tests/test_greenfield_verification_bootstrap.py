"""A passing agent-authored suite bootstraps best-effort verification.

Later turns retain that contract. New modules without detected test references
produce an advisory; the reference scan does not measure executed coverage.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events

MODULE_SOURCE = "def add(a, b):\n    return a + b\n"
TEST_SOURCE = (
    "import unittest\n"
    "\n"
    "from logic import add\n"
    "\n"
    "\n"
    "class AddTests(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)
_VERIFY_ARGS = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
VERIFY_COMMAND = (
    subprocess.list2cmdline(_VERIFY_ARGS) if sys.platform == "win32" else shlex.join(_VERIFY_ARGS)
)

pytestmark = pytest.mark.usefixtures("approved_local_checks")


@pytest.fixture(autouse=True)
def _isolated_verification_environment(monkeypatch) -> None:
    # These integration tests execute their own small suites in tmp_path. They
    # exercise bootstrap behavior, not Docker availability or the caller's
    # sandbox preference. The session config explicitly selects host execution.
    monkeypatch.delenv("ALYSIS_VERIFY_SANDBOX_MODE", raising=False)
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")


class _ScriptedClient:
    """Replays scripted responses; tool-enabled and tool-less calls share one queue."""

    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
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
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(content="Nothing further.", tool_calls=[], raw={})


def _session(tmp_path: Path) -> Any:
    cfg = AppConfig(model="test-model")
    # This suite qualifies bootstrap from a real local check. Sandbox absence
    # and fail-closed behavior have their own tests; no Docker daemon is needed.
    cfg.extra_fields["verify_sandbox"] = {"mode": "off"}
    return create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "_sessions",
        verification_enabled=True,
        enable_chat_turn_step_budget=True,
    )


def _events(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def _write_tool(call_id: str, path: str, content: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="fs_write",
        arguments={"path": path, "content": content},
    )


def _build_suite_turn_responses() -> list[LLMResponse]:
    return [
        LLMResponse(
            content="Writing the module and its tests.",
            tool_calls=[
                _write_tool("w1", "logic.py", MODULE_SOURCE),
                _write_tool("w0", "tests/__init__.py", ""),
                _write_tool("w2", "tests/test_logic.py", TEST_SOURCE),
            ],
            raw={},
        ),
        LLMResponse(
            content="Running the suite.",
            tool_calls=[
                ToolCall(id="v1", name="verify_run", arguments={"commands": [VERIFY_COMMAND]})
            ],
            raw={},
        ),
        LLMResponse(
            content="Implemented logic.py with a passing agent-authored suite (1/1).",
            tool_calls=[],
            raw={},
        ),
    ]


def test_passing_agent_suite_bootstraps_best_effort_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_GREENFIELD_VERIFY_BOOTSTRAP", raising=False)
    session = _session(tmp_path)
    client = _ScriptedClient(_build_suite_turn_responses())
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("make me a lil adder module with tests")
        log_path = session.store.path
        assert exit_code == 0
        assert session.effective_verification_commands == [VERIFY_COMMAND]
        assert session.verification_contract_type == "best_effort"
        assert session.verification_best_effort is True
        assert session.verification_authoritative is False
    finally:
        session.close()

    updates = _events(log_path, "verification_contract_updated")
    bootstrap_updates = [
        item
        for item in updates
        if str(item.get("verification_selection_source", "")).startswith("agent_authored_bootstrap")
    ]
    assert bootstrap_updates, json.dumps(updates, ensure_ascii=False)[:500]
    assert bootstrap_updates[-1]["verification_contract_type"] == "best_effort"
    assert bootstrap_updates[-1]["verification_best_effort"] is True


def test_bootstrap_survives_next_turn_selection_refresh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_GREENFIELD_VERIFY_BOOTSTRAP", raising=False)
    session = _session(tmp_path)
    session.client = _ScriptedClient(_build_suite_turn_responses())  # type: ignore[assignment]
    try:
        assert session.run_turn("make me a lil adder module with tests") == 0
        assert session.verification_contract_type == "best_effort"
        # Second turn: pure question. The per-turn selection refresh must not
        # re-downgrade the bootstrapped contract back to unavailable.
        session.client = _ScriptedClient(  # type: ignore[assignment]
            [LLMResponse(content="It adds two numbers.", tool_calls=[], raw={})]
        )
        assert session.run_turn("what does add do?") == 0
        assert session.effective_verification_commands == [VERIFY_COMMAND]
        assert session.verification_contract_type == "best_effort"
    finally:
        session.close()


def test_failed_agent_suite_does_not_bootstrap_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_GREENFIELD_VERIFY_BOOTSTRAP", raising=False)
    session = _session(tmp_path)
    responses = _build_suite_turn_responses()
    responses[0].tool_calls[0].arguments["content"] = "def add(a, b):\n    return a - b\n"
    responses[-1] = LLMResponse(
        content="The test failed; the implementation is not verified.", tool_calls=[], raw={}
    )
    session.client = _ScriptedClient(responses)  # type: ignore[assignment]
    try:
        session.run_turn("make an adder module with tests")
        assert session.verification_contract_type == "unavailable"
        assert session.effective_verification_commands == []
        log_path = session.store.path
    finally:
        session.close()

    runs = _events(log_path, "verify_run")
    assert runs and runs[0]["status"] == "failed"
    assert runs[0]["all_passed"] is False
    assert not any(
        item.get("verification_selection_source", "").startswith("agent_authored_bootstrap")
        for item in _events(log_path, "verification_contract_updated")
    )


def test_bootstrapped_contract_arms_the_completion_gate_next_turn(
    tmp_path: Path, monkeypatch
) -> None:
    """The bootstrap payoff: after bootstrap, an edit without a re-run gets gated."""
    monkeypatch.delenv("ALYSIS_GREENFIELD_VERIFY_BOOTSTRAP", raising=False)
    session = _session(tmp_path)
    session.client = _ScriptedClient(_build_suite_turn_responses())  # type: ignore[assignment]
    try:
        assert session.run_turn("make me a lil adder module with tests") == 0

        edit_then_claim = [
            LLMResponse(
                content="Adjusting add.",
                tool_calls=[
                    ToolCall(
                        id="e1",
                        name="fs_write",
                        arguments={
                            "path": "logic.py",
                            "content": "def add(a, b):\n    return b + a\n",
                        },
                    )
                ],
                raw={},
            ),
            # Tries to finalize without re-running the suite.
            LLMResponse(content="Swapped the operand order. Done.", tool_calls=[], raw={}),
            # After the gate nudge: actually re-runs verification.
            LLMResponse(
                content="Re-running the suite.",
                tool_calls=[ToolCall(id="v2", name="verify_run", arguments={})],
                raw={},
            ),
            LLMResponse(content="Suite re-run: 1/1 passing.", tool_calls=[], raw={}),
        ]
        session.client = _ScriptedClient(edit_then_claim)  # type: ignore[assignment]
        assert session.run_turn("swap the operand order in add") == 0
        log_path = session.store.path
    finally:
        session.close()

    nudges = _events(log_path, "completion_gate_nudge")
    assert nudges, "bootstrapped contract must arm the completion gate on greenfield"
    finals = _events(log_path, "final")
    assert "1/1" in str(finals[-1]["content"])
    assessment = _events(log_path, "blast_radius_assessment")[-1]
    # This turn has no pre-edit baseline, so attribution remains unknown. The
    # observed post-edit pass must nevertheless count as an executed scope run.
    assert assessment["status"] == "unattributed"
    assert assessment["unattributed"] == []
    assert assessment["gate_command"] == VERIFY_COMMAND


@pytest.mark.parametrize("enabled", [False, True])
def test_new_module_without_tests_gets_optional_coverage_marker(
    tmp_path: Path, monkeypatch, enabled: bool
) -> None:
    if enabled:
        monkeypatch.setenv("ALYSIS_ZERO_COVERAGE_ADVISORY", "on")
    else:
        monkeypatch.delenv("ALYSIS_ZERO_COVERAGE_ADVISORY", raising=False)
    session = _session(tmp_path)
    responses = [
        LLMResponse(
            content="Writing util.py.",
            tool_calls=[_write_tool("w1", "util.py", MODULE_SOURCE)],
            raw={},
        ),
        LLMResponse(content="util.py is in place.", tool_calls=[], raw={}),
        # In case the gate nudges (no verification exists), finalize again.
        LLMResponse(content="util.py is in place; nothing else to run.", tool_calls=[], raw={}),
    ]
    session.client = _ScriptedClient(responses)  # type: ignore[assignment]
    try:
        assert session.run_turn("write util.py with an add function") == 0
        log_path = session.store.path
    finally:
        session.close()

    events = _events(log_path, "zero_coverage_modules")
    assert bool(events) is enabled
    if enabled:
        assert events[-1]["modules"] == ["util.py"]
    finals = _events(log_path, "final")
    assert ("without detected test references" in str(finals[-1]["content"])) is enabled


def test_covered_new_module_gets_no_marker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_ZERO_COVERAGE_ADVISORY", "on")
    session = _session(tmp_path)
    session.client = _ScriptedClient(_build_suite_turn_responses())  # type: ignore[assignment]
    try:
        assert session.run_turn("make me a lil adder module with tests") == 0
        log_path = session.store.path
    finally:
        session.close()

    assert _events(log_path, "zero_coverage_modules") == []
    finals = _events(log_path, "final")
    assert "without detected test references" not in str(finals[-1]["content"])


def test_bootstrap_kill_switch_restores_legacy_behavior(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_GREENFIELD_VERIFY_BOOTSTRAP", "off")
    session = _session(tmp_path)
    session.client = _ScriptedClient(_build_suite_turn_responses())  # type: ignore[assignment]
    try:
        assert session.run_turn("make me a lil adder module with tests") == 0
        assert session.verification_contract_type != "best_effort"
    finally:
        session.close()
