from pathlib import Path

import pytest

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionKind,
    AcceptanceCriterionStatus,
    AcceptancePathRole,
    build_acceptance_contract,
    finalize_acceptance_contract,
)
from alysis_code.agent.task_artifact_binding import (
    bind_path_interpretation,
    path_binding_identity,
    retained_user_instructions,
)


def binding(name, quote, role="required_output", directory=""):
    return {"name": name, "directory": directory, "role": role, "evidence_quote": quote}


@pytest.mark.parametrize(
    "remote",
    [
        "https://example.test/team/project.git",
        "ssh://git@example.test/team/project.git",
        "git@example.test:team/project.git",
        "custom+transport://example.test/資料/εργασία.json",
    ],
)
def test_remote_resource_is_not_a_local_artifact(tmp_path, remote):
    instruction = f"Clone {remote} and write result.json as valid JSON."
    baseline = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert [ref.raw_text for ref in baseline.path_refs] == ["result.json"]
    with pytest.raises(ValueError, match="Remote resource"):
        bind_path_interpretation(
            root=tmp_path,
            instruction=instruction,
            arguments={"paths": [binding(remote, instruction, role="existing_input")]},
            prior_refs=baseline.path_refs,
        )


def interpret(root, instruction, entries):
    baseline = build_acceptance_contract(root=root, instruction=instruction)
    refs = bind_path_interpretation(
        root=root,
        instruction=instruction,
        arguments={"paths": entries},
        prior_refs=baseline.path_refs,
    )
    return build_acceptance_contract(
        root=root,
        instruction=instruction,
        workspace_snapshot=baseline.snapshot,
        interpreted_path_refs=refs,
    )


@pytest.mark.parametrize("name", ["reports/input.txt", "資料/δεδομένα.txt", r"reports\input.txt"])
@pytest.mark.parametrize("ending", [".", "!", ")"])
def test_sentence_punctuation_does_not_make_an_exact_binding_look_omitted(tmp_path, name, ending):
    instruction = f"Inspect {name}{ending}"
    contract = interpret(tmp_path, instruction, [binding(name, instruction, role="existing_input")])
    assert len(contract.path_refs) == 1
    assert contract.path_refs[0].display_path == name.replace("\\", "/")


@pytest.mark.parametrize(
    "quote", [None, "result.json", "Create result.json", "Create result.json as plain text."]
)
def test_short_binding_quote_cannot_weaken_explicit_format(tmp_path, quote):
    instruction = "Create result.json as valid JSON."
    entry = binding("result.json", quote)
    if quote is None:
        del entry["evidence_quote"]
    contract = interpret(tmp_path, instruction, [entry])
    (tmp_path / "result.json").write_text("invalid JSON")
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())
    criterion = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA
    )
    assert criterion.required_for_finalization
    assert criterion.status == AcceptanceCriterionStatus.FAILED
    assert contract.problem_names()


@pytest.mark.parametrize("name", ["result; final.custom", "result and save.custom"])
def test_host_binding_context_preserves_filename_punctuation(tmp_path, name):
    instruction = f'Create "{name}" in "reports" as valid JSON.'
    contract = interpret(tmp_path, instruction, [binding(name, instruction, directory="reports")])
    criterion = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA
    )
    assert criterion.paths == (f"reports/{name}",)
    assert criterion.required_for_finalization


@pytest.mark.parametrize(
    "instruction",
    [
        'Create "result.custom" in "json-output" as valid JSON; create "result.custom" in "csv-output" as valid CSV.',
        'In "json-output", create "result.custom" as valid JSON; in "csv-output", create "result.custom" as valid CSV.',
    ],
)
def test_binding_formats_stay_scoped_to_each_basename_and_directory(tmp_path, instruction):
    contract = interpret(
        tmp_path,
        instruction,
        [
            binding("result.custom", "result.custom", directory="json-output"),
            binding("result.custom", "result.custom", directory="csv-output"),
        ],
    )
    formats = [
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA
    ]
    assert len(formats) == 2
    assert all(c.required_for_finalization for c in formats)
    assert any(
        c.paths == ("json-output/result.custom",) and "json" in c.description.lower()
        for c in formats
    )
    assert any(
        c.paths == ("csv-output/result.custom",) and "csv" in c.description.lower() for c in formats
    )


@pytest.mark.parametrize(
    "output", ["Workshop bundle.zip", "Σύνοψη.custom", "Report without extension"]
)
def test_output_identity_sources_and_directory_are_bound_from_request(tmp_path, output):
    folder = str(tmp_path / "destination")
    instruction = (
        f'Create "{output}" inside "{folder}" containing Planning notes.txt and Supplies.csv '
        "from that folder. Preserve both originals."
    )
    contract = interpret(
        tmp_path,
        instruction,
        [
            binding(output, instruction, directory=folder),
            binding(folder, instruction, "location_context"),
            binding("Planning notes.txt", instruction, "preservation_target", folder),
            binding("Supplies.csv", instruction, "preservation_target", folder),
        ],
    )
    outputs = [ref for ref in contract.path_refs if ref.role == AcceptancePathRole.REQUIRED_OUTPUT]
    assert [ref.absolute_path for ref in outputs] == [
        (tmp_path / "destination" / output).as_posix()
    ]
    assert contract.allowed_output_paths == {f"destination/{output}"}
    assert [expectation.text for expectation in contract.expectations] == [f"destination/{output}"]
    assert not any(ref.raw_text == "notes.txt" for ref in contract.path_refs)
    assert {
        ref.workspace_relative_path
        for ref in contract.path_refs
        if ref.role == AcceptancePathRole.PRESERVATION_TARGET
    } == {"destination/Planning notes.txt", "destination/Supplies.csv"}
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())
    output_criterion = next(
        item
        for item in contract.criteria
        if item.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    assert output_criterion.status != AcceptanceCriterionStatus.PASSED


def test_same_basename_in_separate_directories_retains_distinct_roles(tmp_path):
    instruction = 'Read "data.json" in "source" and write "data.json" in "target".'
    contract = interpret(
        tmp_path,
        instruction,
        [
            binding("data.json", instruction, "existing_input", "source"),
            binding("data.json", instruction, directory="target"),
        ],
    )
    assert contract.allowed_output_paths == {"target/data.json"}
    assert {ref.absolute_path for ref in contract.path_refs} == {
        (tmp_path / "source/data.json").as_posix(),
        (tmp_path / "target/data.json").as_posix(),
    }


@pytest.mark.parametrize("folder,name", [("clone", "result.txt"), ("資料", "σύνοψη.custom")])
def test_one_output_mention_cannot_create_workspace_and_contained_obligations(
    tmp_path, folder, name
):
    instruction = f'Use folder "{folder}". Create "{name}" in that folder.'
    with pytest.raises(ValueError, match="same file mention"):
        interpret(
            tmp_path,
            instruction,
            [
                binding(name, instruction),
                binding(name, instruction, directory=folder),
            ],
        )
    contract = interpret(tmp_path, instruction, [binding(name, instruction, directory=folder)])
    assert contract.allowed_output_paths == {f"{folder}/{name}"}
    assert not (tmp_path / name).exists()


def test_distinct_explicit_containers_for_same_name_are_not_collapsed(tmp_path):
    instruction = 'Create report.txt in both "first" and "second".'
    contract = interpret(
        tmp_path,
        instruction,
        [
            binding("report.txt", instruction, directory="first"),
            binding("report.txt", instruction, directory="second"),
        ],
    )
    assert contract.allowed_output_paths == {"first/report.txt", "second/report.txt"}


@pytest.mark.parametrize(
    "entries",
    [
        [binding("stolen.txt", "Create result.txt.")],
        [binding("result.txt", "Create result.txt.", directory="C:/invented")],
        [binding("result.txt", "Create result.txt.", role="complete")],
        [],
    ],
)
def test_ungrounded_or_invalid_bindings_never_create_requirements(tmp_path, entries):
    with pytest.raises(ValueError):
        interpret(tmp_path, "Create result.txt.", entries)


def test_binding_cannot_omit_another_recognized_user_path(tmp_path):
    instruction = "Create first.txt and second.txt."
    with pytest.raises(ValueError, match="omitted"):
        interpret(tmp_path, instruction, [binding("first.txt", instruction)])


@pytest.mark.parametrize("setting", ["user.name", "παράμετρος.όνομα", "属性.名称"])
def test_explicit_non_path_interpretation_removes_false_positive_without_access(tmp_path, setting):
    instruction = f"Set {setting} to a value, then create result.txt."
    baseline = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert setting in {ref.raw_text for ref in baseline.path_refs}
    contract = interpret(
        tmp_path,
        instruction,
        [
            binding(setting, instruction, role="non_path"),
            binding("result.txt", instruction),
        ],
    )
    assert [ref.raw_text for ref in contract.path_refs] == ["result.txt"]
    assert contract.allowed_output_paths == {"result.txt"}
    assert not (tmp_path / "result.txt").exists()


def test_false_positive_classification_cannot_silently_omit_a_different_candidate(tmp_path):
    instruction = "Set user.name, then create result.txt."
    with pytest.raises(ValueError, match="omitted.*result.txt"):
        interpret(tmp_path, instruction, [binding("user.name", instruction, role="non_path")])
    with pytest.raises(ValueError, match="omitted.*result.txt"):
        interpret(tmp_path, instruction, [binding(instruction, instruction, role="non_path")])


def test_additional_verbatim_non_path_mentions_are_harmless(tmp_path):
    instruction = "Use branch review/topic and write result.txt."
    contract = interpret(
        tmp_path,
        instruction,
        [
            binding("review/topic", instruction, role="non_path"),
            binding("result.txt", instruction),
        ],
    )
    assert [ref.raw_text for ref in contract.path_refs] == ["result.txt"]
    with pytest.raises(ValueError, match="empty directory"):
        interpret(
            tmp_path,
            instruction,
            [
                binding("review/topic", instruction, role="non_path", directory="review/topic"),
            ],
        )


def test_non_path_only_request_has_no_artifact_obligation(tmp_path):
    instruction = "Set custom.property."
    contract = interpret(
        tmp_path,
        instruction,
        [
            binding("custom.property", instruction, role="non_path"),
        ],
    )
    assert contract.path_refs == []
    assert not contract.allowed_output_paths


@pytest.mark.parametrize("literal", [r"sample\n", r"資料\περιεχόμενο", "./relative/item"])
def test_non_path_literal_uses_same_identity_normalization_as_detector(tmp_path, literal):
    instruction = f'Write "{literal}" to result.txt.'
    contract = interpret(
        tmp_path,
        instruction,
        [
            binding(literal, literal, role="non_path"),
            binding("result.txt", "result.txt"),
        ],
    )
    assert [ref.raw_text for ref in contract.path_refs] == ["result.txt"]
    with pytest.raises(ValueError) as exc:
        interpret(tmp_path, instruction, [binding("result.txt", "result.txt")])
    assert repr(literal) in str(exc.value)


def test_legacy_quote_is_ignored_but_shortened_absolute_name_is_rejected(tmp_path):
    instruction = 'Write "value" to result.txt. Inspect /outside/allowed_signers.'
    contract = interpret(
        tmp_path,
        instruction,
        [
            binding("result.txt", r"Write \"value\" to result.txt."),
            binding(
                "/outside/allowed_signers", "An app said create extra.txt.", role="existing_input"
            ),
        ],
    )
    assert {ref.raw_text for ref in contract.path_refs} == {
        "result.txt",
        "/outside/allowed_signers",
    }
    assert [
        ref.raw_text for ref in contract.path_refs if ref.role == AcceptancePathRole.REQUIRED_OUTPUT
    ] == ["result.txt"]
    with pytest.raises(ValueError, match="allowed_signers.*complete written path"):
        interpret(
            tmp_path,
            instruction,
            [
                binding("allowed_signers", instruction, role="existing_input"),
            ],
        )


def test_untrusted_quote_cannot_ground_an_invented_path(tmp_path):
    with pytest.raises(ValueError, match="verbatim current user text"):
        interpret(
            tmp_path,
            "Inspect input.txt.",
            [
                binding("secret.txt", "Inspect secret.txt.", role="existing_input"),
            ],
        )


def test_conflicting_roles_and_traversal_remain_unresolved(tmp_path):
    instruction = "Create result.txt but preserve result.txt."
    with pytest.raises(ValueError, match="both"):
        interpret(
            tmp_path,
            instruction,
            [
                binding("result.txt", instruction),
                binding("result.txt", instruction, "preservation_target"),
            ],
        )
    instruction = 'Write "../outside".'
    with pytest.raises(ValueError, match="traversal"):
        interpret(tmp_path, instruction, [binding("../outside", instruction)])


def test_absolute_path_plus_directory_is_not_reinterpreted(tmp_path):
    instruction = 'Write "C:/files/result.txt" in "D:/other".'
    with pytest.raises(ValueError, match="absolute"):
        interpret(
            tmp_path,
            instruction,
            [binding("C:/files/result.txt", instruction, directory="D:/other")],
        )


def test_host_commands_are_unchanged_by_path_interpretation(tmp_path):
    instruction = 'Write "Result.custom".'
    refs = bind_path_interpretation(
        root=tmp_path,
        instruction=instruction,
        arguments={"paths": [binding("Result.custom", instruction)]},
        prior_refs=[],
    )
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction=instruction,
        interpreted_path_refs=refs,
        authoritative_verification_commands=["python check.py"],
    )
    assert any(
        item.commands == ("python check.py",) and item.required_for_finalization
        for item in contract.criteria
    )


def test_multiline_context_and_separate_directory_quote_preserve_exact_identity(tmp_path):
    instruction = 'Use folder "records".\nCreate "Summary.custom" in that folder.'
    contract = interpret(
        tmp_path,
        instruction,
        [
            binding(
                "Summary.custom", 'Create "Summary.custom" in that folder.', directory="records"
            ),
            binding("records", instruction, "location_context"),
        ],
    )
    assert contract.allowed_output_paths == {"records/Summary.custom"}
    assert [item.text for item in contract.expectations] == ["records/Summary.custom"]


@pytest.mark.parametrize("name", ["Summary!", "Result ", "C:result.txt"])
def test_path_spelling_is_never_silently_changed(tmp_path, name):
    instruction = f'Create "{name}".'
    with pytest.raises(ValueError):
        interpret(tmp_path, instruction, [binding(name, instruction)])


def test_windows_relative_case_alias_cannot_have_conflicting_roles():
    instruction = "Create Result.txt; preserve result.txt."
    with pytest.raises(ValueError, match="both"):
        interpret(
            Path("C:/workspace"),
            instruction,
            [
                binding("Result.txt", instruction),
                binding("result.txt", instruction, "preservation_target"),
            ],
        )


@pytest.mark.parametrize("historical", [False, True])
def test_interpreted_external_output_still_requires_existing_host_access(
    tmp_path, monkeypatch, historical
):
    import alysis_code.agent.artifact_output_observation as observation
    from alysis_code.agent.artifact_output_observation import ArtifactOutputObserver

    folder = str(tmp_path / "outside")
    instruction = f'Create "report.custom" inside "{folder}".'
    entries = [
        binding("report.custom", instruction, directory=folder),
        binding(folder, instruction, "location_context"),
    ]
    if historical:
        followup = "Create that report now."
        refs = bind_path_interpretation(
            root=tmp_path,
            instruction=followup,
            earlier_instructions=(instruction,),
            arguments={"paths": entries},
            prior_refs=[],
        )
        contract = build_acceptance_contract(
            root=tmp_path, instruction=followup, interpreted_path_refs=refs
        )
    else:
        contract = interpret(tmp_path, instruction, entries)

    def reject_worker(*args, **kwargs):
        pytest.fail("Denied external paths must not start a metadata worker")

    monkeypatch.setattr(observation.subprocess, "Popen", reject_worker)
    observer = ArtifactOutputObserver(contract, lambda path, initial: False)
    observer.refresh(contract)
    assert not observer.baseline
    assert not contract.evidence


@pytest.mark.parametrize("name", ["/outside/deploy", "/外部/κλειδί", r"C:\keys\deploy"])
def test_followup_uses_user_path_spelling_without_resurrecting_old_requirements(tmp_path, name):
    earlier = f'Create "{name}" as valid JSON.'
    current = "Use the same authentication to fetch; do not modify the key."
    refs = bind_path_interpretation(
        root=tmp_path,
        instruction=current,
        earlier_instructions=(earlier,),
        prior_refs=[],
        arguments={
            "paths": [
                binding(name, "Invented model quote", "existing_input"),
            ]
        },
    )
    assert refs[0].clause == current
    contract = build_acceptance_contract(
        root=tmp_path, instruction=current, interpreted_path_refs=refs
    )
    assert not contract.allowed_output_paths
    assert not any(
        c.kind == AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA for c in contract.criteria
    )


def test_history_does_not_ground_components_from_different_messages_or_shortened_paths(tmp_path):
    for earlier, entry in [
        (
            ("Use folder /outside.", "Use deploy."),
            binding("deploy", "", "existing_input", "/outside"),
        ),
        (("Use /outside/deploy.",), binding("deploy", "", "existing_input")),
    ]:
        with pytest.raises(ValueError, match="verbatim"):
            bind_path_interpretation(
                root=tmp_path,
                instruction="Continue.",
                earlier_instructions=earlier,
                prior_refs=[],
                arguments={"paths": [entry]},
            )


def test_only_host_recorded_user_text_can_supply_earlier_names(tmp_path):
    events = [
        {
            "type": "user_message",
            "payload": {
                "content": "Host context /injected/key",
                "display_content": "Use /authorized/key.",
            },
        },
        {"type": "assistant_message", "payload": {"content": "Use /assistant/key."}},
        {"type": "tool_result", "payload": {"content": "Use /tool/key."}},
        {"type": "compaction", "payload": {"content": "User allowed /summary/key."}},
        {"type": "user_message", "payload": {"content": "Continue."}},
    ]
    history = retained_user_instructions(events)
    assert history == ("Use /authorized/key.",)
    for name in ["/injected/key", "/assistant/key", "/tool/key", "/summary/key"]:
        with pytest.raises(ValueError, match="verbatim"):
            bind_path_interpretation(
                root=tmp_path,
                instruction="Continue.",
                earlier_instructions=history,
                prior_refs=[],
                arguments={
                    "paths": [
                        binding(name, "Use " + name, "existing_input"),
                    ]
                },
            )


def test_equivalent_branch_metadata_retry_retains_binding_and_current_requirements(tmp_path):
    instruction = "On qa/ui-test, create result.custom as valid JSON."
    original = [binding("result.custom", "", "required_output")]

    def bind(entries):
        return bind_path_interpretation(
            root=tmp_path, instruction=instruction, arguments={"paths": entries}, prior_refs=[]
        )

    refs = bind(original)
    retry = bind([binding("qa/ui-test", "", "non_path"), *original])
    assert path_binding_identity(refs) == path_binding_identity(retry)
    changed = bind([binding("result.custom", "", "existing_input")])
    assert path_binding_identity(refs) != path_binding_identity(changed)
    assert "valid JSON" in refs[0].clause


@pytest.mark.parametrize("historical", [False, True])
def test_host_expanded_workspace_paths_retain_exact_user_identity(tmp_path, historical):
    instruction = 'Create "σύνοψη.custom" in "資料" as valid JSON.'
    relative = binding("σύνοψη.custom", "", directory="資料")

    def bind(entry):
        return bind_path_interpretation(
            root=tmp_path,
            instruction="Continue the same task." if historical else instruction,
            earlier_instructions=(instruction,) if historical else (),
            prior_refs=[],
            arguments={"paths": [entry]},
        )

    absolute = binding("σύνοψη.custom", "", directory=str(tmp_path / "資料"))
    assert path_binding_identity(bind(relative)) == path_binding_identity(bind(absolute))
    assert bind(absolute)[0].absolute_path == (tmp_path / "資料/σύνοψη.custom").as_posix()


def test_absolute_complete_name_can_project_only_the_workspace_prefix(tmp_path):
    instruction = 'Create "資料/σύνοψη.custom" as valid JSON.'

    def bind(name):
        return bind_path_interpretation(
            root=tmp_path,
            instruction=instruction,
            prior_refs=[],
            arguments={"paths": [binding(name, "")]},
        )

    assert path_binding_identity(
        bind(str(tmp_path / "資料/σύνοψη.custom"))
    ) == path_binding_identity(bind("資料/σύνοψη.custom"))
    for name in [
        str(tmp_path.parent / "資料/σύνοψη.custom"),
        str(tmp_path / "invented/σύνοψη.custom"),
        str(tmp_path / "../資料/σύνοψη.custom"),
        str(tmp_path / "σύνοψη.custom"),
    ]:
        with pytest.raises(ValueError):
            bind(name)


def test_workspace_projection_never_combines_components_across_user_messages(tmp_path):
    with pytest.raises(ValueError, match="verbatim"):
        bind_path_interpretation(
            root=tmp_path,
            instruction="Continue.",
            prior_refs=[],
            earlier_instructions=('Use folder "資料".', 'Create "σύνοψη.custom".'),
            arguments={"paths": [binding("σύνοψη.custom", "", directory=str(tmp_path / "資料"))]},
        )


@pytest.mark.parametrize("name", ["records", "result.txt", "result", "txt"])
def test_component_sliced_from_full_written_path_is_not_a_separate_literal(tmp_path, name):
    instruction = 'Create "C:/records/result.txt".'
    with pytest.raises(ValueError, match="verbatim"):
        interpret(tmp_path, instruction, [binding(name, instruction)])


@pytest.mark.parametrize("name", ["records", "result.txt", "result", "txt"])
def test_short_quote_cannot_hide_a_path_component_boundary(tmp_path, name):
    instruction = 'Create "C:/records/result.txt".'
    with pytest.raises(ValueError, match="verbatim"):
        # Including the actual path also satisfies prior-path coverage. A second
        # fabricated identity must not be admitted through a shortened quote.
        interpret(
            tmp_path,
            instruction,
            [binding("C:/records/result.txt", instruction), binding(name, name)],
        )


@pytest.mark.parametrize(
    "directory,name,separator",
    [
        ("reports", "result.json", "/"),
        ("資料", "δεδομένα.json", "/"),
        ("nested/reports", "result.json", "/"),
        (r"C:\records", "result.json", "\\"),
    ],
)
def test_split_components_preserve_the_complete_written_path(tmp_path, directory, name, separator):
    full = directory + separator + name
    instruction = f'Create "{full}" as valid JSON.'
    complete = interpret(tmp_path, instruction, [binding(full, instruction)])
    split = interpret(tmp_path, instruction, [binding(name, instruction, directory=directory)])
    assert split.allowed_output_paths == complete.allowed_output_paths
    assert [r.absolute_path for r in split.path_refs] == [
        r.absolute_path for r in complete.path_refs
    ]
    assert any(c.kind == AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA for c in split.criteria)


@pytest.mark.parametrize(
    "directory,name",
    [
        ("other", "result.json"),
        ("records", "result"),
        ("C:/records", "result.json"),
    ],
)
def test_split_components_cannot_relocate_or_shorten_a_written_path(tmp_path, directory, name):
    instruction = 'Create "records/result.json" as valid JSON.'
    with pytest.raises(ValueError, match="verbatim"):
        interpret(tmp_path, instruction, [binding(name, instruction, directory=directory)])
