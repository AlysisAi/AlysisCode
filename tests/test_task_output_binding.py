import pytest

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionKind,
    AcceptanceCriterionStatus,
    build_acceptance_contract,
    finalize_acceptance_contract,
)
from alysis_code.agent.artifact_output_observation import ArtifactOutputObserver
from alysis_code.agent.task_artifact_binding import (
    bind_path_interpretation,
    binding_parameters,
    declared_changed_output_paths,
    host_output_directory,
    validated_output_directory,
)
from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect


def interpret(root, names, entries, output="delivered/current-task"):
    instruction = "Create " + " and ".join(names) + "."
    baseline = build_acceptance_contract(root=root, instruction=instruction)
    refs = bind_path_interpretation(
        root=root,
        instruction=instruction,
        prior_refs=baseline.path_refs,
        arguments={
            "paths": [
                {
                    "directory": "",
                    "path_base": "active_workdir",
                    "name": name,
                    "role": "required_output",
                    "evidence_quote": instruction,
                    **extra,
                }
                for name, extra in zip(names, entries, strict=True)
            ]
        },
        output_directory=output,
    )
    return build_acceptance_contract(
        root=root,
        instruction=instruction,
        workspace_snapshot=baseline.snapshot,
        interpreted_path_refs=refs,
    )


@pytest.mark.parametrize("name", ["meeting.md", "τιμές.csv", "O’Brien.txt"])
def test_legacy_quote_typography_cannot_change_original_file_identity(tmp_path, name):
    instruction = f"Create {name} in the task’s output folder."
    quote = instruction.replace("’", "'")
    refs = bind_path_interpretation(
        root=tmp_path,
        instruction=instruction,
        prior_refs=[],
        output_directory="delivered/current-task",
        arguments={
            "paths": [
                {
                    "directory": "",
                    "path_base": "active_workdir",
                    "name": name,
                    "role": "required_output",
                    "location": "task_output",
                    "evidence_quote": quote,
                }
            ]
        },
    )
    assert len(refs) == 1
    assert refs[0].workspace_relative_path == f"delivered/current-task/{name}"
    assert refs[0].clause == instruction


@pytest.mark.parametrize(
    "instruction,name,quote",
    [
        (
            "Create O’Brien.txt in the task’s folder.",
            "O'Brien.txt",
            "Create O'Brien.txt in the task's folder.",
        ),
        (
            "Create Γράμμα.txt in the task’s folder.",
            "Γραμμα.txt",
            "Create Γραμμα.txt in the task's folder.",
        ),
        (
            "Create Notes.txt in the task’s folder.",
            "notes.txt",
            "Create notes.txt in the task's folder.",
        ),
    ],
)
def test_legacy_quote_cannot_rewrite_paths_accents_or_case(tmp_path, instruction, name, quote):
    with pytest.raises(ValueError, match="verbatim current user text"):
        bind_path_interpretation(
            root=tmp_path,
            instruction=instruction,
            prior_refs=[],
            output_directory="delivered/current-task",
            arguments={
                "paths": [
                    {
                        "directory": "",
                        "path_base": "active_workdir",
                        "name": name,
                        "role": "required_output",
                        "location": "task_output",
                        "evidence_quote": quote,
                    }
                ]
            },
        )


def test_legacy_quote_cannot_add_requirements_or_replace_original_context(tmp_path):
    instruction = "Create report.txt as valid JSON."
    refs = bind_path_interpretation(
        root=tmp_path,
        instruction=instruction,
        prior_refs=[],
        output_directory="delivered/current-task",
        arguments={
            "paths": [
                {
                    "name": "report.txt",
                    "directory": "",
                    "role": "required_output",
                    "location": "task_output",
                    "evidence_quote": "Create report.txt as CSV.",
                }
            ]
        },
    )
    assert refs[0].clause == instruction
    assert refs[0].workspace_relative_path == "delivered/current-task/report.txt"


@pytest.mark.parametrize("name", ["agenda.md", "measurements.csv", "Σύνοψη.txt", "drawing.custom"])
def test_host_default_is_exact_output_identity_for_all_acceptance_checks(tmp_path, name):
    contract = interpret(tmp_path, [name], [{"location": "task_output"}])
    expected = f"delivered/current-task/{name}"
    assert [ref.workspace_relative_path for ref in contract.path_refs] == [expected]
    assert contract.path_refs[0].raw_text == name  # User provenance remains intact.
    target = tmp_path / expected
    target.parent.mkdir(parents=True)
    target.write_text("fresh content", encoding="utf-8")
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths={expected})
    output = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    assert output.status == AcceptanceCriterionStatus.PASSED
    assert not (tmp_path / name).exists()  # No duplicate is necessary.


def test_same_basename_at_root_or_another_task_cannot_satisfy_default_destination(tmp_path):
    for folder in [tmp_path, tmp_path / "delivered/older-task"]:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "notes.txt").write_text("stale")
    contract = interpret(tmp_path, ["notes.txt"], [{"location": "task_output"}])
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())
    assert contract.problem_names()
    assert any("delivered/current-task/notes.txt" in c.failure_summary for c in contract.criteria)


def test_explicit_workspace_root_and_subdirectories_keep_their_locations(tmp_path):
    for name in ["root-file.txt", "reports/table.csv", "./explicit-root.txt"]:
        contract = interpret(tmp_path, [name], [{"location": "workspace"}])
        assert contract.path_refs[0].absolute_path == (tmp_path / name).as_posix()


def test_multiple_outputs_keep_separate_identities_and_missing_one_still_fails(tmp_path):
    contract = interpret(tmp_path, ["first.md", "second.txt"], [{"location": "task_output"}] * 2)
    target = tmp_path / "delivered/current-task/first.md"
    target.parent.mkdir(parents=True)
    target.write_text("first")
    finalize_acceptance_contract(
        contract=contract, root=tmp_path, touched_paths={"delivered/current-task/first.md"}
    )
    assert contract.problem_names()
    assert any("second.txt" in c.failure_summary for c in contract.criteria)


@pytest.mark.parametrize(
    "name,extra",
    [
        ("reports/output.txt", {}),
        ("./output.txt", {}),
        ("/outside/output.txt", {}),
        (r"C:\outside\output.txt", {}),
        ("output.txt", {"directory": "reports"}),
    ],
)
def test_default_cannot_replace_an_explicit_path_or_directory(tmp_path, name, extra):
    with pytest.raises(ValueError):
        interpret(tmp_path, [name], [{"location": "task_output", **extra}])


@pytest.mark.parametrize(
    "output", [None, "../outside", "/outside", r"C:\outside", "a/../../outside", "a\x00b", "."]
)
def test_absent_or_invalid_host_defaults_do_not_enroll_paths(tmp_path, output):
    with pytest.raises(ValueError):
        interpret(tmp_path, ["output.txt"], [{"location": "task_output"}], output)


def test_output_default_is_not_an_external_path_permission(tmp_path):
    contract = interpret(tmp_path, ["/unapproved/output.txt"], [{"location": "workspace"}])
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())
    assert all(c.status != AcceptanceCriterionStatus.PASSED for c in contract.criteria)
    assert contract.path_refs[0].absolute_path == "/unapproved/output.txt"


def test_linked_default_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    with pytest.raises(ValueError, match="links"):
        validated_output_directory(tmp_path, "linked/current")


def test_explicit_containing_folder_is_preserved_even_when_the_host_has_a_default(tmp_path):
    instruction = 'Create "table.csv" in "reports".'
    baseline = build_acceptance_contract(root=tmp_path, instruction=instruction)
    refs = bind_path_interpretation(
        root=tmp_path,
        instruction=instruction,
        prior_refs=baseline.path_refs,
        arguments={
            "paths": [
                {
                    "path_base": "active_workdir",
                    "name": "table.csv",
                    "directory": "reports",
                    "role": "required_output",
                    "evidence_quote": instruction,
                    "location": "workspace",
                }
            ]
        },
        output_directory="delivered/current-task",
    )
    assert [ref.workspace_relative_path for ref in refs] == ["reports/table.csv"]


def test_stale_same_path_cannot_confirm_a_new_operation_without_a_current_change(tmp_path):
    target = tmp_path / "delivered/current-task/notes.txt"
    target.parent.mkdir(parents=True)
    target.write_text("older operation")
    contract = interpret(tmp_path, ["notes.txt"], [{"location": "task_output"}])
    contract.task_id = "new-operation"
    contract.artifact_output_observer = ArtifactOutputObserver(contract, lambda path, initial: True)
    state = TurnExecutionState(execution_requested=True, acceptance_contract=contract)
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())
    assert state.compute_expectation_assessment(enabled=True, turn_intent="execute").unaddressed
    target.write_text("new operation with independently observed changed content")
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())
    assert state.compute_expectation_assessment(enabled=True, turn_intent="execute").confirmed


def test_output_declaration_requires_current_tool_effect_and_exact_required_identity(tmp_path):
    contract = interpret(
        tmp_path, ["one.csv", "nested/one.csv", ".private.txt"], [{"location": "workspace"}] * 3
    )
    state = TurnExecutionState(execution_requested=True, acceptance_contract=contract)
    state.touched_repo_paths.update(
        {"one.csv", "nested/one.csv"}
    )  # Prior-task evidence cannot enroll a current card.
    assert declared_changed_output_paths(state) == []
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="fs_write",
        arguments={"path": "one.csv", "content": "new"},
        status="ok",
        result={"path": "one.csv"},
        known_verification_commands=[],
    )
    assert declared_changed_output_paths(state) == ["one.csv"]
    state.turn_touched_repo_paths.update({"unrequested.txt", ".private.txt", "elsewhere/one.csv"})
    assert declared_changed_output_paths(state) == ["one.csv"]
    state.turn_touched_repo_paths.add("nested/one.csv")
    assert declared_changed_output_paths(state) == ["nested/one.csv", "one.csv"]


def test_current_effect_set_is_not_restored_from_cumulative_task_evidence(tmp_path):
    from alysis_code.agent.task_evidence import TaskEvidenceState, seed_execution_state

    state = TurnExecutionState(execution_requested=True)
    seed_execution_state(
        state,
        TaskEvidenceState(
            task_id="current-task",
            workspace_root=str(tmp_path),
            snapshot=build_acceptance_contract(root=tmp_path, instruction="Review.").snapshot,
            touched_repo_paths={"older.csv"},
        ),
    )
    assert state.touched_repo_paths == {"older.csv"}
    assert state.turn_touched_repo_paths == set()


@pytest.mark.parametrize("folder", ["package", "packages/Έργο", "nested/nested"])
def test_relative_request_paths_match_file_tool_bases_without_rewriting_literal_proof(
    tmp_path, folder
):
    instruction = "Change src/value.mjs and preserve package.json unchanged."
    arguments = {
        "paths": [
            {
                "directory": "",
                "path_base": "active_workdir",
                "name": "src/value.mjs",
                "role": "required_output",
                "evidence_quote": instruction,
            },
            {
                "directory": "",
                "path_base": "active_workdir",
                "name": "package.json",
                "role": "preservation_target",
                "evidence_quote": instruction,
            },
        ]
    }
    refs = bind_path_interpretation(
        root=tmp_path,
        instruction=instruction,
        arguments=arguments,
        prior_refs=[],
        working_directory=folder,
    )
    assert [ref.workspace_relative_path for ref in refs] == [
        f"{folder}/src/value.mjs",
        f"{folder}/package.json",
    ]
    assert [ref.raw_text for ref in refs] == ["src/value.mjs", "package.json"]
    arguments["paths"][0]["path_base"] = "workspace_root"
    refs = bind_path_interpretation(
        root=tmp_path,
        instruction=instruction,
        arguments=arguments,
        prior_refs=[],
        working_directory=folder,
    )
    assert refs[0].workspace_relative_path == "src/value.mjs"
    assert refs[1].workspace_relative_path == f"{folder}/package.json"
    assert (
        "path_base" in binding_parameters(None, folder)["properties"]["paths"]["items"]["required"]
    )


def test_host_prefix_is_not_deduplicated_and_output_default_stays_with_selected_project(
    tmp_path, monkeypatch
):
    instruction = "Create package/note.txt."
    refs = bind_path_interpretation(
        root=tmp_path,
        instruction=instruction,
        prior_refs=[],
        working_directory="package",
        arguments={
            "paths": [
                {
                    "directory": "",
                    "path_base": "active_workdir",
                    "name": "package/note.txt",
                    "role": "required_output",
                    "evidence_quote": instruction,
                }
            ]
        },
    )
    assert refs[0].workspace_relative_path == "package/package/note.txt"
    monkeypatch.setenv("ALYSIS_DESKTOP_TASK_OUTPUT_DIRECTORY", "outputs/current")
    assert host_output_directory(tmp_path, relative_to="package") == "package/outputs/current"
    contract = interpret(
        tmp_path, ["note.txt"], [{"location": "task_output"}], "package/outputs/current"
    )
    state = TurnExecutionState(execution_requested=True, acceptance_contract=contract)
    state.turn_touched_repo_paths.update({"package/outputs/current/note.txt", "other/note.txt"})
    assert declared_changed_output_paths(state, relative_to="package") == [
        "outputs/current/note.txt"
    ]
    assert declared_changed_output_paths(state, relative_to="other") == []


@pytest.mark.parametrize("base", ["../outside", "/outside", "C:/outside", "", None])
def test_untrusted_working_directory_cannot_rebase_an_interpretation_outside_scope(tmp_path, base):
    with pytest.raises(ValueError):
        bind_path_interpretation(
            root=tmp_path,
            instruction="Create note.txt.",
            prior_refs=[],
            working_directory=base,
            arguments={
                "paths": [
                    {
                        "directory": "",
                        "path_base": "active_workdir",
                        "name": "note.txt",
                        "role": "required_output",
                        "evidence_quote": "Create note.txt.",
                    }
                ]
            },
        )
