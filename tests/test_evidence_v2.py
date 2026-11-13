from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alysis_code.agent.completion_certificate import (
    CompletionCertificateInput,
    evaluate_completion_certificate,
)
from alysis_code.agent.verification import (
    EVIDENCE_REPAIR_ROUND_BOUND,
    TurnExecutionState,
    _completion_gate_nudge_message,
    _completion_gate_problems,
)
from alysis_code.agent.verification_evidence import (
    VerificationEvidenceCategory,
    _evidence_v2_enabled,
    classify_verification_evidence,
    command_is_qualifying_execution_evidence,
)
from alysis_code.config import AppConfig, ConfigError, set_config_value
from alysis_code.pipeline_facts import PIPELINE_STATUS_SENTINEL
from alysis_code.tools import shell as shell_mod
from alysis_code.tools.shell import shell_run

# ---------------------------------------------------------------------------
# Change A: shell layer PIPESTATUS capture (fact capture; semantics unchanged)
# ---------------------------------------------------------------------------


class _FakeRunner:
    """A ShellRunner stub. When it receives the capture wrapper it emulates bash
    by appending the PIPESTATUS sentinel to stderr with a configured status."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "out\n",
        stderr: str = "err\n",
        pipestatus: list[int] | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.pipestatus = pipestatus
        self.commands: list[str] = []

    def run(self, *, root: Path, cwd: Path, cmd: str, timeout_s: int):
        self.commands.append(cmd)
        stderr = self.stderr
        if "command -v bash" in cmd and self.pipestatus is not None:
            stderr = (
                self.stderr
                + PIPELINE_STATUS_SENTINEL
                + " ".join(str(code) for code in self.pipestatus)
                + "\n"
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=self.returncode, stdout=self.stdout, stderr=stderr
        )


@pytest.fixture(autouse=True)
def _force_posix_capture(monkeypatch):
    # Exercise the POSIX capture path even on a Windows test host.
    monkeypatch.setattr(shell_mod, "_pipeline_capture_platform_ok", lambda: True)


def test_plain_command_has_single_element_status(tmp_path: Path) -> None:
    runner = _FakeRunner(returncode=0, stdout="hi\n", stderr="")
    result = shell_run(root=tmp_path, cmd="echo hi", runner=runner, capture_pipeline_status=True)
    assert result["pipeline_stage_status"] == [0]
    assert result["pipeline_stages"] == ["echo hi"]
    # A plain command is not wrapped.
    assert all("command -v bash" not in c for c in runner.commands)


def test_piped_command_captures_pipestatus(tmp_path: Path) -> None:
    runner = _FakeRunner(returncode=0, stdout="", stderr="boom\n", pipestatus=[1, 0])
    result = shell_run(
        root=tmp_path,
        cmd="pytest -x foo | tail -40",
        runner=runner,
        capture_pipeline_status=True,
    )
    # Per-stage status observed; overall exit code stays the last stage's (0).
    assert result["pipeline_stage_status"] == [1, 0]
    assert result["exit_code"] == 0
    # The sentinel is stripped from user-visible stderr.
    assert PIPELINE_STATUS_SENTINEL not in result["stderr"]
    assert result["stderr"] == "boom\n"
    assert any("command -v bash" in c for c in runner.commands)


def test_multistage_pipeline_captures_all_stages(tmp_path: Path) -> None:
    runner = _FakeRunner(returncode=0, stdout="", stderr="", pipestatus=[2, 0, 0])
    result = shell_run(
        root=tmp_path,
        cmd="pytest | grep -i fail | tail -1",
        runner=runner,
        capture_pipeline_status=True,
    )
    assert result["pipeline_stage_status"] == [2, 0, 0]


def test_capture_disabled_leaves_plain_status(tmp_path: Path) -> None:
    runner = _FakeRunner(returncode=0, stdout="hi\n", stderr="")
    result = shell_run(root=tmp_path, cmd="echo hi", runner=runner, capture_pipeline_status=False)
    assert result["pipeline_stage_status"] == [0]
    assert all("command -v bash" not in c for c in runner.commands)


def test_pipeline_without_capture_leaves_status_unknown(tmp_path: Path) -> None:
    runner = _FakeRunner(returncode=0, stdout="", stderr="")
    result = shell_run(
        root=tmp_path,
        cmd="pytest | tail -5",
        runner=runner,
        capture_pipeline_status=False,
    )
    assert result["pipeline_stage_status"] is None


# ---------------------------------------------------------------------------
# Change B: fact-based evidence classifier
# ---------------------------------------------------------------------------


def test_piped_passing_pytest_counts_as_pass_evidence() -> None:
    evidence = classify_verification_evidence(
        "pytest -x foo | tail -40",
        exit_code=0,
        output="2 passed\n",
        stage_status=[0, 0],
    )
    assert evidence.category == VerificationEvidenceCategory.REPO_NATIVE
    assert evidence.allowed_to_satisfy_contract is True
    assert evidence.evidence_verdict == "pass"


def test_piped_failing_pytest_counts_as_fail_evidence() -> None:
    evidence = classify_verification_evidence(
        "pytest -x foo | tail -40",
        exit_code=0,  # tail masks the real code
        output="1 failed\n",
        stage_status=[1, 0],
    )
    # Still evidence (of a failure): a verification attempt that did not pass.
    assert evidence.category == VerificationEvidenceCategory.REPO_NATIVE
    assert evidence.allowed_to_satisfy_contract is False
    assert evidence.evidence_verdict == "fail"


def test_piped_pytest_unknown_stage_status_is_unverified() -> None:
    evidence = classify_verification_evidence(
        "pytest -x foo | tail -40",
        exit_code=0,
        output="2 passed\n",
        stage_status=None,
    )
    assert evidence.category == VerificationEvidenceCategory.NOT_VERIFICATION
    assert evidence.reason == "pipeline_stage_status_unavailable"
    assert evidence.evidence_verdict == "unavailable"


def test_piped_contract_command_matches_on_first_stage() -> None:
    evidence = classify_verification_evidence(
        "pytest -q | tail -40",
        known_verification_commands=["pytest -q"],
        authoritative=True,
        output="1 passed\n",
        stage_status=[0, 0],
    )
    assert evidence.category == VerificationEvidenceCategory.AUTHORITATIVE
    assert evidence.allowed_to_satisfy_contract is True
    assert evidence.covered_verification_commands == ("pytest -q",)


def test_syntax_only_piped_command_never_qualifies() -> None:
    evidence = classify_verification_evidence(
        "python -c 'import ast; ast.parse(open(\"x.py\").read())' | tail -1",
        exit_code=0,
        output="",
        stage_status=[0, 0],
    )
    assert evidence.category == VerificationEvidenceCategory.NOT_VERIFICATION
    assert evidence.evidence_verdict == "not_verification"


@pytest.mark.parametrize(
    "command,expected",
    [
        ("pytest -q", True),
        ("pytest -q | tail -40", True),
        ("go test ./...", True),
        ("python -c 'import ast; ast.parse(src)'", False),
        ("python -m py_compile foo.py", False),
        ("ruff check src", False),
        ("mypy src", False),
        ("echo ok", False),
    ],
)
def test_command_is_qualifying_execution_evidence(command: str, expected: bool) -> None:
    assert command_is_qualifying_execution_evidence(command) is expected


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


def test_kill_switch_env_and_config(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_EVIDENCE_V2", raising=False)
    assert _evidence_v2_enabled(AppConfig(model="x", evidence_v2_enabled=True)) is True
    assert _evidence_v2_enabled(AppConfig(model="x", evidence_v2_enabled=False)) is False
    monkeypatch.setenv("ALYSIS_EVIDENCE_V2", "off")
    assert _evidence_v2_enabled(AppConfig(model="x", evidence_v2_enabled=True)) is False
    monkeypatch.setenv("ALYSIS_EVIDENCE_V2", "on")
    assert _evidence_v2_enabled(AppConfig(model="x", evidence_v2_enabled=False)) is True


def test_kill_switch_config_key_roundtrip() -> None:
    cfg = AppConfig(model="x")
    assert cfg.evidence_v2_enabled is True
    set_config_value(cfg, "evidence_v2_enabled", "off")
    assert cfg.evidence_v2_enabled is False
    set_config_value(cfg, "evidence_v2_enabled", "true")
    assert cfg.evidence_v2_enabled is True
    with pytest.raises(ConfigError):
        set_config_value(cfg, "evidence_v2_enabled", "maybe")


def test_kill_switch_reverts_classifier_to_legacy() -> None:
    facts = dict(exit_code=0, output="2 passed\n", stage_status=[0, 0])
    v2 = classify_verification_evidence("pytest -q | tail -40", evidence_v2=True, **facts)
    legacy = classify_verification_evidence("pytest -q | tail -40", evidence_v2=False, **facts)
    assert v2.category == VerificationEvidenceCategory.REPO_NATIVE
    assert legacy.category == VerificationEvidenceCategory.NOT_VERIFICATION
    assert legacy.reason == "unsafe_pipeline"


# ---------------------------------------------------------------------------
# Change C: ordering rule (post-edit execution evidence)
# ---------------------------------------------------------------------------


def _certificate_problems(**overrides):
    base = dict(
        contract=None,
        final_text="done",
        blocked=False,
        blocker_valid=False,
        material_edit_count=1,
        require_material_result=True,
        verification_expected=False,
        verification_attempt_count=0,
        last_verification_passed=None,
    )
    base.update(overrides)
    return evaluate_completion_certificate(CompletionCertificateInput(**base)).problems


def test_ordering_rule_blocks_when_no_post_edit_evidence() -> None:
    problems = _certificate_problems(
        execution_evidence_required=True,
        post_edit_execution_evidence_present=False,
        verification_attempt_count=0,
    )
    assert "verification_not_attempted" in problems


def test_ordering_rule_clears_with_post_edit_evidence() -> None:
    problems = _certificate_problems(
        execution_evidence_required=True,
        post_edit_execution_evidence_present=True,
        verification_attempt_count=1,
    )
    assert "verification_not_attempted" not in problems
    assert "verification_incomplete" not in problems


def test_ordering_rule_incomplete_when_prior_attempt_but_stale() -> None:
    problems = _certificate_problems(
        execution_evidence_required=True,
        post_edit_execution_evidence_present=False,
        verification_attempt_count=2,  # ran before the last edit
    )
    assert "verification_incomplete" in problems


def test_ordering_rule_exempts_turns_without_the_requirement() -> None:
    problems = _certificate_problems(
        execution_evidence_required=False,
        post_edit_execution_evidence_present=False,
    )
    assert "verification_not_attempted" not in problems
    assert "verification_incomplete" not in problems


def _source_edit(state: TurnExecutionState) -> None:
    state.note_material_edit()
    state.note_verification_relevant_edit()


def test_state_post_edit_evidence_ordering() -> None:
    # edit -> execute -> finalize: present
    state = TurnExecutionState(execution_requested=True)
    _source_edit(state)
    state.note_qualifying_execution_evidence()
    assert state.has_post_edit_execution_evidence() is True

    # ... then another source edit without a rerun: absent
    _source_edit(state)
    assert state.has_post_edit_execution_evidence() is False

    # ... rerun after the edit: present again
    state.note_qualifying_execution_evidence()
    assert state.has_post_edit_execution_evidence() is True

    # ... a docs-only edit (not verification-relevant) does not re-open it
    state.note_material_edit()
    assert state.has_post_edit_execution_evidence() is True


def test_no_edit_turn_has_no_post_edit_requirement() -> None:
    state = TurnExecutionState(execution_requested=True)
    state.note_qualifying_execution_evidence()
    # No material edits recorded -> the ordering rule does not apply.
    assert state.has_post_edit_execution_evidence() is False


def test_completion_gate_problems_ordering_rule_integration() -> None:
    # Ran tests, then edited source again without re-running: the ordering rule
    # flags it even though a prior verification passed (independent of coverage).
    state = TurnExecutionState(execution_requested=True)
    state.verification_attempt_count = 1
    state.last_verification_passed = True
    state.note_qualifying_execution_evidence()  # pre-edit run
    _source_edit(state)
    state.touched_repo_paths.add("src/app.py")

    problems = _completion_gate_problems(
        state=state,
        final_text="Everything matches the spec.",
        blocked=False,
        verification_expected=True,
        evidence_v2=True,
        turn_intent="execute",
    )
    assert "verification_incomplete" in problems

    # Re-run tests after the edit -> deficit cleared.
    state.verification_attempt_count = 2
    state.note_qualifying_execution_evidence()
    cleared = _completion_gate_problems(
        state=state,
        final_text="Everything matches the spec.",
        blocked=False,
        verification_expected=True,
        evidence_v2=True,
        turn_intent="execute",
    )
    assert "verification_incomplete" not in cleared
    assert "verification_not_attempted" not in cleared


def test_ordering_rule_off_under_kill_switch() -> None:
    state = TurnExecutionState(execution_requested=True)
    state.verification_attempt_count = 1
    state.last_verification_passed = True
    state.note_qualifying_execution_evidence()
    _source_edit(state)
    state.touched_repo_paths.add("src/app.py")
    problems = _completion_gate_problems(
        state=state,
        final_text="done",
        blocked=False,
        verification_expected=True,
        evidence_v2=False,
        turn_intent="execute",
    )
    assert "verification_incomplete" not in problems


# ---------------------------------------------------------------------------
# Change D: nag loop names the missing fact; prose cannot clear
# ---------------------------------------------------------------------------


def test_nudge_names_the_missing_fact_and_demands_a_run() -> None:
    message = _completion_gate_nudge_message(
        ["verification_not_attempted"],
        execution_evidence_missing_detail="after your edit to src/app.py (step 12)",
    )
    assert "No test execution recorded after your edit to src/app.py (step 12)" in message
    assert "A written explanation cannot clear this" in message
    # Not advisory when it is an evidence deficit.
    assert "this checklist is advisory" not in message


def test_nudge_without_detail_stays_advisory() -> None:
    message = _completion_gate_nudge_message(["verification_not_attempted"])
    assert "this checklist is advisory" in message
    assert "A written explanation cannot clear this" not in message


def test_evidence_repair_bound_is_small() -> None:
    assert EVIDENCE_REPAIR_ROUND_BOUND == 2


# ---------------------------------------------------------------------------
# Prompt byte-immutability with the feature on/off
# ---------------------------------------------------------------------------


def test_prompt_bytes_identical_regardless_of_kill_switch(monkeypatch) -> None:
    from alysis_code.agent.prompt_context import _compose_session_system_prompt
    from alysis_code.agent_loop import SYSTEM_PROMPT

    def compose() -> str:
        return _compose_session_system_prompt(
            base_prompt=SYSTEM_PROMPT,
            trusted_prompt_append="",
            include_write_guidance=True,
            include_skill_discovery_guidance=True,
            include_skill_lifecycle_guidance=True,
            include_subagent_guidance=True,
            include_one_shot_guidance=True,
        )

    monkeypatch.setenv("ALYSIS_EVIDENCE_V2", "on")
    prompt_on = compose()
    monkeypatch.setenv("ALYSIS_EVIDENCE_V2", "off")
    prompt_off = compose()

    assert prompt_on.encode("utf-8") == prompt_off.encode("utf-8")
    # The kill-switch and evidence-v2 mechanics never leak into the prompt.
    assert "EVIDENCE_V2" not in prompt_on
    assert "evidence_v2" not in prompt_on
    assert "PIPESTATUS" not in prompt_on
