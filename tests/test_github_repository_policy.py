"""Finite #7 extractors; U2R corpus binds current-main push schema."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from alysis_code.host_actions import HostActionError
from alysis_code.mcp import github_repository_policy as policy

CORPUS = json.loads(
    (
        Path(__file__).parent / "fixtures/github_repository_policy/repository-policy-v1.json"
    ).read_text()
)


def payload():
    return dict(
        version=1,
        plugin_id="alysis.github",
        plugin_manifest_sha256="a" * 64,
        host="github.com",
        account_login="fixture",
        account_link_revision="00000000-0000-4000-8000-000000000001",
        policy_revision="00000000-0000-4000-8000-000000000002",
        mode="allow_list",
        repositories=[dict(owner="acme", repo="allowed")],
        workspace_repository=dict(owner="acme", repo="allowed"),
    )


def test_corpus_receipt():
    raw = (
        Path(__file__).parent / "fixtures/github_repository_policy/repository-policy-v1.json"
    ).read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "6c9714cf818619f2e05827e674b9b49bb85f7863f78a2c352042aa0afdfbca75"
    )


@pytest.mark.parametrize(
    "patch",
    [
        dict(version=True),
        dict(extra=True),
        dict(host="github.example"),
        dict(mode="all"),
        dict(account_login="Fixture"),
        dict(policy_revision="bad"),
        dict(repositories=[dict(owner="acme", repo="allowed")] * 2),
        dict(workspace_repository=dict(owner="acme", repo="..")),
    ],
)
def test_closed_sidecar(patch):
    with pytest.raises(HostActionError):
        policy.RepositoryPolicy.parse({**payload(), **patch})


def test_binding_is_immutable_and_private_copy():
    value = payload()
    bound = policy.RepositoryPolicy.parse(value)
    value["repositories"].clear()
    assert bound.payload() == payload()
    bound.payload()["repositories"].clear()
    assert bound.payload() == payload()


@pytest.mark.parametrize(
    "name,args,expected",
    [
        (
            "github_api_read",
            dict(path="branches"),
            dict(path="branches", query=dict(page="1", per_page="100")),
        ),
        ("github_api_read", dict(path=""), dict(path="")),
        (
            "github_api_write",
            dict(
                path="pulls",
                method="POST",
                body=dict(base="main", head="work", title=" t ", body="\nbody\t"),
            ),
            dict(
                path="pulls",
                method="POST",
                body=dict(base="main", head="work", title=" t ", body="\nbody\t", draft=True),
            ),
        ),
        (
            "github.pr.create",
            dict(base="main", head="work", title="t", body="", draft=False),
            dict(base="main", head="work", title="t", body="", draft=False),
        ),
        ("connectors.github.repositories", {}, dict(page=1)),
    ],
)
def test_normalization_precedes_approval(name, args, expected):
    original = copy.deepcopy(args)
    assert policy.normalize_arguments(name, args) == expected
    assert args == original


@pytest.mark.parametrize(
    "name,args",
    [
        ("github_api_read", dict(path="", query={})),
        ("github_api_read", dict(path="search")),
        ("github_api_read", dict(path="branches", query=dict(page="01"))),
        ("github_api_write", dict(path="pulls", method="POST", query={}, body={})),
        ("github.pr.create", dict(base="main", head="other:branch", title="t", body="")),
        ("github.pr.create", dict(base="main", head="work", title="😀" * 129, body="")),
        ("github.pr.create", dict(base="main", head="work", title="\ufeff\x85", body="")),
        ("github.git.push", dict(branch="a\ufeffb")),
        ("github.git.push", dict(branch="a\ud800")),
        ("connectors.github.repositories", dict(page=True)),
    ],
)
def test_unsupported_arguments_never_normalize(name, args):
    with pytest.raises(HostActionError):
        policy.normalize_arguments(name, args)


@pytest.mark.parametrize("row", CORPUS["schemas"], ids=lambda r: r["name"])
def test_pinned_definitions(row):
    if row["source_kind"] == "engine_native":
        from alysis_code.tools.registry import copied_tool_parameters

        assert policy.schema_digest(copied_tool_parameters(row["name"])) == row["schema_sha256"]
        policy.check_native_schema(row["action"])
    else:
        assert policy.EFFECTIVE_PINS[row["name"]] == row["effective_schema_sha256"]
        policy.check_definition(
            dict(
                name=row["name"],
                scope="repository",
                readOnly=row["name"] == "github_api_read",
                inputSchema=row["effectiveInputSchema"],
            )
        )


def test_unknown_policy_keys_do_not_leak_values():
    with pytest.raises(HostActionError) as caught:
        policy.RepositoryPolicy.parse({**payload(), "token": "PRIVATE_SENTINEL"})
    assert "PRIVATE_SENTINEL" not in str(caught.value)


def test_forbidden_target_fails_before_target_id_exists():
    bound = policy.RepositoryPolicy.parse(payload())
    with pytest.raises(HostActionError):
        policy.guard_arguments(
            bound,
            "mcp.github.describe",
            dict(
                catalog_id="catalog",
                names=["github_api_read"],
                target=dict(scope="repository", owner="acme", repo="forbidden"),
            ),
            None,
        )


@pytest.mark.parametrize(
    "raw",
    [
        '{"page":1,"page":2}',
        '{"path":"","query":{"page":"1","page":"2"}}',
        '{"body":"\\ud800"}',
        '{"page":NaN}',
    ],
)
def test_scoped_json_refuses_raw_ambiguity(raw):
    from alysis_code.mcp.github_policy_json import argument_scope, arguments

    with argument_scope(True):
        assert arguments(raw, "github_call") == {"_invalid_github_arguments": True}
        assert arguments(raw, "computer_state") is None
    assert arguments(raw, "github_call") is None


def test_sidecar_duplicate_keys_use_real_protocol_parser():
    from alysis_code.ide.protocol import ProtocolError, parse_request_line

    raw = json.dumps(
        dict(
            protocol_version="1",
            id="p",
            method="session.create",
            params=dict(github_repository_policy=payload()),
        )
    )
    raw = raw.replace('"mode": "allow_list"', '"mode":"all","mode":"allow_list"')
    with pytest.raises(ProtocolError):
        parse_request_line(raw)
    # Unrelated namespace retains its existing decoder.
    assert parse_request_line(
        '{"protocol_version":"1","id":"p","method":"health","params":{"x":1,"x":2}}'
    ).params == {"x": 2}


@pytest.mark.parametrize(
    "patch",
    [
        {"account": "another"},
        {"next_page": 2},
        {
            "repositories": [
                {"name": "acme/blocked", "url": "https://github.com/acme/blocked", "private": True}
            ]
        },
    ],
)
def test_restricted_list_result_must_match_bound_page(patch):
    bound = policy.RepositoryPolicy.parse(payload())
    result = dict(
        account="fixture",
        repositories=[
            dict(name="Acme/Allowed", url="https://github.com/Acme/Allowed", private=True)
        ],
        next_page=None,
    )
    policy.guard_result(bound, "connectors.github.repositories", result, {"page": 1})
    with pytest.raises(HostActionError):
        policy.guard_result(
            bound, "connectors.github.repositories", {**result, **patch}, {"page": 1}
        )


def test_query_counts_utf16_without_changing_legacy_whitespace_rules():
    bound = policy.RepositoryPolicy.parse(payload())
    assert policy.guard_arguments(bound, "mcp.github.discover", {"query": "😀" * 128}, None)
    assert policy.guard_arguments(bound, "mcp.github.discover", {"query": "\x01"}, None)
    with pytest.raises(HostActionError):
        policy.guard_arguments(bound, "mcp.github.discover", {"query": "😀" * 129}, None)


def test_repository_order_uses_canonical_owner_slash_repo_key():
    # Paired APP parser uses repoKey ordering, not tuple/prefix ordering.
    value = {**payload(), "repositories": [dict(owner="a-b", repo="r"), dict(owner="a", repo="r")]}
    assert policy.RepositoryPolicy.parse(value).payload() == value
    value["repositories"].reverse()
    with pytest.raises(HostActionError):
        policy.RepositoryPolicy.parse(value)
