"""GraphQL comments must not hide mutations from command approval checks."""

import json
import subprocess

import pytest
from test_lr49_extra_bypasses import Vector, gate_harness

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason
from alysis_code.shell_command_lexer import split_simple_commands

gate_harness = gate_harness


def document(ending):
    return (
        "query Safe { viewer { login } } # inert comment"
        + ending
        + 'mutation Write { addStar(input:{starrableId:"SYNTHETIC_TEST_ID"}) { clientMutationId } }'
    )


def command_for(doc):
    return "gh api graphql -f query='" + doc + "' -f operationName=Write"


@pytest.mark.parametrize("ending", ["\r", "\n", "\r\n"], ids=["CR", "LF", "CRLF"])
def test_mutation_after_graphql_comment_requires_person(ending):
    command = command_for(document(ending))
    assert github_write_reason(command, dialect="posix"), repr(command)


@pytest.mark.parametrize("ending", ["\r", "\n", "\r\n"], ids=["CR", "LF", "CRLF"])
@pytest.mark.parametrize("route", ["full", "auto", "ssh"])
def test_actual_guard_does_not_dispatch_graphql_mutation_after_comment(
    gate_harness, ending, route, record_property
):
    _, surface, dispatched, _, run = gate_harness
    command = command_for(document(ending))
    error = run(Vector("graphql-comment", command), route)
    cards = [r for r in surface.requests if r.kind == "shell_run"]
    record_property(
        "admission_evidence",
        json.dumps(
            {
                "route": route,
                "ending": repr(ending),
                "command": repr(command),
                "cards": [r.kind for r in surface.requests],
                "dispatches": len(dispatched),
                "error": type(error).__name__ if error else None,
            }
        ),
    )
    assert not dispatched, (
        "GraphQL mutation-bearing original bytes reached captured dispatch without a command decision"
    )
    assert len(cards) == 1 and isinstance(error, ApprovalDeclinedError)
    assert cards[0].command == command


@pytest.mark.parametrize("ending", ["\r", "\n", "\r\n"], ids=["CR", "LF", "CRLF"])
def test_inert_posix_function_receives_the_original_graphql_comment_bytes(tmp_path, ending):
    doc = document(ending)
    command = command_for(doc)
    parsed = split_simple_commands(command, dialect="posix")
    assert parsed is not None and parsed[0].words == (
        "gh",
        "api",
        "graphql",
        "-f",
        "query=" + doc,
        "-f",
        "operationName=Write",
    )
    # Shell-byte oracle only: fixed function, no actual gh executable, token or remote connection.
    script = "gh() { /usr/bin/printf '%s\\0' \"$@\"; }; " + command
    result = subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split(b"\0")[:-1] == [
        b"api",
        b"graphql",
        b"-f",
        ("query=" + doc).encode(),
        b"-f",
        b"operationName=Write",
    ]


@pytest.mark.parametrize(
    "query",
    [
        "query Safe { viewer { login } }",
        "query Safe { viewer { login } } # literal mutation\r",
        'query Safe { repository(owner:"mutation", name:"fixture") { name } }',
    ],
)
def test_actual_query_or_literal_mutation_name_is_not_mislabeled(query):
    command = "gh api graphql -f query='" + query + "'"
    # R5's explicit raw-text policy requires a person even for operation
    # keywords used only as comment/string data. The plain query still reads.
    if "mutation" in query:
        assert github_write_reason(command, dialect="posix")
    else:
        assert github_write_reason(command, dialect="posix") is None
