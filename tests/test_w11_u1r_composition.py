"""LR-29 composition controls, authored for later separately authorized execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.agent.acceptance_contract import (
    AcceptancePathRole,
    _iter_clauses,
    build_acceptance_contract,
)


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_quoted_path_keeps_clause_words_and_literal_spacing(quote: str) -> None:
    path = "out/input; and save  two.txt"
    first = f"Read {quote}{path}{quote}"
    assert _iter_clauses([first + " and create report.txt."]) == [
        first,
        "create report.txt.",
    ]
    text = first + " and create report.txt."
    assert _iter_clauses([text], split_actions=False) == [text]


def test_protected_literal_cannot_supply_attempt_predicate() -> None:
    literal = "attempt to read; and save  literal"
    first = f"Discuss {literal}"
    assert _iter_clauses([first + " and create report.txt."], protected_literals=(literal,)) == [
        first,
        "create report.txt.",
    ]


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_attempt_words_in_quoted_filename_do_not_supply_predicate(quote: str) -> None:
    first = f"Read {quote}out/attempt to read  input.txt{quote}"
    assert _iter_clauses([first + " and create report.txt."]) == [
        first,
        "create report.txt.",
    ]


@pytest.mark.parametrize("quote", ["", '"', "'", "`"])
@pytest.mark.parametrize(
    "source,target",
    [
        (
            r"C:\Users\test\Windows App\input.txt",
            r"C:\Users\test\Windows App\output.txt",
        ),
        (
            r"\\server\share\Windows App\input.txt",
            r"\\server\share\Windows App\output.txt",
        ),
    ],
)
def test_drive_and_unc_paths_keep_input_output_roles(
    tmp_path: Path, quote: str, source: str, target: str
) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction=f"Read {quote}{source}{quote} and create {quote}{target}{quote}.",
    )
    assert {ref.display_path: ref.role for ref in contract.path_refs} == {
        source.replace("\\", "/"): AcceptancePathRole.EXISTING_INPUT,
        target.replace("\\", "/"): AcceptancePathRole.REQUIRED_OUTPUT,
    }


def test_attempted_continuation_keeps_references_without_required_output(tmp_path: Path) -> None:
    clause = "Attempt to read outside/input.txt and create outside/denied.txt"
    assert _iter_clauses([clause + "; then create report.txt."]) == [
        clause,
        "create report.txt.",
    ]
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction=clause + "; then create report.txt.",
    )
    roles = {ref.display_path: ref.role for ref in contract.path_refs}
    assert roles["outside/input.txt"] == AcceptancePathRole.UNKNOWN_REFERENCE
    assert roles["outside/denied.txt"] == AcceptancePathRole.UNKNOWN_REFERENCE
    assert roles["report.txt"] == AcceptancePathRole.REQUIRED_OUTPUT
    assert contract.allowed_output_paths == {"report.txt"}


def test_git_and_shell_compatibility_exports_keep_canonical_error_objects() -> None:
    from alysis_code.tools import errors, git, shell
    from alysis_code.tools.fs import StaleFileError

    assert git.GitError is errors.GitError
    assert git.GitDirectoryAccessError is errors.GitDirectoryAccessError
    assert shell.ShellError is errors.ShellError
    assert issubclass(git.GitDirectoryAccessError, errors.GitError)
    assert issubclass(git.GitStaleFileError, errors.GitError)
    assert issubclass(git.GitStaleFileError, StaleFileError)


@pytest.mark.parametrize("command", ["python synthetic.py", "python3 synthetic.py"])
def test_uncertain_cleanup_uses_canonical_shell_error_and_retains_partial_output(
    tmp_path: Path, command: str
) -> None:
    from alysis_code.process_reaping import CommandCleanupError
    from alysis_code.tools import errors, shell

    failure = CommandCleanupError("synthetic", stdout="x" * 20001, stderr="retained stderr")

    class Runner:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run(self, *, cmd: str, **_kwargs):
            self.commands.append(cmd)
            raise failure

    runner = Runner()
    with pytest.raises(errors.ShellError) as caught:
        shell.shell_run(root=tmp_path, cmd=command, runner=runner)
    assert type(caught.value) is errors.ShellError
    assert caught.value.__cause__ is failure
    payload = caught.value.result_payload
    assert payload["cleanup_confirmed"] is False
    assert payload["cancelled"] is False
    assert payload["truncated"] is True
    assert payload["stdout"].startswith("x" * 20000)
    assert payload["stderr"] == "retained stderr"
    assert runner.commands == [command]
