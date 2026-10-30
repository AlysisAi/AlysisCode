"""Turn-contract v2 (verification step 4): apply-don't-advise + spec literalism.

Mirrors the pure-logic + gate-integration style of ``test_evidence_v2.py`` and
``test_regression_baseline.py``. Every test here is pure or gate-level (no LLM);
the end-to-end marker/event wiring is exercised by one scripted-session test at
the bottom plus the advisory-completion assertions added to
``test_agent_loop_one_shot_follow_through.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.agent.acceptance_contract import (
    AcceptanceContract,
    build_acceptance_contract,
    extract_explicit_acceptance_commands,
)
from alysis_code.agent.completion_certificate import (
    CompletionCertificateInput,
    CompletionCertificateStatus,
    evaluate_completion_certificate,
)
from alysis_code.agent.turn_contract import (
    DEFAULT_ADVISORY_COMPLETION_EXPLANATION,
    MAX_EXPECTATIONS,
    AdvisoryCompletion,
    AdvisoryCompletionReason,
    DispositionRecord,
    Expectation,
    ExpectationDisposition,
    ExpectationKind,
    _turn_contract_v2_enabled,
    assess_expectations,
    build_advisory_completion_summary,
    build_unconfirmed_expectations_marker,
    coerce_advisory_reason,
    coerce_expectation_disposition,
    match_expectation_evidence,
    resolve_advisory_completion,
)
from alysis_code.agent.verification import (
    HONEST_UNVERIFIED_FINALIZATION_MARKER,
    TurnExecutionState,
    _completion_gate_problems,
    _completion_gate_repair_stage,
    _record_tool_effect,
)
from alysis_code.config import AppConfig, ConfigError, set_config_value

# ---------------------------------------------------------------------------
# Test 1: contract v2 schema — expectation extraction
# ---------------------------------------------------------------------------


def test_expected_output_literal_is_extracted(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="The display should show the expected output `{b^\\dagger_{0}}^{2}`.",
    )
    kinds = {(e.kind, e.text) for e in contract.expectations}
    assert (ExpectationKind.EXPECTED_OUTPUT, "{b^\\dagger_{0}}^{2}") in kinds


def test_named_locus_required_output_is_extracted(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Count the lines and save the result to out/answer.txt.",
    )
    loci = {e.text for e in contract.expectations if e.kind == ExpectationKind.NAMED_LOCUS}
    assert "out/answer.txt" in loci


def test_input_reference_is_not_extracted_as_named_locus(tmp_path: Path) -> None:
    # Regression guard: "count the lines in data.txt" names a read-only input, not
    # an edit site. It must NOT become a named_locus (which would demand editing it).
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Count the lines in data.txt and save the result to answer.txt.",
    )
    loci = {e.text for e in contract.expectations if e.kind == ExpectationKind.NAMED_LOCUS}
    assert "data.txt" not in loci
    assert "answer.txt" in loci


@pytest.mark.parametrize(
    "instruction",
    [
        "Give a concise explanation with an input/output example table and a Mermaid flowchart.",
        "Explain the read/write behavior with examples.",
        "Inspect src/output.py and report the findings.",
        "Inspect src/create.py and report the findings.",
    ],
)
def test_path_words_do_not_create_output_requirements(tmp_path: Path, instruction: str) -> None:
    contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert not any(e.kind == ExpectationKind.NAMED_LOCUS for e in contract.expectations)
    assert not any(ref.role.value == "required_output" for ref in contract.path_refs)


@pytest.mark.parametrize("path", ["input/output", "before/after", "src/output.py"])
def test_explicit_output_path_still_requires_an_artifact(tmp_path: Path, path: str) -> None:
    contract = build_acceptance_contract(root=tmp_path, instruction=f"Create the file `{path}`.")
    assert any(
        e.kind == ExpectationKind.NAMED_LOCUS and e.text == path for e in contract.expectations
    )


def test_backtick_command_is_not_expected_output(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Run `pytest -q` and `git status` to validate.",
    )
    outputs = {e.text for e in contract.expectations if e.kind == ExpectationKind.EXPECTED_OUTPUT}
    assert "pytest -q" not in outputs
    assert "git status" not in outputs


@pytest.mark.parametrize("separator", [" and ", ", ", ", and "])
def test_explicit_quoted_command_list_preserves_each_command(separator):
    instruction = f"Run `pytest -q`{separator}`git status` and `custom-check --strict`."
    assert extract_explicit_acceptance_commands(instruction) == [
        "pytest -q",
        "git status",
        "custom-check --strict",
    ]


@pytest.mark.parametrize(
    "tail",
    [
        "inspect `result.txt`",
        "write `out/result.txt`",
        "inspect `request.path`",
        "print `DONE OK`",
        "inspect `request.path` and `git status`",
    ],
)
def test_command_list_does_not_promote_intervening_prose_paths_or_symbols(tail):
    assert extract_explicit_acceptance_commands(f"Run `pytest -q` and {tail}.") == ["pytest -q"]


def test_negated_quoted_command_list_has_no_execution_authority():
    assert extract_explicit_acceptance_commands("Do not run `pytest -q` and `git status`.") == []


def test_bare_identifier_is_not_expected_output(tmp_path: Path) -> None:
    # `request.path` is a code reference, not a runtime-output literal.
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="The redirect should use `request.path` in the response.",
    )
    outputs = {e.text for e in contract.expectations if e.kind == ExpectationKind.EXPECTED_OUTPUT}
    assert "request.path" not in outputs


def test_empty_expectations_is_valid(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Please improve performance a bit.",
    )
    assert contract.expectations == []


def test_expectations_cap_is_enforced(tmp_path: Path) -> None:
    literals = " ".join(f"`OUTPUT literal number {i} here`" for i in range(20))
    contract = build_acceptance_contract(root=tmp_path, instruction=f"Show these: {literals}")
    assert len(contract.expectations) <= MAX_EXPECTATIONS


def test_acceptance_contract_payload_carries_expectations(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="The output must be `RESULT: 42 tokens counted` exactly.",
    )
    payload = contract.as_payload()
    assert "expectations" in payload
    assert any(item["kind"] == "expected_output" for item in payload["expectations"])
    assert all(
        {"id", "kind", "text", "source_quote"} <= set(item) for item in payload["expectations"]
    )


# ---------------------------------------------------------------------------
# Test 2: evidence linker (pure)
# ---------------------------------------------------------------------------


def _exp(text: str, kind: ExpectationKind = ExpectationKind.EXPECTED_OUTPUT) -> Expectation:
    return Expectation(expectation_id="exp001", kind=kind, text=text, source_quote="q")


def test_evidence_literal_hit() -> None:
    evidence = match_expectation_evidence(
        [_exp("EXPECTED TOKEN 9")],
        [
            {
                "normalized_command": "pytest x",
                "output": "saw EXPECTED TOKEN 9 in run",
                "generation": 2,
            }
        ],
    )
    assert len(evidence) == 1
    assert evidence[0].expectation_id == "exp001"
    assert evidence[0].generation == 2


def test_evidence_literal_miss() -> None:
    evidence = match_expectation_evidence(
        [_exp("EXPECTED TOKEN 9")],
        [{"normalized_command": "pytest x", "output": "nothing relevant here", "generation": 2}],
    )
    assert evidence == []


def test_multiline_literal_hit() -> None:
    evidence = match_expectation_evidence(
        [_exp("line one\nline two")],
        [
            {
                "normalized_command": "c",
                "output": "prefix line one\nline two suffix",
                "generation": 1,
            }
        ],
    )
    assert len(evidence) == 1


def test_no_match_across_run_boundary() -> None:
    # A literal split across two separate runs must not match — outputs are never
    # concatenated. This guards against a truncation-boundary false positive.
    evidence = match_expectation_evidence(
        [_exp("line one\nline two")],
        [
            {"normalized_command": "a", "output": "line one", "generation": 1},
            {"normalized_command": "b", "output": "line two", "generation": 1},
        ],
    )
    assert evidence == []


def test_short_literal_precision_floor() -> None:
    # A 1-2 char literal is below the precision floor and never yields evidence.
    evidence = match_expectation_evidence(
        [_exp("{}")],
        [{"normalized_command": "c", "output": "the dict is {} today", "generation": 1}],
    )
    assert evidence == []


def test_evidence_is_deterministic_first_run_wins() -> None:
    runs = [
        {"normalized_command": "first", "output": "MATCH_TOKEN here", "generation": 1},
        {"normalized_command": "second", "output": "MATCH_TOKEN again", "generation": 2},
    ]
    evidence = match_expectation_evidence([_exp("MATCH_TOKEN")], runs)
    assert len(evidence) == 1
    assert evidence[0].normalized_command == "first"


# ---------------------------------------------------------------------------
# Test 3a: assessment (pure)
# ---------------------------------------------------------------------------


def test_assess_confirmed_by_evidence() -> None:
    exp = _exp("TOKEN_X")
    evidence = match_expectation_evidence(
        [exp], [{"normalized_command": "c", "output": "TOKEN_X", "generation": 1}]
    )
    result = assess_expectations(expectations=[exp], evidence=evidence)
    assert result.confirmed == ("exp001",)
    assert result.unaddressed == ()


def test_assess_confirmed_by_locus_edit() -> None:
    exp = _exp("src/foo.py", ExpectationKind.NAMED_LOCUS)
    result = assess_expectations(expectations=[exp], evidence=[], edited_loci={"src/foo.py"})
    assert result.confirmed == ("exp001",)


def test_assess_unaddressed_when_locus_not_edited() -> None:
    exp = _exp("src/foo.py", ExpectationKind.NAMED_LOCUS)
    result = assess_expectations(expectations=[exp], evidence=[], edited_loci={"src/other.py"})
    assert result.unaddressed == ("exp001",)
    assert result.confirmed == ()


def test_assess_superseded_does_not_block_but_counts() -> None:
    exp = _exp("src/foo.py", ExpectationKind.NAMED_LOCUS)
    dispositions = {
        "exp001": DispositionRecord("exp001", ExpectationDisposition.SUPERSEDED, "spec was wrong")
    }
    result = assess_expectations(
        expectations=[exp], evidence=[], edited_loci=set(), dispositions=dispositions
    )
    assert result.superseded == ("exp001",)
    assert result.unaddressed == ()
    assert result.as_payload()["superseded_count"] == 1


def test_assess_not_applicable() -> None:
    exp = _exp("src/foo.py", ExpectationKind.NAMED_LOCUS)
    dispositions = {
        "exp001": DispositionRecord("exp001", ExpectationDisposition.NOT_APPLICABLE, "n/a")
    }
    result = assess_expectations(expectations=[exp], evidence=[], dispositions=dispositions)
    assert result.not_applicable == ("exp001",)
    assert result.unaddressed == ()


# ---------------------------------------------------------------------------
# Test 3b: gate integration via _completion_gate_problems
# ---------------------------------------------------------------------------


def _state_with_locus(text: str = "src/foo.py") -> TurnExecutionState:
    contract = AcceptanceContract(
        expectations=[Expectation("exp001", ExpectationKind.NAMED_LOCUS, text, "q")]
    )
    return TurnExecutionState(execution_requested=True, acceptance_contract=contract)


def _gate(state: TurnExecutionState, *, enabled: bool = True) -> list[str]:
    return _completion_gate_problems(
        state=state,
        final_text="Done.",
        blocked=False,
        verification_expected=False,
        evidence_v2=True,
        turn_intent="execute",
        regression_baseline_enabled=True,
        turn_contract_v2_enabled=enabled,
    )


def test_gate_unaddressed_expectation_blocks_then_clears_after_editing_locus() -> None:
    state = _state_with_locus("src/foo.py")
    state.note_material_edit()
    state.touched_repo_paths.add("src/other.py")  # edited the wrong file
    problems = _gate(state)
    assert "expectations_unaddressed" in problems
    assert _completion_gate_repair_stage(problems) == "expectations_unaddressed"

    state.touched_repo_paths.add("src/foo.py")  # now edit the named locus
    cleared = _gate(state)
    assert "expectations_unaddressed" not in cleared


@pytest.mark.parametrize("failure", [False, True])
def test_scoped_external_edit_confirms_only_its_exact_locus(tmp_path, failure):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "shared"
    external.mkdir()
    target = external / "report.txt"
    state = _state_with_locus(str(target))
    _record_tool_effect(
        root=project,
        state=state,
        tool_name="fs_write",
        arguments={"path": str(target)},
        status="failed" if failure else "done",
        result={"path": str(target), "scoped_workspace": str(external), "created": True},
        known_verification_commands=[],
    )
    assert not state.touched_repo_paths
    assert state.touched_external_paths == (set() if failure else {str(target)})
    assert ("expectations_unaddressed" in _gate(state)) is failure
    state.acceptance_contract.expectations = [
        Expectation("exp002", ExpectationKind.NAMED_LOCUS, str(external / "other.txt"), "q")
    ]
    assert "expectations_unaddressed" in _gate(state)


@pytest.mark.parametrize("canonical_argument", [False, True])
def test_external_output_uses_exact_host_observation_and_rechecks_alias(
    tmp_path, canonical_argument
):
    from alysis_code.agent.acceptance_contract import finalize_acceptance_contract

    root, external, other = (tmp_path / name for name in ("main", "outside", "other"))
    for path in (root, external, other):
        path.mkdir()
        (path / "output.custom").write_text("synthetic")
    alias = tmp_path / "外部 alias"
    try:
        alias.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink creation privilege")
        raise
    requested, target = alias / "output.custom", external / "output.custom"
    contract = build_acceptance_contract(
        root=root, instruction=f'Write the result to "{requested}".'
    )
    state = TurnExecutionState(execution_requested=True, acceptance_contract=contract)
    finalize_acceptance_contract(contract=contract, root=root, touched_paths=set())
    assert "acceptance_criteria_unverified" in contract.problem_names()
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="fs_write",
        arguments={"path": str(target if canonical_argument else requested)},
        status="done",
        result={"path": str(target), "scoped_workspace": str(external), "created": True},
        known_verification_commands=[],
    )
    finalize_acceptance_contract(contract=contract, root=root, touched_paths=set())
    assert "acceptance_criteria_unverified" not in contract.problem_names()
    assert not state.compute_expectation_assessment(enabled=True, turn_intent="execute").unaddressed
    assert "external_path_observations" not in contract.as_payload()
    target.unlink()
    finalize_acceptance_contract(contract=contract, root=root, touched_paths=set())
    assert "acceptance_criteria_unverified" in contract.problem_names()
    target.write_text("restored")
    finalize_acceptance_contract(contract=contract, root=root, touched_paths=set())
    assert "acceptance_criteria_unverified" not in contract.problem_names()
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    finalize_acceptance_contract(contract=contract, root=root, touched_paths=set())
    assert "acceptance_criteria_unverified" in contract.problem_names()


@pytest.mark.parametrize("failure,private", [(True, False), (False, True)])
def test_failed_or_private_external_output_cannot_satisfy_required_artifact(
    tmp_path, monkeypatch, failure, private
):
    from alysis_code.agent.acceptance_contract import finalize_acceptance_contract

    root, external = tmp_path / "main", tmp_path / "outside"
    root.mkdir()
    external.mkdir()
    target = external / "output.custom"
    target.write_text("synthetic")
    if private:
        monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(external))
    contract = build_acceptance_contract(root=root, instruction=f'Write the result to "{target}".')
    state = TurnExecutionState(execution_requested=True, acceptance_contract=contract)
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="fs_write",
        arguments={"path": str(target)},
        status="failed" if failure else "done",
        result={"path": str(target), "scoped_workspace": str(external), "created": True},
        known_verification_commands=[],
    )
    finalize_acceptance_contract(contract=contract, root=root, touched_paths=set())
    assert "acceptance_criteria_unverified" in contract.problem_names()


@pytest.mark.parametrize("attached", [False, True])
def test_attached_folder_edit_records_only_the_authorized_exact_path(tmp_path, attached):
    from alysis_code.project_folders import folder_scope

    project, extra = tmp_path / "project", tmp_path / "共有"
    project.mkdir()
    extra.mkdir()
    target = extra / "report.txt"
    state = _state_with_locus(str(target))
    with folder_scope(project, [str(extra)] if attached else []):
        _record_tool_effect(
            root=project,
            state=state,
            tool_name="fs_write",
            arguments={"path": str(target)},
            status="done",
            result={"path": str(target), "created": True},
            known_verification_commands=[],
        )
    assert state.touched_external_paths == ({str(target)} if attached else set())
    assert ("expectations_unaddressed" in _gate(state)) is not attached
    # Same filename in another folder must not be credited as the edited target.
    state.acceptance_contract.expectations = [
        Expectation("exp002", ExpectationKind.NAMED_LOCUS, str(project / target.name), "q")
    ]
    assert "expectations_unaddressed" in _gate(state)


@pytest.mark.parametrize("tool", ["fs_read", "shell_run", "fs_write"])
def test_unrelated_or_read_only_scoped_results_cannot_confirm_an_edit(tmp_path, tool):
    project = tmp_path / "project"
    external = tmp_path / "shared"
    target = external / "report.txt"
    state = _state_with_locus(str(target))
    _record_tool_effect(
        root=project,
        state=state,
        tool_name=tool,
        arguments={"path": str(external / "different.txt") if tool == "fs_write" else str(target)},
        status="done",
        result={"path": str(target), "scoped_workspace": str(external)},
        known_verification_commands=[],
    )
    assert not state.touched_external_paths
    assert "expectations_unaddressed" in _gate(state)


def test_gate_expectation_confirmed_by_run_output() -> None:
    contract = AcceptanceContract(
        expectations=[Expectation("exp001", ExpectationKind.EXPECTED_OUTPUT, "TARGET_STRING", "q")]
    )
    state = TurnExecutionState(execution_requested=True, acceptance_contract=contract)
    state.note_material_edit()
    state.note_verification_relevant_edit()
    state.touched_repo_paths.add("src/app.py")
    state.note_post_edit_run_output(
        command="pytest",
        output="run produced TARGET_STRING once",
        generation=state.verification_relevant_edit_generation,
    )
    problems = _gate(state)
    assert "expectations_unaddressed" not in problems


def test_gate_zero_expectations_is_unaffected() -> None:
    state = TurnExecutionState(execution_requested=True, acceptance_contract=AcceptanceContract())
    state.note_material_edit()
    state.touched_repo_paths.add("src/app.py")
    problems = _gate(state)
    assert "expectations_unaddressed" not in problems


def test_gate_non_execute_turn_ignores_expectations() -> None:
    state = _state_with_locus("src/foo.py")
    state.note_material_edit()
    state.touched_repo_paths.add("src/other.py")
    problems = _completion_gate_problems(
        state=state,
        final_text="Here is my analysis.",
        blocked=False,
        verification_expected=False,
        evidence_v2=True,
        turn_intent="advisory_non_execution",
        turn_contract_v2_enabled=True,
    )
    assert "expectations_unaddressed" not in problems


def test_certificate_expectations_unaddressed_is_insufficient_not_contradicted() -> None:
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=None,
            final_text="done",
            blocked=False,
            blocker_valid=False,
            material_edit_count=1,
            require_material_result=True,
            verification_expected=False,
            verification_attempt_count=0,
            last_verification_passed=None,
            turn_contract_v2_enabled=True,
            expectations_unaddressed=("exp001",),
        )
    )
    assert "expectations_unaddressed" in certificate.problems
    assert certificate.status == CompletionCertificateStatus.INSUFFICIENT
    assert certificate.expectations_unaddressed == ("exp001",)


def test_certificate_kill_switch_off_drops_expectations_problem() -> None:
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=None,
            final_text="done",
            blocked=False,
            blocker_valid=False,
            material_edit_count=1,
            require_material_result=True,
            verification_expected=False,
            verification_attempt_count=0,
            last_verification_passed=None,
            turn_contract_v2_enabled=False,
            expectations_unaddressed=("exp001",),
        )
    )
    assert "expectations_unaddressed" not in certificate.problems
    assert certificate.expectations_unaddressed == ()


def test_certificate_blocked_turn_exempt_from_expectations() -> None:
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=None,
            final_text="blocked by missing creds",
            blocked=True,
            blocker_valid=True,
            material_edit_count=0,
            require_material_result=True,
            verification_expected=False,
            verification_attempt_count=0,
            last_verification_passed=None,
            turn_contract_v2_enabled=True,
            expectations_unaddressed=("exp001",),
        )
    )
    assert "expectations_unaddressed" not in certificate.problems


# ---------------------------------------------------------------------------
# Test 4: apply-don't-advise (advisory completion) — pure resolution + enums
# ---------------------------------------------------------------------------


def test_resolve_advisory_completion_synthesizes_other_when_absent() -> None:
    advisory = resolve_advisory_completion(None)
    assert advisory.reason == AdvisoryCompletionReason.OTHER
    assert advisory.explanation


def test_resolve_advisory_completion_uses_recorded() -> None:
    recorded = AdvisoryCompletion(AdvisoryCompletionReason.NO_CHANGE_NEEDED, "already correct")
    advisory = resolve_advisory_completion(recorded)
    assert advisory.reason == AdvisoryCompletionReason.NO_CHANGE_NEEDED
    assert advisory.explanation == "already correct"


def test_advisory_summary_surfaces_reason_and_explanation() -> None:
    summary = build_advisory_completion_summary(
        AdvisoryCompletionReason.CANNOT_REPRODUCE, "the bug does not reproduce locally"
    )
    # The agent's own explanation, in plain words; the machine reason label is not shown.
    assert summary == "\n\n---\nNo files were changed: the bug does not reproduce locally."
    assert "cannot_reproduce" not in summary


def test_coerce_advisory_reason_valid_and_invalid() -> None:
    assert (
        coerce_advisory_reason("out_of_scope_request")
        == AdvisoryCompletionReason.OUT_OF_SCOPE_REQUEST
    )
    assert coerce_advisory_reason("not_a_reason") is None
    assert coerce_advisory_reason(None) is None


def test_coerce_expectation_disposition_valid_and_invalid() -> None:
    assert coerce_expectation_disposition("superseded") == ExpectationDisposition.SUPERSEDED
    assert coerce_expectation_disposition("bogus") is None


# ---------------------------------------------------------------------------
# Test 5a: kill-switch (env + config)
# ---------------------------------------------------------------------------


def test_kill_switch_env_and_config(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_TURN_CONTRACT_V2", raising=False)
    assert _turn_contract_v2_enabled(AppConfig(model="x", turn_contract_v2_enabled=True)) is True
    assert _turn_contract_v2_enabled(AppConfig(model="x", turn_contract_v2_enabled=False)) is False
    monkeypatch.setenv("ALYSIS_TURN_CONTRACT_V2", "off")
    assert _turn_contract_v2_enabled(AppConfig(model="x", turn_contract_v2_enabled=True)) is False
    monkeypatch.setenv("ALYSIS_TURN_CONTRACT_V2", "on")
    assert _turn_contract_v2_enabled(AppConfig(model="x", turn_contract_v2_enabled=False)) is True


def test_kill_switch_config_key_roundtrip() -> None:
    cfg = AppConfig(model="x")
    assert cfg.turn_contract_v2_enabled is True
    set_config_value(cfg, "turn_contract_v2_enabled", "off")
    assert cfg.turn_contract_v2_enabled is False
    set_config_value(cfg, "turn_contract_v2_enabled", "true")
    assert cfg.turn_contract_v2_enabled is True
    with pytest.raises(ConfigError):
        set_config_value(cfg, "turn_contract_v2_enabled", "maybe")


# ---------------------------------------------------------------------------
# Test 5b: system-prompt invariance (runtime kind x kill-switch -> identical bytes)
# ---------------------------------------------------------------------------

_PROMPT_ANCHORS = (
    "Apply, do not just describe",
    "untrusted hypothesis",
    "the faulty file, function, commit, or PR as the fix site",
    "differential evidence",
)


def _compose(*, one_shot: bool) -> str:
    from alysis_code.agent.prompt_context import _compose_session_system_prompt
    from alysis_code.agent_loop import SYSTEM_PROMPT

    return _compose_session_system_prompt(
        base_prompt=SYSTEM_PROMPT,
        trusted_prompt_append="",
        include_write_guidance=True,
        include_skill_discovery_guidance=True,
        include_skill_lifecycle_guidance=True,
        include_subagent_guidance=True,
        include_one_shot_guidance=one_shot,
    )


def test_turn_contract_prompt_norms_present_in_base_prompt() -> None:
    from alysis_code.agent_loop import SYSTEM_PROMPT

    for anchor in _PROMPT_ANCHORS:
        assert anchor in SYSTEM_PROMPT


def test_prompt_bytes_identical_regardless_of_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_TURN_CONTRACT_V2", "on")
    prompt_on = _compose(one_shot=True)
    monkeypatch.setenv("ALYSIS_TURN_CONTRACT_V2", "off")
    prompt_off = _compose(one_shot=True)

    assert prompt_on.encode("utf-8") == prompt_off.encode("utf-8")
    assert "TURN_CONTRACT_V2" not in prompt_on
    assert "turn_contract_v2" not in prompt_on
    assert "ALYSIS_TURN_CONTRACT_V2" not in prompt_on


def test_prompt_norms_identical_across_runtime_kinds(monkeypatch) -> None:
    # The base prompt is one constant used by every runtime kind; only the one-shot
    # section differs (and it strips the clarification rule, which is nowhere near
    # the turn-contract norms). Each norm bullet must appear byte-for-byte in the
    # composed prompt whether or not the one-shot section is included.
    monkeypatch.delenv("ALYSIS_TURN_CONTRACT_V2", raising=False)
    interactive = _compose(one_shot=False)
    one_shot = _compose(one_shot=True)
    for anchor in _PROMPT_ANCHORS:
        assert interactive.count(anchor) == 1
        assert one_shot.count(anchor) == 1


# ---------------------------------------------------------------------------
# Test 6: markers are visible and distinct
# ---------------------------------------------------------------------------


def test_unconfirmed_expectations_marker_visible_and_distinct() -> None:
    marker = build_unconfirmed_expectations_marker(
        ["add AMOUNT CATEGORY [--note TEXT]", "summary --month YYYY-MM"]
    )
    # The request's own words, never the internal expectation ids.
    assert marker == (
        "\n\n---\n⚠️ Not confirmed: “add AMOUNT CATEGORY [--note TEXT]”; "
        "“summary --month YYYY-MM”. The request asked for these, and this turn did not show "
        "them working."
    )
    assert "exp0" not in marker
    assert marker != HONEST_UNVERIFIED_FINALIZATION_MARKER
    assert "REGRESSIONS" not in marker


def test_unconfirmed_expectations_marker_singular_long_and_empty() -> None:
    single = build_unconfirmed_expectations_marker(["x" * 100])
    assert "this turn did not show it working" in single
    assert "“" + "x" * 79 + "…”" in single
    assert build_unconfirmed_expectations_marker([]) == (
        "\n\n---\n⚠️ Not confirmed: some things the request named were not shown "
        "working in this turn."
    )


def test_advisory_completion_summary_format() -> None:
    summary = build_advisory_completion_summary(
        AdvisoryCompletionReason.NO_CHANGE_NEEDED, "already right"
    )
    assert summary == "\n\n---\nNo files were changed: already right."


def test_advisory_summary_without_a_recorded_reason_says_only_what_happened() -> None:
    summary = build_advisory_completion_summary(
        AdvisoryCompletionReason.OTHER, DEFAULT_ADVISORY_COMPLETION_EXPLANATION
    )
    assert summary == "\n\n---\nNo files were changed in this turn."
    assert build_advisory_completion_summary(AdvisoryCompletionReason.NO_CHANGE_NEEDED, "") == (
        "\n\n---\nNo files were changed: no change was needed."
    )
