from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from alysis_code import cli as cli_mod
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMError, LLMResponse, ToolCall
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface


def _session(root: Path, command: str, session_id: str, *, one_shot: bool = True):
    class CheckpointSurface(NoopSurface):
        def request_approval(self, request):
            return ApprovalDecision(request.kind == "shell_run")

    return create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", verify_commands=[command]),
        root=root,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=one_shot,
        session_log_dir_override=root / "logs",
        session_id_override=session_id,
        surface=CheckpointSurface(),
    )


class _Client:
    model = "test-model"
    temperature = 0.2

    def __init__(self, calls: list[ToolCall], *, provider_failure: bool = False):
        self.calls = list(calls)
        self.provider_failure = provider_failure

    def chat(self, **kwargs):
        if self.calls:
            return LLMResponse(content="", tool_calls=[self.calls.pop(0)], raw={})
        if self.provider_failure:
            raise LLMError("Provider unavailable: connection refused after retries")
        return LLMResponse(
            content="The requested change is complete and checks passed.", tool_calls=[], raw={}
        )


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.parametrize("prior_edit", [False, True])
def test_passing_baseline_without_requested_edit_cannot_be_a_verified_checkpoint(
    tmp_path, monkeypatch, one_shot, prior_edit
):
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    (tmp_path / "deliverable.py").write_text('print("old behavior")\n')
    (tmp_path / "check.py").write_text('print("baseline check passed")\n')
    command = '"' + sys.executable.replace("\\", "/") + '" check.py'
    session = _session(tmp_path, command, "checkpoint-no-edit-review", one_shot=one_shot)
    try:
        if prior_edit:
            session.client = _Client(
                [
                    ToolCall(
                        id="earlier",
                        name="fs_write",
                        arguments={"path": "notes.txt", "content": "earlier work"},
                    )
                ]
            )
            session.run_turn("Create notes.txt.")
        session.client = _Client(
            [ToolCall(id="check", name="shell_run", arguments={"cmd": command})]
        )
        session.run_turn(
            'Change deliverable.py to print "Hello production" instead of its old behavior, '
            f"then run `{command}`."
        )
        outcome = session.last_turn_outcome
        assert not outcome["verified_success"]
        assert "no_material_edits" in outcome["problems"]
        assert "best_verified_checkpoint" not in outcome
        assert not any(
            event["type"] == "anytime_checkpoint"
            and event["payload"].get("status") == "verified_checkpoint_preserved"
            for event in session.store.events_snapshot()
        )
        assert (tmp_path / "deliverable.py").read_text() == 'print("old behavior")\n'
    finally:
        session.close()


def test_cli_resume_restores_checkpoint_identity_and_keeps_good_bytes_after_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    (tmp_path / "check.py").write_text("import deliverable\nassert deliverable.value == 7\n")
    command = '"' + sys.executable.replace("\\", "/") + '" check.py'
    original = _session(tmp_path, command, "checkpoint-resume-origin")
    original.client = _Client(
        [
            ToolCall(
                id="write",
                name="fs_write",
                arguments={"path": "deliverable.py", "content": "value = 7\n"},
            ),
            ToolCall(id="check", name="shell_run", arguments={"cmd": command}),
        ]
    )
    try:
        original.run_turn(f"Implement deliverable.py with value 7 and verify using `{command}`.")
        checkpoint = dict(original.last_turn_outcome["best_verified_checkpoint"])
        task_id = original._task_evidence_state.task_id
    finally:
        original.close()

    resumed = _session(tmp_path, command, "checkpoint-resume-current")
    try:
        ok, message, history = cli_mod._resume_chat_session(
            session=resumed, target_session_id="checkpoint-resume-origin"
        )
        assert ok, message
        assert history
        assert resumed._task_evidence_state.task_id == task_id
        assert resumed.store.session_id == "checkpoint-resume-origin"
        resumed.client = _Client(
            [
                ToolCall(
                    id="worse",
                    name="fs_write",
                    arguments={"path": "deliverable.py", "content": "value = 999\n"},
                )
            ],
            provider_failure=True,
        )
        with pytest.raises(LLMError, match="Provider unavailable"):
            resumed.run_turn(f"Implement deliverable.py with value 7 and verify using `{command}`.")
        outcome = resumed.last_turn_outcome
        assert not outcome["verified_success"]
        assert outcome["task_id"] == task_id
        assert outcome["best_verified_checkpoint"] == checkpoint
        with zipfile.ZipFile(checkpoint["archive_path"]) as archive:
            assert archive.read("deliverable.py") == b"value = 7\n"
        assert (tmp_path / "deliverable.py").read_text() == "value = 999\n"
    finally:
        resumed.close()
