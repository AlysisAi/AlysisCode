"""Shell delimiter handling and approval checks for unresolved commands."""

import json
import subprocess

import pytest
from test_lr49_extra_bypasses import Vector, gate_harness

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason

gate_harness = gate_harness


@pytest.mark.parametrize("space", ["\u00a0", "\u2003", "\u202f", "\v", "\f"])
@pytest.mark.parametrize("dialect", ["posix", "cmd"])
def test_non_shell_delimiter_does_not_certify_unknown_executable(space, dialect):
    command = "echo" + space + "opaque fixture"
    assert github_write_reason(command, dialect=dialect), repr(command)


@pytest.mark.parametrize("space", ["\u00a0", "\u2003", "\u202f", "\v", "\f"])
@pytest.mark.parametrize("route", ["full", "auto", "ssh"])
def test_real_guard_does_not_dispatch_non_echo_name(gate_harness, space, route, record_property):
    root, surface, dispatched, client, run = gate_harness
    command = "echo" + space + "opaque fixture"
    error = run(Vector("non-shell-space", command), route)
    cards = [r for r in surface.requests if r.kind == "shell_run"]
    record_property(
        "admission_evidence",
        json.dumps(
            {
                "command": repr(command),
                "route": route,
                "cards": [r.kind for r in surface.requests],
                "dispatches": len(dispatched),
                "error": type(error).__name__ if error else None,
            }
        ),
    )
    assert not dispatched, "unknown executable passed captured dispatch without person"
    assert len(cards) == 1 and isinstance(error, ApprovalDeclinedError)


@pytest.mark.parametrize("space", ["\u00a0", "\u2003", "\u202f", "\v", "\f"])
def test_posix_semantics_runs_only_fixed_non_echo_stub(tmp_path, space):
    binary = tmp_path / ("echo" + space + "opaque")
    binary.write_text('#!/bin/sh\nprintf "OPAQUE_STUB:%s\\n" "$*"\n')
    binary.chmod(0o700)
    p = subprocess.run(
        ["/bin/sh", "-c", "echo" + space + "opaque fixture"],
        cwd=tmp_path,
        env={"PATH": str(tmp_path), "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert p.returncode == 0, p.stderr
    assert p.stdout == "OPAQUE_STUB:fixture\n", repr(p.stdout)


@pytest.mark.parametrize(
    "command",
    [
        "echo fixture",
        "printf '%s\\n' 'fixture'",
        "pwd",
        "cat fixture.txt",
        "head fixture.txt",
        "tail fixture.txt",
        "wc fixture.txt",
        "ls",
        "basename fixture.txt",
        "dirname fixture.txt",
        "uname",
        "id",
        "whoami",
        "readlink fixture",
        "realpath fixture.txt",
        "stat fixture.txt",
        "cut fixture.txt",
        "tr 'a' 'b'",
        "grep fixture fixture.txt",
        "gh pr view 123",
        "gh api repos/owner/a --method GET",
    ],
)
def test_bounded_literal_read_or_data_positive(command):
    assert github_write_reason(command, dialect="posix") is None
