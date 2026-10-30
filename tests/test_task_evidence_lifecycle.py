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
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface


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
    class EvidenceSurface(NoopSurface):
        def request_approval(self, request):
            return ApprovalDecision(request.kind == "shell_run")

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
        surface=EvidenceSurface(),
    )
    return session, command


@pytest.mark.parametrize("default_output", [False, True])
def test_nested_project_interpretation_writes_and_acceptance_use_the_same_exact_path(
    tmp_path, monkeypatch, default_output
):
    import subprocess

    from alysis_code.agent.task_artifact_binding import declared_changed_output_paths

    repo = tmp_path / "repo"
    project = repo / "module"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    (repo / "keep.txt").write_text("parent original")
    (project / "keep.txt").write_text("project original")
    if default_output:
        monkeypatch.setenv("ALYSIS_DESKTOP_TASK_OUTPUT_DIRECTORY", "outputs/current")
    session, _ = _session(project, monkeypatch, checks=False)
    instruction = "Create note.txt and preserve keep.txt unchanged."
    relative = "outputs/current/note.txt" if default_output else "note.txt"
    try:
        session.client = _ScriptedClient(
            (
                "task_artifacts",
                {
                    "paths": [
                        {
                            "directory": "",
                            "name": "note.txt",
                            "role": "required_output",
                            "location": "task_output" if default_output else "workspace",
                            "path_base": "active_workdir",
                            "evidence_quote": instruction,
                        },
                        {
                            "directory": "",
                            "name": "keep.txt",
                            "role": "preservation_target",
                            "location": "workspace",
                            "path_base": "active_workdir",
                            "evidence_quote": instruction,
                        },
                    ]
                },
            ),
            ("fs_write", {"path": relative, "content": "new note\n"}),
            ("fs_read", {"path": relative}),
        )
        session.run_turn(instruction)
        contract = session._turn_execution_state.acceptance_contract
        assert contract.path_refs[0].workspace_relative_path == f"module/{relative}"
        assert contract.path_refs[1].workspace_relative_path == "module/keep.txt"
        assert (project / relative).read_text() == "new note\n"
        assert not (repo / relative).exists()
        assert (repo / "keep.txt").read_text() == "parent original"
        assert (project / "keep.txt").read_text() == "project original"
        assert declared_changed_output_paths(
            session._turn_execution_state, relative_to="module"
        ) == [relative]
        assert not contract.problem_names()
    finally:
        session.close()


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


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("later_code_edit", [False, True])
def test_inferred_checks_follow_current_turn_edits_without_resetting_task_authority(
    tmp_path, monkeypatch, resume, later_code_edit
):
    (tmp_path / "source.py").write_text("value = 1\n")
    checker = "from source import value\nassert value in (2, 3)\n"
    (tmp_path / "check.py").write_text(checker)
    session, command = _session(tmp_path, monkeypatch)
    try:
        session.client = _ScriptedClient(
            ("fs_read", {"path": "source.py"}),
            ("fs_write", {"path": "source.py", "content": "value = 2\n"}),
            ("shell_run", {"cmd": command}),
        )
        session.run_turn("Update source.py to set value to 2. Preserve check.py unchanged.")
        assert session.verification_contract_type == "repo_native"
        assert session.last_turn_outcome["verified_success"]
        task_id = session._task_evidence_state.task_id
        snapshot = session._task_evidence_state.snapshot
        if resume:
            recovered = session.store.events_snapshot()
            old_session = session
            session, _ = _session(tmp_path, monkeypatch, name="resumed")
            restore_conversation_evidence(
                session, recovered, source_session_id=old_session.store.session_id
            )
            old_session.close()
        instruction = (
            "Update source.py to set value to 3. Preserve check.py unchanged."
            if later_code_edit
            else "Create note.txt and inventory.csv. Keep every existing file unchanged."
        )
        calls = (
            [
                ("fs_read", {"path": "source.py"}),
                ("fs_write", {"path": "source.py", "content": "value = 3\n"}),
            ]
            if later_code_edit
            else [
                (
                    "task_artifacts",
                    {
                        "paths": [
                            {
                                "directory": "",
                                "name": name,
                                "role": "required_output",
                                "evidence_quote": instruction,
                            }
                            for name in ("note.txt", "inventory.csv")
                        ]
                    },
                ),
                ("fs_write", {"path": "note.txt", "content": "A new note.\n"}),
                ("fs_read", {"path": "note.txt"}),
                ("fs_write", {"path": "inventory.csv", "content": "item,count\nmap,2\n"}),
                ("fs_read", {"path": "inventory.csv"}),
            ]
        )
        session.client = _ScriptedClient(*calls)
        event_start = len(session.store.events_snapshot())
        session.run_turn(instruction)
        state = session._turn_execution_state
        assert session._task_evidence_state.task_id == task_id
        assert session._task_evidence_state.snapshot == snapshot
        assert "source.py" in state.touched_repo_paths
        assert (tmp_path / "check.py").read_text() == checker
        assert state.verification_attempt_count == 0
        if later_code_edit:
            assert state.turn_touched_repo_paths == {"source.py"}
            assert not session.last_turn_outcome["verified_success"]
            assert "verification_not_attempted" in session.last_turn_outcome["problems"]
        else:
            from alysis_code.agent.task_artifact_binding import declared_changed_output_paths

            assert state.turn_touched_repo_paths == {"note.txt", "inventory.csv"}
            assert session.last_turn_outcome["verified_success"], session.last_turn_outcome
            assert session.last_turn_outcome["problems"] == []
            assert declared_changed_output_paths(state) == ["inventory.csv", "note.txt"]
            assert session.client.serial == len(calls) + 1
            assert not any(
                event["type"] == "completion_gate_nudge"
                for event in session.store.events_snapshot()[event_start:]
            )
    finally:
        session.close()


def test_real_turn_output_declaration_matches_final_task_and_does_not_recycle_prior_paths(
    tmp_path, monkeypatch
):
    from alysis_code.agent.task_artifact_binding import declared_changed_output_paths

    session, _ = _session(tmp_path, monkeypatch, checks=False)
    instruction = "Create inventory.csv."
    try:
        session.client = _ScriptedClient(
            (
                "task_artifacts",
                {
                    "paths": [
                        {
                            "directory": "",
                            "name": "inventory.csv",
                            "role": "required_output",
                            "evidence_quote": instruction,
                        }
                    ]
                },
            ),
            ("fs_write", {"path": "inventory.csv", "content": "item,total\nbooks,7\n"}),
            ("fs_read", {"path": "inventory.csv"}),
        )
        assert session.run_turn(instruction) == 0
        state = session._turn_execution_state
        assert state.acceptance_contract.task_id == session.last_turn_outcome["task_id"]
        assert session.last_turn_outcome["terminal"] is True
        assert declared_changed_output_paths(state) == ["inventory.csv"]
        session.client = _ScriptedClient(("fs_read", {"path": "inventory.csv"}))
        session.run_turn("Read inventory.csv and explain its contents. Do not change files.")
        assert session._turn_execution_state.turn_touched_repo_paths == set()
        assert declared_changed_output_paths(session._turn_execution_state) == []
    finally:
        session.close()


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
