from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.acceptance_contract import AcceptanceCriterionKind, EvidenceOrigin
from alysis_code.agent.task_evidence import (
    clear_conversation_evidence,
    restore_conversation_evidence,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.llm.openai_compat import LLMError, LLMResponse, ToolCall


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, *calls: tuple[str, dict[str, Any]]):
        self.calls = list(calls)
        self.serial = 0

    def chat(self, **_: Any) -> LLMResponse:
        self.serial += 1
        if self.calls:
            name, arguments = self.calls.pop(0)
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id=f"call-{self.serial}", name=name, arguments=arguments)],
                raw={},
            )
        return LLMResponse(content="The requested work is complete.", tool_calls=[], raw={})


def _session(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "source",
    checkpoint: bool = False,
    checks: bool = True,
):
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    command = '"' + sys.executable.replace("\\", "/") + '" check.py'
    configured = {"verify_commands": [command]} if checks else {}
    cfg = AppConfig(model="test-model", routing_mode="code_only", **configured)
    cfg.anytime_checkpoint.enabled = checkpoint
    if checkpoint:
        cfg.anytime_checkpoint.objective_command = command
        cfg.anytime_checkpoint.max_candidates = 2
    session = create_session(
        cfg=cfg,
        root=root,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=True,
        session_log_dir_override=root / "logs",
        session_id_override=name,
    )
    return session, command


def _rewrite_checker(session, command):
    session.client = _ScriptedClient(
        ("fs_read", {"path": "check.py"}),
        ("fs_write", {"path": "check.py", "content": "raise SystemExit(0)\n"}),
    )
    session.run_turn(
        f"Run `{command}` and report the result. Preserve check.py unchanged.",
    )
    assert not session.last_turn_outcome["verified_success"]
    assert session.last_turn_outcome["generation"] == 1


@pytest.mark.parametrize("relation", ["continuation", "amendment"])
@pytest.mark.parametrize("resume", [False, True])
def test_rewritten_checker_cannot_become_authoritative_on_continuation_or_resume(
    tmp_path, monkeypatch, relation, resume
):
    (tmp_path / "check.py").write_text("raise SystemExit(3)\n")
    session, command = _session(tmp_path, monkeypatch)
    try:
        _rewrite_checker(session, command)
        original_id = session._task_evidence_state.task_id
        original_revision = session.last_turn_outcome["acceptance_revision"]
        original_snapshot = session._task_evidence_state.snapshot
        if resume:
            recovered = session.store.events_snapshot()
            old_session = session
            session, _ = _session(tmp_path, monkeypatch, name="resumed")
            restore_conversation_evidence(
                session,
                recovered,
                source_session_id=old_session.store.session_id,
            )
            old_session.close()
        session.client = _ScriptedClient(("shell_run", {"cmd": command}))
        session.run_turn(
            f"Run `{command}`. Preserve check.py unchanged."
            if relation == "continuation"
            else f"Run `{command}`. Preserve check.py and the logs directory unchanged.",
        )
        assert session._task_evidence_state.task_id == original_id
        assert session._task_evidence_state.snapshot == original_snapshot
        assert session.last_turn_outcome["generation"] == 1
        assert "check.py" in session.last_turn_outcome["material_paths"]
        assert not session.last_turn_outcome["verified_success"]
        contract = session._turn_execution_state.acceptance_contract
        assert any(
            item.kind == AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH
            and item.status.value == "FAILED"
            for item in contract.criteria
        )
        assert any(
            item.origin == EvidenceOrigin.SELF_AUTHORED
            for item in contract.evidence
            if item.command == command
        )
        assert contract.acceptance_revision != original_revision
    finally:
        session.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "foreign_writer", "unfinished_tool"])
def test_missing_or_untrusted_resumed_baseline_cannot_verify_current_checker(
    tmp_path, monkeypatch, damage
):
    (tmp_path / "check.py").write_text("raise SystemExit(0)\n")
    source, command = _session(tmp_path, monkeypatch)
    resumed = None
    try:
        source.client = _ScriptedClient(("shell_run", {"cmd": command}))
        source.run_turn(f"Run `{command}` and report the result.")
        assert source.last_turn_outcome["verified_success"]
        events = json.loads(json.dumps(source.store.events_snapshot()))
        if damage == "missing":
            events = [event for event in events if event["type"] != "session_task_evidence"]
        elif damage == "corrupt":
            next(event for event in reversed(events) if event["type"] == "session_task_evidence")[
                "payload"
            ]["snapshot"] = {}
        elif damage == "foreign_writer":
            next(event for event in reversed(events) if event["type"] == "session_task_evidence")[
                "session_id"
            ] = "another-session"
        else:
            events.append({"type": "tool_call", "payload": {"name": "fs_write"}})
        recovered = events
        resumed, _ = _session(tmp_path, monkeypatch, name="resumed")
        restore_conversation_evidence(
            resumed,
            recovered,
            source_session_id=source.store.session_id,
        )
        resumed.client = _ScriptedClient(("shell_run", {"cmd": command}))
        resumed.run_turn("Continue the task and run the check.")
        assert not resumed.last_turn_outcome["verified_success"]
        assert "acceptance_baseline_unavailable" in resumed.last_turn_outcome["problems"]
        assert not resumed._turn_execution_state.acceptance_contract.baseline_available
        assert not resumed._turn_execution_state.accepted_verification_evidence
    finally:
        source.close()
        if resumed:
            resumed.close()


def test_explicit_clear_resets_verification_baseline(tmp_path, monkeypatch):
    (tmp_path / "check.py").write_text("raise SystemExit(3)\n")
    session, command = _session(tmp_path, monkeypatch)
    try:
        _rewrite_checker(session, command)
        old_task = session._task_evidence_state.task_id
        clear_conversation_evidence(session)
        for _ in range(2):
            session.client = _ScriptedClient(("shell_run", {"cmd": command}))
            session.run_turn(
                f"Run `{command}` and report the result. Preserve check.py unchanged.",
            )
            assert session._task_evidence_state.task_id != old_task
            assert session.last_turn_outcome["verified_success"]
            assert session.last_turn_outcome["generation"] == 0
            assert not session.last_turn_outcome["material_paths"]
    finally:
        session.close()


@pytest.mark.parametrize("resume", [False, True])
def test_task_wide_generations_and_amended_requirements_scope_checkpoints(
    tmp_path, monkeypatch, resume
):
    (tmp_path / "check.py").write_text(
        'import json\nfrom pathlib import Path\nprint(json.dumps({"score": int(Path("score.txt").read_text())}))\n'
    )
    session, command = _session(tmp_path, monkeypatch, checkpoint=True)
    try:
        for score in (7, 3):
            calls = [("fs_write", {"path": "score.txt", "content": str(score)})]
            if score == 3:
                calls.insert(0, ("fs_read", {"path": "score.txt"}))
            session.client = _ScriptedClient(*calls, ("shell_run", {"cmd": command}))
            session.run_turn(
                f"Write score.txt and run `{command}`.",
            )
            assert session.last_turn_outcome["verified_success"]
            assert session._anytime_checkpoints.best["objective_value"] == score
            if resume and score == 7:
                recovered = session.store.events_snapshot()
                owner = session.store.session_id
                original_snapshot = session._task_evidence_state.snapshot
                session.close()
                session, _ = _session(tmp_path, monkeypatch, name=owner, checkpoint=True)
                restore_conversation_evidence(session, recovered, source_session_id=owner)
                assert session._task_evidence_state.snapshot == original_snapshot
                assert session._acknowledged_checkpoint["objective_value"] == 7
        assert session.last_turn_outcome["generation"] == 2
        assert session._anytime_checkpoints.experiments_exhausted
        old_revision = session.last_turn_outcome["acceptance_revision"]
        session.client = _ScriptedClient()
        session.run_turn("Also create additional.txt.")
        assert session.last_turn_outcome["acceptance_revision"] != old_revision
        assert session._anytime_checkpoints.best is None
        assert not session._anytime_checkpoints.experiments_exhausted
        assert "best_verified_checkpoint" not in session.last_turn_outcome
    finally:
        session.close()


def test_evidence_ledger_write_failure_cannot_grant_authority(tmp_path, monkeypatch):
    (tmp_path / "check.py").write_text("raise SystemExit(0)\n")
    session, command = _session(tmp_path, monkeypatch)
    append = session.store.append

    def failing_append(event, payload, **kwargs):
        if event == "session_task_evidence":
            raise OSError("evidence volume unavailable")
        return append(event, payload, **kwargs)

    monkeypatch.setattr(session.store, "append", failing_append)
    try:
        session.client = _ScriptedClient(("shell_run", {"cmd": command}))
        session.run_turn(f"Run `{command}` and report the result.")
        assert not session.last_turn_outcome["verified_success"]
        assert "acceptance_baseline_unavailable" in session.last_turn_outcome["problems"]
    finally:
        session.close()


def test_early_deadline_after_prior_turn_keeps_task_generation_without_reusing_proof(
    tmp_path, monkeypatch
):
    (tmp_path / "check.py").write_text("raise SystemExit(3)\n")
    session, command = _session(tmp_path, monkeypatch)
    try:
        _rewrite_checker(session, command)
        clock = [0.0]
        session.execution_deadline = ExecutionDeadline.from_duration(1, clock=lambda: clock[0])
        clock[0] = 2
        session.client = _ScriptedClient()
        session.run_turn("Continue the task.")
        assert session.last_turn_outcome["outcome"] == "deadline_exceeded"
        assert session.last_turn_outcome["generation"] == 1
        assert session.last_turn_outcome["material_paths"] == ["check.py"]
        assert not session.last_turn_outcome["verified_success"]
        assert session.client.serial == 0
    finally:
        session.close()


@pytest.mark.parametrize("saved_parent", ["owner", "other-parent", None])
def test_resumed_child_evidence_requires_its_parent(tmp_path, saved_parent):
    from types import SimpleNamespace

    from alysis_code.agent.acceptance_contract import AcceptanceWorkspaceSnapshot
    from alysis_code.agent.task_evidence import TaskEvidenceState

    baseline = TaskEvidenceState(
        "evidence",
        str(tmp_path.resolve()),
        AcceptanceWorkspaceSnapshot(),
        parent_session_id=saved_parent,
    )
    session = SimpleNamespace(
        root=tmp_path,
        evidence_parent_session_id="owner",
        store=SimpleNamespace(append=lambda *args: None),
    )
    restore_conversation_evidence(
        session,
        [
            {
                "type": "session_task_evidence",
                "session_id": "child",
                "payload": baseline.to_payload(),
            }
        ],
        source_session_id="child",
    )
    assert session._task_evidence_state.baseline_available is (saved_parent == "owner")


def test_task_baseline_roundtrip_retains_entries_beyond_diagnostic_truncation(tmp_path):
    from types import SimpleNamespace

    from alysis_code.agent.acceptance_contract import AcceptanceWorkspaceSnapshot
    from alysis_code.agent.task_evidence import TaskEvidenceState, restore_task_evidence
    from alysis_code.verification_command_analysis import CheckerEntrypointFingerprint

    paths = frozenset(f"checks/check_{index}.py" for index in range(250))
    snapshot = AcceptanceWorkspaceSnapshot(
        preexisting_paths=paths,
        preexisting_checker_paths=paths,
        preexisting_checker_fingerprints=tuple(
            CheckerEntrypointFingerprint(path, str(tmp_path / path), True, 1, "0" * 64)
            for path in sorted(paths)
        ),
    )
    baseline = TaskEvidenceState("task", str(tmp_path.resolve()), snapshot)
    assert len(snapshot.as_payload()["preexisting_checker_fingerprints"]) == 200
    session = SimpleNamespace(
        root=tmp_path,
        store=SimpleNamespace(append=lambda *args: None),
    )
    restore_task_evidence(
        session, baseline.to_payload(), event_session_id="owner", expected_session_id="owner"
    )
    assert session._task_evidence_state.snapshot == snapshot
    assert session._task_evidence_state.baseline_available


@pytest.mark.parametrize("host_capped", [True, False])
def test_chat_only_provider_timeout_keeps_deadline_and_provider_failures_distinct(
    tmp_path, monkeypatch, host_capped
):
    clock = [0.0]
    session, _ = _session(tmp_path, monkeypatch)
    session.execution_deadline = ExecutionDeadline.from_duration(10, clock=lambda: clock[0])

    class Client:
        model = "test-model"
        temperature = 0.2
        timeout_s = 60 if host_capped else 1

        def chat(self, **kwargs):
            clock[0] = 10 if host_capped else 1
            raise LLMError("Normalized provider timeout") from TimeoutError("Read timed out")

    session.client = Client()
    try:
        if host_capped:
            assert session.run_turn("Explain this term.", chat_only=True) == 0
        else:
            with pytest.raises(LLMError):
                session.run_turn("Explain this term.", chat_only=True)
        assert session.last_turn_outcome["outcome"] == (
            "deadline_exceeded" if host_capped else "provider_failure"
        )
        assert not session.last_turn_outcome["verified_success"]
    finally:
        session.close()


def _last_final(session) -> str:
    finals = [e["payload"] for e in session.store.events_snapshot() if e["type"] == "final"]
    return str(finals[-1]["content"])


def test_a_lost_baseline_marks_later_work_but_not_a_reply_that_does_none(tmp_path, monkeypatch):
    # A turn stopped while a tool ran leaves its call logged and its evidence
    # unwritten, so the resumed chat no longer trusts the task's baseline.
    (tmp_path / "check.py").write_text("raise SystemExit(0)\n")
    # No configured check: the lost baseline is the only problem left to report.
    source, command = _session(tmp_path, monkeypatch, checks=False)
    resumed = None
    try:
        source.client = _ScriptedClient(("shell_run", {"cmd": command}))
        source.run_turn(f"Run `{command}` and report the result.")
        events = json.loads(json.dumps(source.store.events_snapshot()))
        events.append({"type": "tool_call", "payload": {"name": "shell_run"}})
        resumed, _ = _session(tmp_path, monkeypatch, name="resumed", checks=False)
        restore_conversation_evidence(resumed, events, source_session_id=source.store.session_id)

        # A reply that changes and checks nothing has nothing to verify.
        resumed.client = _ScriptedClient()
        resumed.run_turn("Reply with one word: back.")
        assert resumed.last_turn_outcome["problems"] == ["acceptance_baseline_unavailable"]
        assert "Not verified" not in _last_final(resumed)

        # Work done after the lost baseline still says it could not be verified.
        resumed.client = _ScriptedClient(("fs_write", {"path": "notes.txt", "content": "x\n"}))
        resumed.run_turn("Write notes.txt.")
        assert "acceptance_baseline_unavailable" in resumed.last_turn_outcome["problems"]
        assert "Not verified" in _last_final(resumed)
    finally:
        source.close()
        if resumed:
            resumed.close()
