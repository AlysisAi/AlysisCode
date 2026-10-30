from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from alysis_code import agent_loop
from alysis_code.agent.subagent_execution import _child_resume_messages
from alysis_code.agent.verification_result import verification_result_for_model
from alysis_code.compaction.tool_output_offload import ToolOutputOffloader
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.runtime_kind import RuntimeKind

pytestmark = pytest.mark.usefixtures("approved_local_checks")


def test_mixed_failures_different_effective_commands_and_unknown_fields_survive() -> None:
    result = {
        "commands": ["primary", "check"],
        "command_results": [
            {"command": "primary", "effective_command": "fallback", "exit_code": 0},
            {"command": "check", "effective_command": "check", "exit_code": 127},
        ],
        "all_passed": False,
        "verification_evidence_allowed": False,
        "primary_failure": {"command": "check", "snippet": "a real test failed"},
        "fallback_details": [{"primary": "primary", "effective": "fallback"}],
        "verification_command_specs": [
            {
                "command_id": "internal",
                "original_text": "check",
                "display_text": "different display",
                "provenance": "TASK_AUTHORED",
                "trust_level": "UNTRUSTED",
                "requirement": "REQUIRED",
                "working_directory": "other",
                "validation_status": "INVALID",
                "rejection_reason": "unsafe",
                "acceptance_criterion_ids": ["criterion"],
            }
        ],
        "verification_evidence_records": [
            {"matched_command": "contract", "covered_verification_commands": ["different"]}
        ],
        "new_future_field": {"detail": "must not silently disappear"},
    }
    original = copy.deepcopy(result)
    projected = verification_result_for_model(result)
    assert result == original
    assert projected["command_results"][0]["effective_command"] == "fallback"
    assert projected["command_results"][1]["exit_code"] == 127
    for key in (
        "all_passed",
        "verification_evidence_allowed",
        "primary_failure",
        "fallback_details",
        "verification_evidence_records",
        "new_future_field",
    ):
        assert projected[key] == original[key]
    assert projected["verification_command_specs"] == [
        {
            key: value
            for key, value in original["verification_command_specs"][0].items()
            if key != "command_id"
        }
    ]


class _InspectVerificationClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0
        self.visible: dict[str, Any] | None = None
        self.read_full_output = False

    def chat(self, *, messages: list[dict[str, Any]], **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="", tool_calls=[ToolCall("verify", "verify_run", {})], raw={}
            )
        latest = next(message for message in reversed(messages) if message.get("role") == "tool")
        payload = json.loads(latest["content"])
        if payload.get("offloaded"):
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        "read-json",
                        "session_artifact_read",
                        {"locator": payload["artifact_locator"]},
                    )
                ],
                raw={},
            )
        if "content" in payload and "tool_outputs/" in payload.get("locator", ""):
            payload = json.loads(json.loads(payload["content"])["content_json"])
        if "command_results" in payload:
            self.visible = payload
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        "read-output",
                        "session_artifact_read",
                        {"locator": payload["artifact_locator"]},
                    )
                ],
                raw={},
            )
        assert "END_REPORT" in payload["content"]
        self.read_full_output = True
        return LLMResponse(
            content="The configured unittest check passed. Its saved output contains END_REPORT.",
            tool_calls=[],
            raw={},
        )


@pytest.mark.parametrize("runtime_kind", [RuntimeKind.ONE_SHOT, RuntimeKind.INTERACTIVE_CHAT])
@pytest.mark.parametrize("offload", [False, True])
def test_real_verification_keeps_full_evidence_and_replays_concise_content(
    tmp_path: Path,
    runtime_kind: RuntimeKind,
    offload: bool,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "test_report.py").write_text(
        "import unittest\nclass Report(unittest.TestCase):\n"
        "    def test_report(self):\n        print('x' * 1200 + 'END_REPORT')\n"
        "        self.assertEqual(2 + 2, 4)\n"
    )
    executable = (
        subprocess.list2cmdline([Path(sys.executable).as_posix()])
        if os.name == "nt"
        else shlex.quote(sys.executable)
    )
    command = f"{executable} -m unittest discover -v"
    cfg = AppConfig(
        model="test-model",
        routing_mode="code_only",
        stream=False,
        skills_enabled=False,
        verify_commands=[command],
    )
    cfg.extra_fields["verify_sandbox"] = {"mode": "off"}
    session = agent_loop.create_session(
        cfg=cfg,
        root=root,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="offline-test-placeholder",
        runtime_kind=runtime_kind,
        one_shot_execution=runtime_kind == RuntimeKind.ONE_SHOT,
        session_log_dir_override=tmp_path / "sessions",
        session_id_override="projection",
        enable_compaction=False,
        enable_tool_output_offload=False,
        verify_cmd=[command],
    )
    client = _InspectVerificationClient()
    session.client = client  # type: ignore[assignment]
    if offload:
        session.tool_output_offloader = ToolOutputOffloader(
            artifact_layout=session.store.session_artifact_layout,
            workspace_root=root,
            threshold_chars=400,
            preview_chars=100,
        )
    try:
        assert session.run_turn("Run the configured check and read the full saved output.") == 0
        assert client.read_full_output and client.visible is not None
        visible = client.visible
        events = session.store.events_snapshot()
        event = next(
            e for e in events if e["type"] == "tool_result" and e["payload"]["name"] == "verify_run"
        )
        full = event["payload"]["result"]
        assert full["commands"] == [command]
        assert full["verification_command_specs"][0]["command_id"]
        assert full["command_results"][0]["effective_command"] == command
        assert full["command_results"][0]["host_test_report"]["counts_known"] is True
        assert "host_test_report" not in visible["command_results"][0]
        assert "commands" not in visible
        assert "command_id" not in visible["verification_command_specs"][0]
        for key in (
            "all_passed",
            "status",
            "verification_evidence_allowed",
            "verification_note",
            "artifact_locator",
        ):
            assert visible[key] == full[key]
        assert (
            visible["verification_command_specs"][0]["provenance"]
            == full["verification_command_specs"][0]["provenance"]
        )
        assert len(json.dumps(visible)) < len(json.dumps(full))
        restored = _child_resume_messages(events)
        replay = next(
            m for m in restored if m.get("role") == "tool" and m.get("tool_call_id") == "verify"
        )
        assert replay["content"] == event["payload"]["content"]
        assert full["verification_evidence_allowed"] is True
    finally:
        session.close()
