"""Recommendations remain usable without becoming unrequested test mandates."""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any

import pytest

# Reuse the socket/HTTP deny guard, including inherited Python subprocesses.
# It is installed before any session factory, independently of test credentials.
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)

import alysis_code.agent.turn.core as turn_core
from alysis_code.agent import session as session_mod
from alysis_code.agent.verification_commands import _matching_effective_verification_commands
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.verification_contract import (
    VerificationCommandProvenance,
    VerificationCommandRequirement,
    build_verification_command_spec,
)

_INFERRED = "python -m unittest discover"
_SELECTED_ARGV = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
_SELECTED = (
    subprocess.list2cmdline(_SELECTED_ARGV) if os.name == "nt" else shlex.join(_SELECTED_ARGV)
)
_REQUIRED = f"{_SELECTED} -p test_logic.py"
_FIXED = "def add(a, b):\n    return a + b\n"


class _ScriptedClient:
    def __init__(self, responses: list[LLMResponse], *, model: str, **_kwargs: Any) -> None:
        self.model = model
        self.temperature = 0.0
        self.responses = deque(responses)
        self.requests: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.requests.append(copy.deepcopy(kwargs))
        assert self.responses, "unexpected request beyond the bounded offline script"
        return self.responses.popleft()


def _run(tool: str, call_id: str, command: str) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id=call_id,
                name=tool,
                arguments={"commands": [command]} if tool == "verify_run" else {"cmd": command},
            )
        ],
        raw={},
    )


@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
@pytest.mark.parametrize(
    ("authority", "tool"),
    [
        ("inferred", "verify_run"),
        ("inferred", "shell_run"),
        ("cli", "verify_run"),
        ("config", "verify_run"),
        # Managed verify_run deliberately locks its command list. A real shell
        # check can still run; it must not satisfy a different managed command.
        ("managed", "shell_run"),
    ],
)
def test_real_turn_distinguishes_recommended_and_required_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
    tool: str,
    one_shot: bool,
) -> None:
    for prefix in ("ALYSIS", "ALYSIS"):
        for setting in ("VERIFY_SANDBOX_MODE", "SHELL_SANDBOX_MODE"):
            monkeypatch.delenv(f"{prefix}_{setting}", raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr(
        NoopSurface,
        "request_approval",
        lambda self, request: ApprovalDecision(request.kind in {"verify_run", "shell_run"}),
    )
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'authority-fixture'\nversion = '0.1.0'\n"
    )
    (root / "logic.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_logic.py").write_text(
        "import unittest\nfrom logic import add\n\n"
        "class AddTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n"
    )
    expected_commands = [] if authority == "inferred" else [_REQUIRED]
    known_commands = [_INFERRED] if authority == "inferred" else [_REQUIRED]
    assert not _matching_effective_verification_commands(
        observed_command=_SELECTED, effective_verification_commands=known_commands
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
        content="Fixed addition in logic.py. The local tests passed.", tool_calls=[], raw={}
    )
    responses = [
        _run(tool, "baseline", _SELECTED),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="fix", name="fs_write", arguments={"path": "logic.py", "content": _FIXED}
                )
            ],
            raw={},
        ),
        _run(tool, "selected-after-fix", _SELECTED),
        final,
    ]
    # The established one-shot gate requires explicit obligations to finish.
    # Supplying the real required execution also checks that it can converge.
    if authority != "inferred" and one_shot:
        responses.extend([_run(tool, "required-after-nudge", _REQUIRED), final])
    clients: list[_ScriptedClient] = []

    def create_client(**kwargs: Any) -> _ScriptedClient:
        client = _ScriptedClient(responses, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(session_mod, "_make_session_llm_client", create_client)
    cfg = AppConfig(
        model="offline-authority-model",
        routing_mode="code_only",
        skills_enabled=False,
        extra_fields={
            "verify_sandbox": {"mode": "off"},
            "shell_sandbox": {"mode": "off", "clear_env": False},
        },
    )
    kwargs: dict[str, Any] = {}
    if authority == "cli":
        kwargs["verify_cmd"] = [_REQUIRED]
    elif authority == "config":
        cfg.verify_commands = [_REQUIRED]
    elif authority == "managed":
        kwargs["authoritative_verification_commands"] = [_REQUIRED]
    session = session_mod.create_session(
        cfg=cfg,
        root=root,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="unused-offline-key",
        one_shot_execution=one_shot,
        session_log_dir_override=tmp_path / "sessions",
        surface=NoopSurface(),
        enable_compaction=False,
        subagents_enabled=False,
        **kwargs,
    )
    try:
        assert session.effective_verification_commands == known_commands
        assert (
            session.verification_selection_source
            == {
                "inferred": "repo_scan.likely_test_commands",
                "cli": "cli.verify_cmd",
                "config": "config.verify_commands",
                "managed": "environment.authoritative_verification_commands",
            }[authority]
        )
        exit_code = session.run_turn("Fix add in logic.py so it returns the correct sum.")
        assert session.verification_authoritative is (authority != "inferred")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(clients) == 1
    client = clients[0]
    assert not client.responses
    assert (root / "logic.py").read_text() == _FIXED
    environment = next(
        item["content"]
        for item in client.requests[0]["messages"]
        if isinstance(item.get("content"), str)
        and item["content"].startswith("<environment_context>")
    )
    flag = "true" if authority != "inferred" else "false"
    assert f"verification_authoritative: {flag}" in environment
    assert f"verification_commands_authoritative: {flag}" in environment
    command_field = (
        "authoritative_verification_commands"
        if authority != "inferred"
        else "recommended_verification_commands"
    )
    assert f"{command_field}: {json.dumps(known_commands)}" in environment

    runs = [item for item in snapshots if item["tool"] == tool]
    baseline = runs[0]
    baseline_row = (
        baseline["result"]["command_results"][0] if tool == "verify_run" else baseline["result"]
    )
    assert baseline_row["exit_code"] == 1
    assert baseline["state"]["last_verification_passed"] is False
    selected = runs[1]["state"]
    assert selected["last_verification_passed"] is True
    assert selected["expected_verification_commands"] == expected_commands
    assert selected["missing_verification_commands"] == expected_commands
    # A physically passing alternative is not proof that the other selection
    # ran, irrespective of whether that other selection is merely recommended.
    assert selected["covered_verification_commands"] == []
    if authority == "inferred":
        # It is useful independent evidence when no command is mandatory.
        # This must not trigger a supplemental-only finalization nudge.
        assert len(selected["accepted_verification_evidence"]) == 1
        assert selected["accepted_verification_evidence"][0]["normalized_command"] == _SELECTED
        assert not selected["supplemental_verification_evidence"]
    else:
        assert selected["accepted_verification_evidence"] == []
        assert selected["supplemental_verification_evidence"]
    events = list(read_session_events(log_path))
    nudges = [
        event["payload"]
        for event in events
        if event["type"] in {"completion_gate_nudge", "one_shot_completion_gate_nudge"}
    ]
    if authority != "inferred" and one_shot:
        assert len(runs) == 3
        assert any("verification_incomplete" in event["problems"] for event in nudges)
        assert runs[-1]["state"]["missing_verification_commands"] == []
        assert runs[-1]["state"]["covered_verification_commands"] == [_REQUIRED]
    else:
        assert len(runs) == 2
        assert all(not event["problems"] for event in nudges)
    assert len([event for event in events if event["type"] == "final"]) == 1


@pytest.mark.parametrize(
    ("source", "contract_type"),
    [
        ("repo_scan.likely_test_commands", "repo_native"),
        ("verification_fallback.detected_runner", "selected"),
        ("task_refinement.node_test", "task_inferred"),
        ("task_refinement.doctest", "task_inferred"),
        ("config.verify_commands_fallback", "generic_fallback"),
        ("config.verify_commands_generic_preset", "generic_fallback"),
    ],
)
def test_inferred_specs_do_not_gain_authority_from_legacy_contract_labels(
    source: str, contract_type: str
) -> None:
    spec = build_verification_command_spec(_INFERRED, source=source, contract_type=contract_type)
    assert spec.provenance == VerificationCommandProvenance.INFERRED_HEURISTIC
    assert spec.requirement == VerificationCommandRequirement.ADVISORY


@pytest.mark.parametrize(
    "different_command",
    [
        f"{_SELECTED} -k test_add",
        f"{_SELECTED} -p test_other.py",
        f"cd tests && {_SELECTED}",
        f"TEST_MODE=alternate {_SELECTED}",
    ],
    ids=["selector", "file-pattern", "cwd", "environment"],
)
def test_authority_change_does_not_weaken_command_identity(different_command: str) -> None:
    assert _matching_effective_verification_commands(
        observed_command=_SELECTED, effective_verification_commands=[_SELECTED]
    ) == {_SELECTED}
    assert not _matching_effective_verification_commands(
        observed_command=different_command, effective_verification_commands=[_SELECTED]
    )
