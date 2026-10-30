import json
import subprocess

import pytest
from test_lr49_extra_bypasses import Vector, gate_harness

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason
from alysis_code.shell_command_lexer import split_simple_commands

gate_harness = gate_harness
SEPARATORS = ["\u00a0", "\v", "\f", "\u2003"]


@pytest.mark.parametrize(
    "separator", SEPARATORS, ids=["nbsp", "vertical-tab", "form-feed", "em-space"]
)
@pytest.mark.parametrize("route", ["full", "auto", "ssh"])
def test_non_shell_whitespace_cannot_certify_relative_executable(
    gate_harness, separator, route, record_property
):
    root, surface, dispatched, client, run = gate_harness
    command = "echo" + separator + "./publish"
    program = root / command
    program.parent.mkdir()
    program.write_text("#!/bin/sh\nprintf 'INERT_ONLY\\n'\n")
    program.chmod(0o700)
    error = run(Vector("non-shell-whitespace", command), route)
    observed = {
        "command": command,
        "route": route,
        "error": repr(error),
        "dispatch": dispatched,
        "program": str(program),
        "person_command_cards": [
            {"command": x.command, "metadata": x.metadata}
            for x in surface.requests
            if x.kind == "shell_run"
        ],
        "all_card_kinds": [x.kind for x in surface.requests],
        "reviewer_calls": len(client.calls),
    }
    record_property("observed", json.dumps(observed, sort_keys=True))
    assert not dispatched, {
        "command": repr(command),
        "error": repr(error),
        "dispatch": dispatched,
        "cards": [(x.kind, x.metadata) for x in surface.requests],
    }
    assert isinstance(error, ApprovalDeclinedError)
    assert any(
        x.kind == "shell_run" and x.metadata.get("always_ask_person") for x in surface.requests
    )


@pytest.mark.parametrize(
    "separator", SEPARATORS, ids=["nbsp", "vertical-tab", "form-feed", "em-space"]
)
def test_real_posix_parser_preserves_non_shell_whitespace(tmp_path, separator):
    # Literal relative path, NOT a PATH shadow, Git binary, network, or credential.
    directory = tmp_path / ("echo" + separator + ".")
    directory.mkdir()
    program = directory / "publish"
    program.write_text("#!/bin/sh\nprintf 'REVIEWER_RELATIVE_PROGRAM_EXECUTED\\n'\n")
    program.chmod(0o700)
    command = "echo" + separator + "./publish"
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        env={"PATH": "/nonexistent", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "REVIEWER_RELATIVE_PROGRAM_EXECUTED\n"
    # R4: retain real-shell proof; the old parser-mismatch observation is
    # now an explicit refusal assertion, not an expected production bypass.
    assert split_simple_commands(command, dialect="posix", strict=True) is None
    assert github_write_reason(command, dialect="posix")


@pytest.mark.parametrize(
    "command",
    [
        "echo literal",
        "printf '%s' 'fixture'",
        "cat README.md",
        "pwd",
        "gh pr view 1",
        "gh api repos/owner/repo --method GET",
        "echo 'git push origin main'",
        'echo "\u00a0"',
    ],
)
def test_ordinary_posix_literal_reads_remain_readable(command, gate_harness):
    assert github_write_reason(command, dialect="posix") is None
    root, surface, dispatched, _, run = gate_harness
    (root / "README.md").write_text("inert read fixture")
    assert run(Vector("ordinary-read", command), "full") is None
    assert len(dispatched) == 1 and dispatched[0]["cmd"] == command
    assert not [x for x in surface.requests if x.kind == "shell_run"]


@pytest.mark.parametrize(
    "dialect,command",
    [
        ("posix", "git status"),
        ("posix", "git ls-remote origin"),
        ("posix", "gh api repos/o/r -XDELETE"),
        (
            "posix",
            "gh api graphql -f 'query=mutation { deleteIssue(input:{id:\"x\"}){clientMutationId}}'",
        ),
        ("posix", "gh alias set push '!git push'"),
        ("posix", "GH pr view 1"),
        ("cmd", "GIT.EXE status"),
        ("cmd", "gh api repos/o/r --method GET"),
        ("cmd", "echo a & git push origin main"),
        ("cmd", "echo !CMD!"),
        ("cmd", "echo %CMD%"),
        ("cmd", "ec^ho fixture"),
        ("powershell", "Write-Output fixture"),
        ("powershell", "& ('g'+'it') push origin main"),
        ("powershell", "[scriptblock]::Create('git push origin main').Invoke()"),
        ("powershell", "Set-Alias x git; x push origin main"),
        ("powershell", "Get-Content README.md"),
    ],
)
def test_other_unresolved_and_windows_spellings_require_person(gate_harness, dialect, command):
    _, surface, dispatched, _, run = gate_harness
    error = run(Vector("independent-extra", command, dialect), "full")
    assert isinstance(error, ApprovalDeclinedError)
    assert not dispatched
    assert any(
        x.kind == "shell_run" and x.metadata.get("always_ask_person") for x in surface.requests
    )
