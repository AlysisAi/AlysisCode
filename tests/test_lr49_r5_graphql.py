"""R5 GraphQL admission only: all GH dispatch is captured, never executed."""

import shlex

import pytest
from test_lr49_extra_bypasses import Vector, gate_harness

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason

gate_harness = gate_harness
READ = "query Read { viewer { login } }"
WRITE = 'mutation Write { closeIssue(input:{issueId:"INERT_ONLY"}) { clientMutationId } }'


def command(query, fields=()):
    return (
        "gh api graphql -f "
        + shlex.quote("query=" + query)
        + "".join(" -f " + shlex.quote(field) for field in fields)
    )


CASES = (
    [
        ("comment-" + repr(end), command(READ + " # c" + end + WRITE, ["operationName=Write"]))
        for end in ["\r", "\n", "\r\n", "\u2028", "\u2029", "\x85"]
    ]
    + [
        (
            "block-string-mutation",
            command('query Read { search(query:"""mutation Write { x }""") { count } }'),
        ),
        (
            "escaped-quote-mutation",
            command(r'query Read { search(query:"before \"mutation\" after") { count } }'),
        ),
        (
            "escaped-quote-then-write",
            command(r'query Read { search(query:"before \" after") { count } }' + WRITE),
        ),
        (
            "fragment-then-write",
            command("fragment F on User { login } " + WRITE, ["operationName=Write"]),
        ),
        (
            "query-fragment-write",
            command(READ + " fragment F on User { login } " + WRITE, ["operationName=Write"]),
        ),
        (
            "two-queries-second",
            command(READ + " query Second { viewer { id } }", ["operationName=Second"]),
        ),
        (
            "two-queries-first",
            command(READ + " query Second { viewer { id } }", ["operationName=Read"]),
        ),
        ("two-queries-unselected", command(READ + " query Second { viewer { id } }")),
        ("two-anonymous", command("{ viewer { login } } { viewer { id } }")),
        ("comment-mutation", command(READ + " # mutation")),
        ("comment-query", command(READ + " # query")),
        ("comment-subscription", command(READ + " # subscription\r")),
        ("string-subscription", command('query Read { search(query:"subscription") { count } }')),
        ("string-mutation", command('query Read { search(query:"mutation") { count } }')),
        ("selected-missing", command(READ, ["operationName=Missing"])),
        ("selected-empty", command(READ, ["operationName="])),
        ("selected-anonymous", command("{ viewer { login } }", ["operationName=Read"])),
        ("duplicate-selection", command(READ, ["operationName=Read", "operationName=Read"])),
        ("duplicate-query", command(READ, ["query=" + READ])),
        ("nested-query-field", command(READ, ["query[extra]=" + WRITE])),
        ("nested-selection", command(READ, ["operationName[]=Write"])),
        ("unknown-field", command(READ, ["unresolved=@fixture"])),
        ("query-file", "gh api graphql -F query=@fixture.graphql"),
        ("query-stdin", "gh api graphql -F query=@-"),
        ("raw-query-file", "gh api graphql -f query=@fixture.graphql"),
        ("selection-file", command(READ) + " -F operationName=@fixture"),
        ("typed-extra-file", command(READ) + " -F input=@fixture"),
        ("typed-literal-query", "gh api graphql -F " + shlex.quote("query=" + READ)),
        ("input-file", command(READ) + " --input fixture.json"),
        ("input-stdin", command(READ) + " --input -"),
        (
            "unresolved-variables",
            command("query Read($x: String!) { search(query:$x) { count } }", ["x=fixture"]),
        ),
        (
            "unresolved-fragment",
            command("query Read { ...Other } fragment Other on Query { viewer { id } }"),
        ),
        ("unresolved-directive", command("query Read @unknown { viewer { id } }")),
        ("unterminated-string", command('query Read { search(query:"unfinished) { count } }')),
        ("unclosed-selection", command("query Read { viewer { login }")),
        ("extra-close", command(READ + "}")),
        ("empty-query", command("")),
        ("empty-selection", command("query Read {}")),
        ("number-name-join", command("query Read { search(limit:1other:2) { count } }")),
        ("number-exponent-join", command("query Read { search(limit:1e2other:3) { count } }")),
        ("wrong-method", command(READ) + " -X DELETE"),
        ("duplicate-method", command(READ) + " -X POST -X GET"),
        ("redirect-input", command(READ) + " < fixture.graphql"),
        ("unresolved-header", command(READ) + " -H X-Fixture:opaque"),
        ("block-string-opaque", command('query Read { search(query:"""fixture""") { count } }')),
        ("depth-limit", command("query Read " + "{ a " * 35 + "}" * 35)),
        ("query-string-endpoint", "gh api " + shlex.quote("graphql?query=" + WRITE)),
        (
            "absolute-endpoint",
            "gh api https://api.github.com/graphql -X GET -f " + shlex.quote("query=" + WRITE),
        ),
        ("encoded-endpoint", "gh api %67raphql -X GET -f " + shlex.quote("query=" + WRITE)),
        ("dot-endpoint", "gh api ./graphql -X GET -f " + shlex.quote("query=" + WRITE)),
        ("placeholder-endpoint", "gh api '{owner}' -X GET -f " + shlex.quote("query=" + WRITE)),
        ("branch-endpoint", "gh api '{branch}' -X GET -f " + shlex.quote("query=" + WRITE)),
    ]
    + [
        ("unicode-comment-" + repr(end), command(READ + " # harmless" + end))
        for end in ["\u2028", "\u2029", "\x85"]
    ]
)


@pytest.mark.parametrize("name,cmd", CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize("route", ["full", "auto", "ssh"])
def test_unresolved_graphql_requires_its_own_person_card(gate_harness, name, cmd, route):
    _, surface, dispatched, _, run = gate_harness
    error = run(Vector(name, cmd), route)
    assert isinstance(error, ApprovalDeclinedError)
    assert not dispatched
    cards = [r for r in surface.requests if r.kind == "shell_run"]
    assert len(cards) == 1
    assert cards[0].command == cmd and cards[0].preview == cmd
    assert cards[0].metadata["always_ask_person"]
    assert [r.kind for r in surface.requests].count("sandbox_access") == (route == "ssh")


READS = [
    command("{ viewer { login } }"),
    command(READ),
    command(READ, ["operationName=Read"]),
    command("query { viewer { login } }"),
    command("query Read { search(limit:1.25e2) { count } }"),
    command('query Read { search(query:"permutation subscriptionCount") { count } }'),
    command('query Read { repository(owner:"δοκιμή", name:"fixture") { name } }'),
    command(r'query Read { repository(owner:"a\"b", name:"fixture") { name } }'),
    command("query Read { alias: viewer { login } }"),
    command('query Read { search(input:{name:"fixture",flags:[true,false],limit:10}) { count } }'),
] + [
    command("# start" + end + READ + " # end" + end, ["operationName=Read"])
    for end in ["\r", "\n", "\r\n"]
]


@pytest.mark.parametrize("cmd", READS)
@pytest.mark.parametrize("route", ["full", "auto", "ssh"])
def test_single_literal_query_retains_read_exception(gate_harness, cmd, route):
    _, surface, dispatched, _, run = gate_harness
    assert github_write_reason(cmd, dialect="posix") is None
    assert run(Vector("single-query", cmd), route) is None
    assert len(dispatched) == 1 and dispatched[0]["cmd"] == cmd
    assert not [r for r in surface.requests if r.kind == "shell_run"]
    assert [r.kind for r in surface.requests].count("sandbox_access") == (route == "ssh")


@pytest.mark.parametrize("dialect", ["cmd", "powershell"])
@pytest.mark.parametrize("cmd", [command(READ), command(READ + " # c\r" + WRITE)])
def test_windows_conservative_gate_is_preserved(gate_harness, dialect, cmd):
    _, surface, dispatched, _, run = gate_harness
    assert isinstance(run(Vector("windows-graphql", cmd, dialect), "full"), ApprovalDeclinedError)
    assert not dispatched
    assert [r.command for r in surface.requests if r.kind == "shell_run"] == [cmd]


@pytest.mark.parametrize("field_flag", ["-f", "--raw-field", "-fquery=", "--raw-field=query="])
@pytest.mark.parametrize("method", ["", " -X GET", " --method=POST"])
def test_bounded_field_spellings_and_methods(gate_harness, field_flag, method):
    if field_flag.endswith("="):
        cmd = "gh api graphql " + field_flag + shlex.quote(READ)
    else:
        cmd = "gh api graphql " + field_flag + " " + shlex.quote("query=" + READ)
    cmd += " --raw-field=operationName=Read" + method
    _, surface, dispatched, _, run = gate_harness
    assert run(Vector("literal-field-spelling", cmd), "full") is None
    assert len(dispatched) == 1 and dispatched[0]["cmd"] == cmd
    assert not surface.requests


@pytest.mark.parametrize(
    "cmd",
    [
        "gh api repos/owner/repo",
        "gh api repos/owner/repo -X GET -f per_page=10",
        "gh api repos/owner/repo/issues --method HEAD",
    ],
)
def test_literal_rest_reads_are_preserved(gate_harness, cmd):
    _, surface, dispatched, _, run = gate_harness
    assert run(Vector("rest-read", cmd), "full") is None
    assert len(dispatched) == 1 and not surface.requests
