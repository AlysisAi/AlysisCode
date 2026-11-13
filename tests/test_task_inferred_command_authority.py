"""Task-text recommendations cannot invent exact-command authority."""

from pathlib import Path

import pytest
from acceptance_declared import declared_contract
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionEnforcement,
    AcceptanceCriterionKind,
    build_acceptance_contract,
)
from alysis_code.agent.verification_commands import _matching_effective_verification_commands
from alysis_code.config import AppConfig
from alysis_code.verification_contract import (
    VerificationCommandProvenance,
    VerificationCommandRequirement,
    build_verification_command_spec,
)
from alysis_code.verify_gate import (
    ResolvedVerifyCommands,
    required_verify_commands,
    resolve_task_aware_verify_command_selection,
    verification_command_specs_for_selection,
)


def _selection(instruction: str):
    return resolve_task_aware_verify_command_selection(
        cfg=AppConfig(model="offline"),
        verify_cmd=None,
        task={"estimated_files": ["app.py"], "acceptance_criteria": [instruction]},
        selection=ResolvedVerifyCommands(
            commands=("pytest -q",),
            source="config.verify_commands_fallback",
            contract_type="generic_fallback",
        ),
    )


@pytest.mark.parametrize(
    "command",
    [
        "cd tests && pytest -q",
        "PYTHONPATH=src pytest -q",
        "pytest tests/test_case.py -k 'Case and not Other'",
    ],
)
def test_lossy_task_recommendation_cannot_create_an_explicit_command_mandate(command: str) -> None:
    selection = _selection("Run " + command)
    assert selection.contract_type == "task_inferred"
    assert selection.commands
    assert required_verify_commands(selection) == ()
    assert all(
        spec.provenance == VerificationCommandProvenance.INFERRED_HEURISTIC
        and spec.requirement == VerificationCommandRequirement.ADVISORY
        for spec in verification_command_specs_for_selection(selection)
    )
    # Keep the existing exact cwd/environment/selector fence. Executing the
    # requested command must not certify a different, partially extracted one.
    assert not _matching_effective_verification_commands(
        observed_command=command, effective_verification_commands=list(selection.commands)
    )


@pytest.mark.parametrize(
    ("source", "contract_type"),
    [
        ("cli.verify_cmd", "explicit_override"),
        ("config.verify_commands", "repo_native"),
        ("environment.authoritative_verification_commands", "authoritative_override"),
        ("task_refinement.explicit_user_command", "task_acceptance"),
    ],
)
def test_structured_explicit_command_sources_stay_required(source: str, contract_type: str) -> None:
    command = "cd tests && pytest -q"
    spec = build_verification_command_spec(command, source=source, contract_type=contract_type)
    assert spec.original_text == command
    assert spec.requirement == VerificationCommandRequirement.REQUIRED
    assert spec.provenance != VerificationCommandProvenance.INFERRED_HEURISTIC


def test_exact_quoted_acceptance_remains_hard_even_when_recommendation_is_advisory(
    tmp_path: Path,
) -> None:
    command = "cd tests && pytest -q"
    instruction = "Run `" + command + "`"
    selection = _selection(instruction)
    assert required_verify_commands(selection) == ()
    # The request writes the command line; declared by the model it is required.
    detected = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert detected.request_commands == [command]
    assert not [c for c in detected.criteria if c.commands]
    contract = declared_contract(
        root=tmp_path, instruction=instruction, roles={}, commands=[command]
    )
    matching = [
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.EXPLICIT_COMMAND_IO
    ]
    assert len(matching) == 1
    assert matching[0].commands == (command,)
    assert matching[0].enforcement == AcceptanceCriterionEnforcement.HARD


def test_tool_explicit_command_keeps_requested_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_verify_tool import _build_tools, _cp, _patch_host_execution

    working = tmp_path / "tests"
    working.mkdir()
    observed_launches = []

    def fake_run(command, **kwargs):
        observed_launches.append((command, Path(kwargs["cwd"]), kwargs["shell"]))
        return _cp(returncode=0, stdout="1 passed\n")

    _patch_host_execution(monkeypatch, fake_run)
    command = "cd tests && pytest -q"
    selection = _selection("Run " + command)
    tools = _build_tools(
        tmp_path,
        cfg=AppConfig(model="offline"),
        effective_verification_commands=list(selection.commands),
        verify_command_selection=selection,
    )
    result = tools["verify_run"].run({"commands": [command]})
    assert result["commands"] == [command]
    # The shell executes the intact cd prefix relative to the workspace root.
    assert observed_launches == [(command, tmp_path, True)]
    assert result["command_results"][0]["exit_code"] == 0
