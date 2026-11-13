"""A filename that reads like a verb is data, and so are the words around it.

Path detection recognises how a path is written. It gives no mention a role,
so no word (in a filename or next to one, in any language) can turn a mention
into an output, an input or a file to keep unchanged.
"""

import pytest
from acceptance_declared import declared_contract

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionEnforcement,
    AcceptanceCriterionKind,
    _extract_path_refs,
    build_acceptance_contract,
)
from alysis_code.agent.acceptance_contract import (
    AcceptancePathRole as Role,
)


def _assert_mentions_without_roles(root, instruction, paths):
    refs = _extract_path_refs(root=root, texts=[instruction])
    assert {ref.display_path: ref.role for ref in refs} == dict.fromkeys(
        paths, Role.UNKNOWN_REFERENCE
    )
    contract = build_acceptance_contract(root=root, instruction=instruction)
    assert {ref.display_path: ref.role for ref in contract.path_refs} == dict.fromkeys(
        paths, Role.UNKNOWN_REFERENCE
    )
    assert contract.allowed_output_paths == set()
    assert not any(
        c.kind
        in {
            AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH,
            AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH,
        }
        for c in contract.criteria
    )
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("quote", ["", '"', "'", "`"])
@pytest.mark.parametrize("first", ["read.py", "docs/read.py"])
def test_compare_filename_is_data_in_parser_and_contract(tmp_path, quote, first):
    instruction = f"Compare {quote}{first}{quote} to {quote}result.txt{quote}."
    _assert_mentions_without_roles(tmp_path, instruction, [first, "result.txt"])


@pytest.mark.parametrize("quote", ["", '"', "'", "`"])
@pytest.mark.parametrize("verb", ["Read", "Read from", "Διάβασε το", "读取"])
@pytest.mark.parametrize(
    "source",
    [
        "docs/read.py",
        r"C:\Users\test\Windows App\read.py",
        r"\\server\share\Windows App\read.py",
    ],
)
def test_a_literal_input_path_is_kept_whole_and_given_no_role(tmp_path, quote, verb, source):
    instruction = f"{verb} {quote}{source}{quote} and write {quote}result.txt{quote}."
    _assert_mentions_without_roles(tmp_path, instruction, [source.replace("\\", "/"), "result.txt"])


@pytest.mark.parametrize(
    "instruction",
    [
        "Attempt to read read.py and create denied.txt.",
        "Preserve read.py unchanged, and write denied.txt.",
        "Create read.py and denied.txt.",
        "Discuss read.py and denied.txt.",
        "Μην αλλάξεις το read.py, γράψε το denied.txt.",
    ],
)
def test_no_wording_gives_a_mention_a_role(tmp_path, instruction):
    _assert_mentions_without_roles(tmp_path, instruction, ["read.py", "denied.txt"])


def test_declared_roles_are_the_only_roles(tmp_path):
    instruction = "Preserve read.py unchanged, and write result.txt."
    contract = declared_contract(
        root=tmp_path,
        instruction=instruction,
        roles={"read.py": "preservation_target", "result.txt": "required_output"},
    )
    assert {ref.display_path: ref.role for ref in contract.path_refs} == {
        "read.py": Role.PRESERVATION_TARGET,
        "result.txt": Role.REQUIRED_OUTPUT,
    }
    assert contract.allowed_output_paths == {"result.txt"}
    output = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    assert output.enforcement == AcceptanceCriterionEnforcement.HARD
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("quote", ['"', "'", "`"])
@pytest.mark.parametrize("folder", ["Design library", "共有 データ", "and write; keep"])
def test_quoted_absolute_output_keeps_its_complete_file_identity(tmp_path, quote, folder):
    target = tmp_path / folder / "résultat.custom"
    instruction = f"Write the result to {quote}{target}{quote}."
    detected = build_acceptance_contract(root=tmp_path / "main", instruction=instruction)
    assert [ref.absolute_path for ref in detected.path_refs] == [str(target)]
    assert [expectation.text for expectation in detected.expectations] == []
    contract = declared_contract(
        root=tmp_path / "main", instruction=instruction, roles={str(target): "required_output"}
    )
    assert [ref.absolute_path for ref in contract.path_refs] == [str(target)]
    assert [expectation.text for expectation in contract.expectations] == [str(target)]
    assert list(tmp_path.iterdir()) == []
