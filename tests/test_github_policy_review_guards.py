"""Review guard gaps through raw provider -> run_turn -> broker, and raw startup."""

import io
import json

import pytest

from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from tests.test_github_repository_policy_production import run_case, sidecar
from tests.test_ide_host_actions import _request, _response_for

pytest_plugins = ["tests.test_github_mcp_plugin"]


def previews(raw):
    return [
        e["payload"].get("result_preview", "")
        for line in raw.splitlines()
        if (e := json.loads(line)).get("type") == "tool_call_completed"
    ]


@pytest.mark.parametrize(
    "repository,account,allowed",
    [
        ("acme/allowed", "fixture", True),
        ("ACME/Allowed", "FIXTURE", True),
        ("acme/other", "fixture", False),
        ("acme/allowed", "other", False),
    ],
)
def test_native_result_is_checked_after_dispatch(
    installed, monkeypatch, repository, account, allowed
):
    requests, host, raw, _ = run_case(
        installed,
        monkeypatch,
        "responses",
        False,
        [("github_pr_create", json.dumps(dict(base="main", head="feature", title="t", body="")))],
        host_reply=lambda *_: dict(
            repository=repository,
            account_login=account,
            url=f"https://github.com/{repository}/pull/1",
            number=1,
        ),
    )
    assert len(host) == 1 and len(requests) >= 2
    result = " ".join(previews(raw))
    assert ("github_repository_policy_denied" not in result) is allowed
    if not allowed:
        assert '"dispatch_state": "unknown"' in result
        assert '"retryable": false' in result


@pytest.mark.parametrize("change", [None, "repository", "account", "next_page"])
def test_list_result_is_checked_at_real_broker(installed, monkeypatch, change):
    def row(name):
        return dict(name=name, url="https://github.com/" + name, private=False)

    result = dict(account="fixture", repositories=[row("acme/allowed")], next_page=None)
    if change == "repository":
        result["repositories"] = [row("acme/other")]
    elif change == "account":
        result["account"] = "other"
    elif change == "next_page":
        result["next_page"] = 2
    _, host, raw, _ = run_case(
        installed,
        monkeypatch,
        "responses",
        False,
        [("github_repositories", "{}")],
        host_reply=lambda *_: result,
    )
    assert len(host) == 1
    delivered = json.loads(previews(raw)[-1])
    assert ("error" not in delivered) is (change is None)
    if change is not None:
        assert delivered["dispatch_state"] == "unknown"
        assert "repositories" not in delivered


@pytest.mark.parametrize(
    "tool,args",
    [
        ("github_pr_list", {"head": "owner:branch"}),
        ("github_pr_list", {"base": "owner:branch"}),
        ("github_pr_list", {"head": "feature..bad"}),
        ("github_git_push", {"branch": "feature..bad"}),
        ("github_repositories", {"page": 0}),
        ("github_repositories", {"page": 10001}),
    ],
)
def test_invalid_branch_or_page_never_reaches_host(installed, monkeypatch, tool, args):
    _, host, raw, _ = run_case(
        installed, monkeypatch, "responses", False, [(tool, json.dumps(args))]
    )
    assert host == []
    assert "github_repository_policy_denied" in " ".join(previews(raw))


def test_wrong_manifest_pin_refused_before_agent_construction(installed, monkeypatch):
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="fixture"))
    calls = []

    def construct(**kwargs):
        calls.append(kwargs)
        raise AssertionError("Wrong manifest pin reached agent construction")

    out = io.StringIO()
    bridge = stdio_bridge.StdioBridge(stdout=out, create_session_fn=construct)
    try:
        bridge.process_line(
            _request(
                "session.create",
                dict(
                    workspace=str(installed),
                    workspace_trusted=True,
                    github_repository_policy={
                        **sidecar(installed),
                        "plugin_manifest_sha256": "f" * 64,
                    },
                ),
                "create",
            )
        )
        reply = _response_for(out, "create")
        assert reply["ok"] is False
        assert reply["error"]["code"] == "github_repository_policy_denied"
        assert calls == []
    finally:
        bridge.close()
