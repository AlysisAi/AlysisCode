"""A plugin turned off after the host read its state drops out of the session, not the session."""

from __future__ import annotations

import hashlib
import io
from typing import Any

import pytest

from alysis_code.config import AppConfig
from alysis_code.extensions.install import disable_plugin, install_plugin
from alysis_code.extensions.registry import load_registry
from alysis_code.ide import stdio_bridge
from alysis_code.ide.health import capabilities_payload
from alysis_code.mcp import github_host
from alysis_code.mcp.github_discovery import ACTIONS as GITHUB_DISCOVERY_ACTIONS
from alysis_code.mcp.github_host import GITHUB_MCP_ACTION
from alysis_code.mcp.google_host import GOOGLE_MCP_ACTION, SERVICES
from tests.fixtures.github_host.bridge import FixtureSession
from tests.fixtures.github_repository_policy import explicit_all
from tests.test_github_discovery import BOOTSTRAP as GITHUB_V2_BOOTSTRAP
from tests.test_github_mcp_plugin import CATALOG as GITHUB_CATALOG
from tests.test_google_mcp_plugin import catalogs as google_catalogs
from tests.test_ide_host_actions import _request, _response_for


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "alysis_code.extensions.install._run_git",
        lambda *_a, **_k: pytest.fail("No network for bundled install"),
    )
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="fixture"))
    root = tmp_path / "workspace"
    root.mkdir()
    for source in ["alysis.github", *(f"alysis.google_{s}" for s in SERVICES)]:
        install_plugin(source=source, repo_root=root, trust_prompt=lambda _: True)
    return root


@pytest.fixture
def all_policy(installed):
    """Main's explicit, stored "all repositories" policy (#7), pinned to the installed GitHub descriptor.

    GitHub's repository actions need this sidecar: without it they are withheld (fail closed), so a
    test that expects them passes it explicitly. Read while GitHub is still on; a policy never brings
    back a plugin that is off or needs review.
    """
    return explicit_all(installed)


def _create(
    root, *, drop: bool | None, policy: dict[str, Any] | None, google=None, github=True, v2=False
) -> dict[str, Any]:
    out = io.StringIO()
    bridge = stdio_bridge.StdioBridge(stdout=out, create_session_fn=FixtureSession)
    google = google_catalogs() if google is None else google
    actions = [GOOGLE_MCP_ACTION] if google else []
    if github:
        actions.extend(sorted(GITHUB_DISCOVERY_ACTIONS) if v2 else [GITHUB_MCP_ACTION])
    params: dict[str, Any] = {
        "workspace": str(root),
        "mode": "auto",
        "workspace_trusted": True,
        "host_capabilities": {"protocol_version": "1", "actions": actions},
        **({"google_mcp": google} if google else {}),
        **({"host_mcp": GITHUB_V2_BOOTSTRAP if v2 else GITHUB_CATALOG} if github else {}),
        **({"drop_inactive_host_plugins": drop} if drop is not None else {}),
        **({"github_repository_policy": policy} if policy is not None else {}),
    }
    try:
        bridge.process_line(_request("session.create", params, "create"))
        return _response_for(out, "create")
    finally:
        bridge.close()


def test_the_engine_advertises_the_opt_in():
    feature = capabilities_payload()["features"]["host_plugin_drop"]
    assert feature == {
        "version": 1,
        "session_param": "drop_inactive_host_plugins",
        "result": "host_plugins_dropped",
    }


def test_a_turned_off_plugin_is_dropped_and_reported(installed):
    disable_plugin(plugin_id="alysis.google_gmail", repo_root=installed)
    disable_plugin(plugin_id="alysis.github", repo_root=installed)

    # No policy: GitHub off with its stored policy is pinned separately below.
    created = _create(installed, drop=True, policy=None)

    assert created["ok"], created
    session = created["result"]
    assert sorted(c["service"] for c in session["google_mcp"]) == sorted(set(SERVICES) - {"gmail"})
    assert "host_mcp" not in session
    assert sorted(session["host_actions"]["actions"]) == [GOOGLE_MCP_ACTION]
    assert [(d["plugin_id"], d["code"]) for d in session["host_plugins_dropped"]] == [
        ("alysis.github", "github_plugin_inactive"),
        ("alysis.google_gmail", "google_plugin_inactive"),
    ]


def test_dropping_every_google_plugin_removes_the_google_action(installed, all_policy):
    for service in SERVICES:
        disable_plugin(plugin_id=f"alysis.google_{service}", repo_root=installed)

    created = _create(installed, drop=True, policy=all_policy)

    assert created["ok"], created
    session = created["result"]
    assert "google_mcp" not in session
    assert session["host_actions"]["actions"] == [GITHUB_MCP_ACTION]
    assert len(session["host_plugins_dropped"]) == len(SERVICES)


def test_nothing_dropped_reports_an_empty_list(installed, all_policy):
    created = _create(installed, drop=True, policy=all_policy)
    assert created["ok"], created
    assert created["result"]["host_plugins_dropped"] == []
    assert len(created["result"]["google_mcp"]) == len(SERVICES)


@pytest.mark.parametrize("drop", [None, False])
def test_without_the_opt_in_a_turned_off_plugin_still_fails_the_session(
    installed, all_policy, drop
):
    disable_plugin(plugin_id="alysis.google_gmail", repo_root=installed)
    created = _create(installed, drop=drop, policy=all_policy)
    assert not created["ok"]
    assert "host_plugins_dropped" not in str(created)


def test_a_malformed_catalog_still_fails_with_the_opt_in(installed, all_policy):
    bad = google_catalogs()
    bad[0]["tools"][0]["name"] = "Bad Name"
    created = _create(installed, drop=True, policy=all_policy, google=bad)
    assert not created["ok"]
    assert created["error"]["code"] == "invalid_google_mcp"


def _ship_changed_access(monkeypatch, plugin_id: str, old: str, new: str) -> None:
    """A newer app ships one built-in descriptor whose endpoint changed: it needs review."""
    registry = load_registry()
    entry = github_host.find_by_id(registry, plugin_id)
    shipped = github_host.bundled_manifest
    content = shipped(entry).replace(old.encode(), new.encode())
    digest = hashlib.sha256(content).hexdigest()
    entry.package_sha256, entry.commit = digest, f"sha256:{digest}"
    monkeypatch.setattr(github_host, "load_registry", lambda: registry)
    monkeypatch.setattr(
        github_host, "bundled_manifest", lambda e: content if e.id == plugin_id else shipped(e)
    )


def test_a_github_update_that_needs_review_is_dropped(installed, monkeypatch):
    _ship_changed_access(
        monkeypatch,
        "alysis.github",
        "https://api.githubcopilot.com/mcp/",
        "https://example.com/mcp/",
    )
    created = _create(installed, drop=True, policy=None)
    assert created["ok"], created
    assert [d["plugin_id"] for d in created["result"]["host_plugins_dropped"]] == ["alysis.github"]
    assert created["result"]["host_actions"]["actions"] == [GOOGLE_MCP_ACTION]
    assert not _create(installed, drop=None, policy=None)["ok"]


def test_a_google_update_that_needs_review_drops_that_service_alone(
    installed, all_policy, monkeypatch
):
    _ship_changed_access(
        monkeypatch,
        "alysis.google_gmail",
        "https://gmailmcp.googleapis.com/mcp/v1",
        "https://example.com/mcp/v1",
    )
    created = _create(installed, drop=True, policy=all_policy)
    assert created["ok"], created
    assert [d["plugin_id"] for d in created["result"]["host_plugins_dropped"]] == [
        "alysis.google_gmail"
    ]
    assert sorted(c["service"] for c in created["result"]["google_mcp"]) == sorted(
        set(SERVICES) - {"gmail"}
    )


@pytest.mark.parametrize("turned_off", [True, False])
def test_v2_github_is_dropped_with_its_discovery_actions_only_when_off(
    installed, all_policy, turned_off
):
    if turned_off:
        disable_plugin(plugin_id="alysis.github", repo_root=installed)
    # An active GitHub keeps its discovery actions only with its explicit policy.
    created = _create(installed, drop=True, policy=None if turned_off else all_policy, v2=True)
    assert created["ok"], created
    actions = set(created["result"]["host_actions"]["actions"])
    dropped = [d["plugin_id"] for d in created["result"]["host_plugins_dropped"]]
    if turned_off:
        assert dropped == ["alysis.github"]
        assert not actions & GITHUB_DISCOVERY_ACTIONS
        assert "host_mcp" not in created["result"]
    else:
        assert dropped == []
        assert GITHUB_DISCOVERY_ACTIONS <= actions
        assert created["result"]["host_mcp"]["protocol_version"] == 2


@pytest.mark.parametrize("v2", [False, True])
def test_github_on_without_a_repository_policy_withholds_its_actions_and_is_not_a_dropped_plugin(
    installed, v2
):
    """No sidecar is not "all": the opt-in never grants repository actions (#7, fail closed)."""
    created = _create(installed, drop=True, policy=None, v2=v2)

    assert created["ok"], created
    session = created["result"]
    actions = set(session["host_actions"]["actions"])
    assert GITHUB_MCP_ACTION not in actions
    assert not actions & GITHUB_DISCOVERY_ACTIONS
    assert "host_mcp" not in session
    assert actions == {GOOGLE_MCP_ACTION}
    # A missing policy isn't an inactive plugin, so it isn't reported as one.
    assert session["host_plugins_dropped"] == []


@pytest.mark.parametrize("v2", [False, True])
@pytest.mark.parametrize("why", ["turned_off", "needs_review"])
def test_github_off_after_main_sent_its_policy_drops_github_not_the_session(
    installed, all_policy, monkeypatch, why, v2
):
    """The race this opt-in exists for, with #7: main read GitHub as on and sent its stored policy.

    GitHub is dropped and reported like any inactive plugin: no repository action, no host_mcp,
    and the policy grants nothing. The session and its other plugins continue. Without the opt-in
    the session still fails, as before.
    """
    if why == "turned_off":
        disable_plugin(plugin_id="alysis.github", repo_root=installed)
    else:
        _ship_changed_access(
            monkeypatch,
            "alysis.github",
            "https://api.githubcopilot.com/mcp/",
            "https://example.com/mcp/",
        )

    created = _create(installed, drop=True, policy=all_policy, v2=v2)

    assert created["ok"], created
    session = created["result"]
    assert [d["plugin_id"] for d in session["host_plugins_dropped"]] == ["alysis.github"]
    assert set(session["host_actions"]["actions"]) == {GOOGLE_MCP_ACTION}
    assert "host_mcp" not in session
    assert len(session["google_mcp"]) == len(SERVICES)
    assert not _create(installed, drop=None, policy=all_policy, v2=v2)["ok"]
