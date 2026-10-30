"""R3 uncertainty controls: quoted data is data; unresolved code needs a person."""

import pytest
from test_lr49_extra_bypasses import Vector, gate_harness

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason

# Re-export the inert production-entry fixture.
gate_harness = gate_harness


@pytest.mark.parametrize(
    "command",
    [
        "echo $LR49_VALUE",
        'echo "$LR49_VALUE"',
        "echo ${LR49_VALUE}",
        'echo "$(printf fixture)"',
        "echo `printf fixture`",
        "echo fi\\\nxture",
        'echo "fi\\\nxture"',
        "echo fi\\xture",
        "custom-program fixture",
        "pytest -q",
        "npm test",
        "env LABEL=fixture echo fixture",
        "sh -c 'echo fixture'",
        "gh --unknown-option pr view 1",
        "gh pr --unknown-option view 1",
    ],
)
@pytest.mark.parametrize("route", ["full", "auto", "ssh"])
def test_unresolved_operation_never_falls_through(gate_harness, command, route):
    _, surface, dispatched, _, run = gate_harness
    error = run(Vector("r3-unresolved", command), route)
    assert isinstance(error, ApprovalDeclinedError)
    assert not dispatched
    cards = [r for r in surface.requests if r.kind == "shell_run"]
    assert len(cards) == 1 and cards[0].command == command
    assert cards[0].metadata["always_ask_person"]


@pytest.mark.parametrize(
    "command",
    [
        "echo '$LR49_VALUE'",
        "echo '$(git push origin main)'",
        "echo '`git push origin main`'",
        "printf '%s\\n' 'git push'",
        "echo literal",
    ],
)
def test_single_quoted_metacharacters_remain_literal_data(command):
    assert github_write_reason(command, dialect="posix") is None


@pytest.mark.parametrize(
    "command",
    [
        "gh api *",
        "gh api ?",
        "gh api [a-z]*",
        "gh api ~/fixture",
        "find . -ex?c git push origin main ';'",
        "sort --compress-program=./opaque-helper --buffer-size=1 fixture.txt",
        "sort --compress-program ./opaque-helper --buffer-size=1 fixture.txt",
        "sort --compress-prog=./opaque-helper fixture.txt",
        "rg --pre ./opaque-helper fixture",
        "file --uncompress fixture.gz",
    ],
)
@pytest.mark.parametrize("route", ["full", "auto", "ssh"])
def test_pathname_expansion_and_helper_programs_require_person(gate_harness, command, route):
    root, surface, dispatched, _, run = gate_harness
    # Text-only cwd entries: expansion can turn names into executable options.
    for name in ["-XPOST", "-fdescription=fixture", "gists", "-exec", "fixture.txt"]:
        (root / name).write_text("inert")
    error = run(Vector("r3-expansion-helper", command), route)
    assert isinstance(error, ApprovalDeclinedError)
    assert not dispatched
    cards = [r for r in surface.requests if r.kind == "shell_run"]
    assert len(cards) == 1 and cards[0].command == command
    assert cards[0].metadata["always_ask_person"]


@pytest.mark.parametrize("command", ["echo '*'", 'echo "?[]~"', "printf '%s' '*'"])
def test_quoted_glob_characters_are_literal_data(command):
    assert github_write_reason(command, dialect="posix") is None
