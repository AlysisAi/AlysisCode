from __future__ import annotations

import os
from pathlib import Path

import pytest

from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.capabilities import build_capabilities_payload
from alysis_code.host_actions import (
    GITHUB_WRITE_ACTIONS,
    HostActionError,
    normalize_host_action_arguments,
    normalize_host_action_result,
)
from alysis_code.ide.health import capabilities_payload
from alysis_code.remote_sync import create_pr_or_mr, find_existing_pr_or_mr, push_base, push_branch
from alysis_code.surface import ApprovalDecision
from tests.test_host_actions import _ApprovalSurface, _build_tools
from tests.test_ide_host_actions import (
    _make_bridge,
    _respond,
    _response_for,
    _start_host_action,
    _wait_for,
)

BINDING = {"repository": "org/repo", "account_login": "selected-user"}
REPO_RESULT = {
    **BINDING,
    "url": "https://github.com/org/repo",
    "private": True,
    "default_branch": "main",
}


def test_github_contract_advertised_in_cli_and_live_handshake() -> None:
    assert build_capabilities_payload()["features"]["github-host-actions-v1"] is True
    assert {"host_action_requested", "host_action_cancelled"} <= set(
        capabilities_payload()["events"]
    )
    assert "host.action.respond" in capabilities_payload()["methods"]
    assert capabilities_payload()["features"]["github_host_actions"] == {
        "version": 1,
        "actions": [
            "github.repo.read",
            "github.pr.list",
            "github.pr.create",
            "github.git.fetch",
            "github.git.pull",
            "github.git.push",
        ],
    }


def test_github_tools_require_negotiation_and_readonly_hides_mutations(tmp_path: Path) -> None:
    actions = ("github.repo.read", "github.pr.list", *GITHUB_WRITE_ACTIONS)
    assert not any(name.startswith("github_") for name in _build_tools(tmp_path))
    tools = _build_tools(tmp_path, mode="readonly", actions=actions, handler=lambda *_: REPO_RESULT)
    assert {name for name in tools if name.startswith("github_")} == {
        "github_repo_read",
        "github_pr_list",
    }
    assert tools["github_repo_read"].run({}) == REPO_RESULT


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        (
            "github.pr.create",
            {"base": "main", "head": "feature/test", "title": "Change", "body": ""},
        ),
        ("github.git.fetch", {}),
        ("github.git.pull", {"branch": "main"}),
        ("github.git.push", {"branch": "feature/test"}),
    ],
)
def test_github_mutations_require_host_approval_authority_without_duplicate_prompt(
    tmp_path: Path, action: str, arguments: dict
) -> None:
    calls = []
    tool_name = action.replace(".", "_")

    def handler(action, args):
        calls.append((action, args))
        return {**BINDING, "output": "done"}

    tools = _build_tools(tmp_path, actions=(action,), handler=handler)
    with pytest.raises(AgentRuntimeError, match="Host-managed approval"):
        tools[tool_name].run(arguments)
    assert calls == []
    surface = _ApprovalSurface(ApprovalDecision(allow=False))
    tools = _build_tools(tmp_path, actions=(action,), handler=handler, surface=surface)
    tools[tool_name].run(arguments)
    assert calls == [
        (action, {**arguments, **({"draft": True} if action == "github.pr.create" else {})})
    ]
    assert surface.requests == []  # Main approves the bound account/repository at dispatch.


@pytest.mark.parametrize(
    "branch",
    [
        "-f",
        ":main",
        "main:other",
        "+main",
        "main..next",
        "a.lock",
        "a/.secret",
        "a@{x}",
        "refs/*",
        "a b",
        "a\\b",
        "a\n",
        "a/",
        "@",
        "a//b",
    ],
)
def test_github_branch_rejects_refspecs_options_and_invalid_names(branch: str) -> None:
    # Leading + is a legal Git branch, but is not a permitted broker branch/refspec.
    with pytest.raises(HostActionError):
        normalize_host_action_arguments("github.git.push", {"branch": branch})


@pytest.mark.parametrize(
    ("action", "args"),
    [
        ("github.repo.read", {"url": "https://github.com/other/repo"}),
        ("github.pr.list", {"repository": "other/repo"}),
        ("github.git.fetch", {"remote": "https://example.com"}),
        ("github.git.push", {"branch": "main", "force": True}),
        ("github.pr.create", {"base": "main", "head": "feat", "title": "x", "body": "x" * 4097}),
    ],
)
def test_github_arguments_cannot_override_target_or_escape_bounds(action: str, args: dict) -> None:
    with pytest.raises(HostActionError):
        normalize_host_action_arguments(action, args)


def test_github_results_bound_repository_urls_and_reject_credentials() -> None:
    assert normalize_host_action_result("github.repo.read", REPO_RESULT) == REPO_RESULT
    for extra in (
        {"token": "synthetic"},
        {"url": "https://github.com/other/repo"},
        {"private": "true"},
    ):
        with pytest.raises(HostActionError):
            normalize_host_action_result("github.repo.read", {**REPO_RESULT, **extra})
    with pytest.raises(HostActionError):
        normalize_host_action_result("github.git.fetch", {**BINDING, "output": "x" * 16385})
    with pytest.raises(HostActionError):
        normalize_host_action_result(
            "github.pr.list", {**BINDING, "pull_requests": [{}] * 31, "truncated": True}
        )


@pytest.mark.parametrize("policy", ["linked-only", "disconnected", "", "unknown"])
def test_desktop_remote_sync_never_runs_local_git_or_gh(
    monkeypatch, tmp_path: Path, policy: str
) -> None:
    monkeypatch.setenv("ALYSIS_DESKTOP_GITHUB_POLICY", policy)
    monkeypatch.setenv("ALYSIS_GITHUB_CLI", "/selected/absolute/gh")
    monkeypatch.setenv("ALYSIS_DESKTOP_GITHUB_EXPECTED_LOGIN", "selected-user")
    monkeypatch.setattr(
        "alysis_code.remote_sync._run", lambda *_a, **_kw: pytest.fail("desktop fallback attempted")
    )
    monkeypatch.setattr(
        "alysis_code.remote_sync.shutil.which", lambda *_a: pytest.fail("PATH gh attempted")
    )
    assert push_branch(tmp_path, remote="origin", branch="feat")[0] is False
    assert push_base(tmp_path, remote="origin", base_branch="main")[0] is False
    for provider in ("github", "gitlab"):
        assert (
            find_existing_pr_or_mr(
                tmp_path, provider=provider, base_branch="main", head_branch="feat"
            )[0]
            is False
        )
        result = create_pr_or_mr(
            tmp_path, provider=provider, base_branch="main", head_branch="feat", title="PR", body=""
        )
        assert result[0] is False
        assert "host broker" in result[-1]


def test_github_broker_round_trip_uses_existing_session_job_and_workspace_fences(
    tmp_path: Path, monkeypatch
) -> None:
    out, bridge, session_id, captured, results, errors = _make_bridge(
        tmp_path,
        monkeypatch,
        actions=("github.repo.read",),
        action="github.repo.read",
        explicit_github_all=True,
    )
    try:
        event = _start_host_action(out, bridge, session_id)
        assert event["payload"]["workspace_root"] == os.fspath(tmp_path.resolve())
        assert event["payload"]["job_id"]
        assert captured["host_action_capabilities"] == frozenset({"github.repo.read"})
        _respond(
            bridge, event, request_id="bad", result=REPO_RESULT, workspace_fence="wf_" + "f" * 32
        )
        assert _response_for(out, "bad")["ok"] is False
        _respond(bridge, event, request_id="good", result=REPO_RESULT)
        assert _response_for(out, "good")["ok"] is True
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert results == [REPO_RESULT]
        assert not errors
        _respond(bridge, event, request_id="duplicate", result=REPO_RESULT)
        assert _response_for(out, "duplicate")["ok"] is False
    finally:
        bridge.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_github_broker_refusal_and_cancellation_reach_the_agent(
    tmp_path: Path, monkeypatch, cancel: bool
) -> None:
    from tests.test_ide_host_actions import _request

    out, bridge, session_id, _captured, results, errors = _make_bridge(
        tmp_path,
        monkeypatch,
        actions=("github.git.push",),
        action="github.git.push",
        arguments={"branch": "feature/test"},
        explicit_github_all=True,
    )
    try:
        event = _start_host_action(out, bridge, session_id)
        if cancel:
            bridge.process_line(_request("session.cancel", {"session_id": session_id}, "cancel"))
            _wait_for(out, lambda line: line.get("type") == "host_action_cancelled")
            _respond(bridge, event, request_id="stale", result={**BINDING, "output": "done"})
            assert _response_for(out, "stale")["ok"] is False
        else:
            _respond(
                bridge,
                event,
                request_id="denied",
                error={
                    "code": "github_account_changed",
                    "message": "Selected GitHub account changed.",
                    "retryable": False,
                },
            )
            assert _response_for(out, "denied")["ok"] is True
        _wait_for(out, lambda line: line.get("type") == "message_end")
        assert results == []
        assert len(errors) == 1
        assert errors[0].code == ("host_action_cancelled" if cancel else "host_action_failed")
        if not cancel:
            assert errors[0].host_error_code == "github_account_changed"
    finally:
        bridge.close()
