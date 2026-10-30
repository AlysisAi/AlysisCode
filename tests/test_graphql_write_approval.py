"""GraphQL comment and operation boundaries require explicit write approval."""

import json
import shlex
import subprocess

import pytest
from test_lr49_extra_bypasses import Vector, gate_harness

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason
from alysis_code.shell_command_lexer import split_simple_commands

gate_harness = gate_harness
READ = "query Read { viewer { login } }"
WRITE = 'mutation Write { closeIssue(input:{issueId:"SYNTHETIC_NO_LIVE_ID"}) { clientMutationId } }'


def gql_command(ending="\r"):
    return (
        "gh api graphql -f "
        + shlex.quote("query=" + READ + " # inert comment" + ending + WRITE)
        + " -f operationName=Write"
    )


@pytest.mark.parametrize("route", ["full", "auto", "ssh"])
@pytest.mark.parametrize("ending", ["\r", "\n", "\r\n"], ids=["cr", "lf", "crlf"])
def test_graphql_comment_boundary_cannot_hide_selected_mutation(
    gate_harness, route, ending, record_property
):
    _, surface, dispatched, client, run = gate_harness
    command = gql_command(ending)
    error = run(Vector("graphql-line-comment", command), route)
    record_property(
        "observed",
        json.dumps(
            {
                "command": command,
                "route": route,
                "error": repr(error),
                "dispatch": dispatched,
                "cards": [
                    {"kind": r.kind, "command": r.command, "metadata": r.metadata}
                    for r in surface.requests
                ],
                "ai_calls": len(client.calls),
            },
            sort_keys=True,
        ),
    )
    assert not dispatched, (repr(command), error, dispatched)
    assert isinstance(error, ApprovalDeclinedError)
    assert [r.command for r in surface.requests if r.kind == "shell_run"] == [command]


@pytest.mark.parametrize("ending", ["\r", "\n", "\r\n"])
def test_real_shell_preserves_graphql_comment_bytes_in_argument(tmp_path, ending):
    # This gh is a fixed argv printer, never the real CLI and never a network client.
    stub = tmp_path / "gh"
    stub.write_text("#!/bin/sh\nprintf '%s\\0' \"$@\"\n")
    stub.chmod(0o700)
    command = gql_command(ending)
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        env={"PATH": str(tmp_path), "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    actual = result.stdout.split(b"\0")[:-1]
    assert actual == [
        b"api",
        b"graphql",
        b"-f",
        ("query=" + READ + " # inert comment" + ending + WRITE).encode(),
        b"-f",
        b"operationName=Write",
    ]
    parsed = split_simple_commands(command, dialect="posix")
    assert parsed is not None and list(parsed[0].words[1:]) == [v.decode() for v in actual]


CASES = [
    ("posix", 'echo safe # "\n./publish'),
    ("posix", "echo safe # \\\n./publish"),
    ("posix", "echo\r#./publish"),
    ("posix", "echo safe\r./publish"),
    ("posix", "echo safe\r\n./publish"),
    ("posix", "echo 'safe'; ./publish"),
    ("posix", 'echo "safe"; ./publish #"'),
    ("posix", "echo $'safe'; ./publish"),
    ("posix", "echo\ufeff./publish"),
    ("posix", "echo\u200b./publish"),
    ("posix", "echo\u2060./publish"),
    ("posix", "echo\x7f./publish"),
    ("posix", "echo\x1b./publish"),
    ("posix", 'echo "`./publish`"'),
    ("posix", 'echo "$(./publish)"'),
    ("posix", "echo safe\n./publish"),
    ("cmd", "echo safe\r\ngit push origin main"),
    ("cmd", "echo safe\rgit push origin main"),
    ("cmd", "echo safe &\r\ngit push origin main"),
    ("cmd", "echo safe && gh pr create"),
    ("cmd", "echo safe | GIT.EXE push origin main"),
    ("cmd", "echo safe ^\r\n& git push"),
    ("cmd", 'echo "%X%"'),
    ("cmd", 'echo "!X!"'),
    ("cmd", "@echo safe & git push"),
    ("cmd", 'cmd /s /c ""C:\\git.exe" push origin main"'),
    ("cmd", "call echo safe & git push"),
    ("cmd", "echo;git push"),
    ("cmd", "echo\u200b.\\publish"),
    ("cmd", "echo\x7f.\\publish"),
    ("powershell", 'Write-Output "safe" # comment\r git push'),
    ("powershell", "echo safe\u2028git push"),
    ("powershell", "echo safe\u2029git push"),
    ("powershell", "echo safe`\r\n; git push"),
    ("powershell", "& ('gi'+'t') push"),
    ("powershell", "echo 'safe'; & './publish'"),
    ("powershell", "echo @'\ntext\n'@; git push"),
    ("powershell", "echo 'a''b'; git push"),
    ("powershell", 'echo "a`"; git push"'),
    ("powershell", "echo safe\x1cgit push"),
]


@pytest.mark.parametrize("route", ["full", "ssh"])
@pytest.mark.parametrize("dialect,command", CASES)
def test_shell_tokenization_hunt(gate_harness, route, dialect, command):
    _, surface, dispatched, _, run = gate_harness
    error = run(Vector("r4-shell-hunt", command, dialect), route)
    assert isinstance(error, ApprovalDeclinedError), (repr(command), error, dispatched)
    assert not dispatched
    assert [r.command for r in surface.requests if r.kind == "shell_run"] == [command]


NAMES = [
    "日本語.txt",
    "café.txt",
    "cafe\u0301.txt",
    "δοκιμή.txt",
    "space name.txt",
    "nbsp\u00a0name.txt",
    "em\u2003name.txt",
    "cr\rname.txt",
    "lf\nname.txt",
    "crlf\r\nname.txt",
    "hash#name.txt",
    "semi;name.txt",
]


@pytest.mark.parametrize("name", NAMES)
def test_unicode_filename_real_guard_and_actual_read(gate_harness, name):
    root, surface, dispatched, _, run = gate_harness
    (root / name).write_text("INERT_UNICODE_READ\n")
    command = "cat " + shlex.quote(name)
    assert github_write_reason(command, dialect="posix") is None
    assert run(Vector("unicode-read", command), "full") is None
    assert len(dispatched) == 1 and dispatched[0]["cmd"] == command
    assert not [r for r in surface.requests if r.kind == "shell_run"]
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=root,
        env={"PATH": "/usr/bin:/bin", "HOME": str(root), "TMPDIR": str(root)},
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stdout == b"INERT_UNICODE_READ\n"
