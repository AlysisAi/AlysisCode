"""Real parser/contract regressions for filename-supplied prefix predicates."""

import pytest

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionEnforcement,
    AcceptanceCriterionKind,
    _extract_path_refs,
    build_acceptance_contract,
)
from alysis_code.agent.acceptance_contract import (
    AcceptancePathRole as Role,
)


@pytest.mark.parametrize("quote", ["", '"', "'", "`"])
@pytest.mark.parametrize("first", ["read.py", "docs/read.py"])
def test_compare_filename_is_data_in_parser_and_contract(tmp_path, quote, first):
    instruction = f"Compare {quote}{first}{quote} to {quote}result.txt{quote}."
    expected = {first: Role.VERIFICATION_CHECKER, "result.txt": Role.VERIFICATION_CHECKER}
    refs = _extract_path_refs(root=tmp_path, texts=[instruction])
    assert {ref.display_path: ref.role for ref in refs} == expected
    contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert {ref.display_path: ref.role for ref in contract.path_refs} == expected
    assert contract.allowed_output_paths == set()
    assert not any(
        c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH for c in contract.criteria
    )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("quote", ["", '"', "'", "`"])
@pytest.mark.parametrize("verb", ["Read", "Read from"])
@pytest.mark.parametrize(
    "source,project_outputs",
    [
        ("docs/read.py", {"result.txt"}),
        (r"C:\Users\test\Windows App\read.py", set()),
        (r"\\server\share\Windows App\read.py", set()),
    ],
)
def test_legitimate_read_preserves_literal_input_and_output(
    tmp_path, quote, verb, source, project_outputs
):
    instruction = f"{verb} {quote}{source}{quote} and write {quote}result.txt{quote}."
    expected = {source.replace("\\", "/"): Role.EXISTING_INPUT, "result.txt": Role.REQUIRED_OUTPUT}
    assert {
        ref.display_path: ref.role for ref in _extract_path_refs(root=tmp_path, texts=[instruction])
    } == expected
    contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert {ref.display_path: ref.role for ref in contract.path_refs} == expected
    assert contract.allowed_output_paths == project_outputs
    output = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    assert output.enforcement == (
        AcceptanceCriterionEnforcement.HARD
        if project_outputs
        else AcceptanceCriterionEnforcement.ADVISORY
    ), "An external input does not bind an unqualified output to the selected project."
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "instruction,expected,outputs",
    [
        (
            "Attempt to read read.py and create denied.txt.",
            {"read.py": Role.UNKNOWN_REFERENCE, "denied.txt": Role.UNKNOWN_REFERENCE},
            set(),
        ),
        (
            "Preserve read.py unchanged, and write result.txt.",
            {"read.py": Role.PRESERVATION_TARGET, "result.txt": Role.REQUIRED_OUTPUT},
            {"result.txt"},
        ),
        (
            "Create read.py and result.txt.",
            {"read.py": Role.REQUIRED_OUTPUT, "result.txt": Role.REQUIRED_OUTPUT},
            {"read.py", "result.txt"},
        ),
        (
            "Discuss read.py and result.txt.",
            {"read.py": Role.UNKNOWN_REFERENCE, "result.txt": Role.UNKNOWN_REFERENCE},
            set(),
        ),
    ],
)
def test_prefix_masking_preserves_role_precedence(tmp_path, instruction, expected, outputs):
    contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert {ref.display_path: ref.role for ref in contract.path_refs} == expected
    assert contract.allowed_output_paths == outputs
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("quote", ['"', "'", "`"])
@pytest.mark.parametrize("folder", ["Design library", "共有 データ", "and write; keep"])
def test_quoted_absolute_output_keeps_its_complete_file_identity(tmp_path, quote, folder):
    target = tmp_path / folder / "résultat.custom"
    instruction = f"Write the result to {quote}{target}{quote}."
    contract = build_acceptance_contract(root=tmp_path / "main", instruction=instruction)
    assert [ref.absolute_path for ref in contract.path_refs] == [str(target)]
    assert [expectation.text for expectation in contract.expectations] == [str(target)]
    assert list(tmp_path.iterdir()) == []
