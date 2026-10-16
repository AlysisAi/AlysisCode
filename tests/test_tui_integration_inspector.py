from types import SimpleNamespace

import pytest

from alysis_code.cli_impl.tui import integration_inspector as inspector
from alysis_code.cli_impl.tui.integration_inspector import (
    IntegrationAction,
    IntegrationController,
    IntegrationRow,
    IntegrationSnapshot,
    inspect_mcp,
    snapshot_lines,
)


def server(key="demo", enabled=True):
    return SimpleNamespace(
        id=key,
        enabled=enabled,
        trust="explicit",
        oauth=None,
        env={},
        headers={},
        enabled_for=lambda runtime: enabled,
    )


class FakeManager:
    def __init__(self):
        self.resolved_servers = [server()]
        self.closed = False
        self.runtime_kind = "interactive_chat"
        self.connected = True
        self.enabled = True
        self.calls = []
        self.failures = set()

    @property
    def tool_bindings(self):
        raise AssertionError("Inspection must not materialize tools")

    def catalog_snapshot_metadata(self):
        return {"server_catalogs": [{"server_id": "demo", "exposed_tool_names": ["search"]}]}

    def server_lifecycle_status(self, *, server_id):
        if server_id in self.failures:
            raise RuntimeError("https://user:secret@host/?token=query-secret")
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "connection_state": "connected" if self.connected else "disconnected",
        }

    def disable_server(self, *, server_id):
        self.calls.append(("disable", server_id))
        self.enabled = self.connected = False
        return self.server_lifecycle_status(server_id=server_id)

    def enable_server(self, *, server_id):
        self.calls.append(("enable", server_id))
        self.enabled = self.connected = True
        return self.server_lifecycle_status(server_id=server_id)

    restart_server = enable_server


def test_refresh_tracks_live_connection_and_disabled_configuration():
    manager = FakeManager()
    manager.resolved_servers.append(server("off", False))
    assert [r.state for r in inspect_mcp(manager).rows] == ["connected", "disabled"]
    manager.connected = False
    row = inspect_mcp(manager).rows[0]
    assert row.state == "disconnected"
    assert "currently unavailable" in " ".join(row.details)
    manager.closed = True
    assert inspect_mcp(manager).rows[0].actions == ()


def test_unavailable_manager_is_never_connected():
    result = inspect_mcp(None, configured_servers=[server()])
    assert result.rows[0].state == "unavailable"
    assert result.rows[0].tools == ()
    assert result.notices


def test_partial_failure_and_credentials_are_not_displayed():
    manager = FakeManager()
    manager.resolved_servers += [
        server("bad"),
        server("https://user:secret@host/?token=query-secret"),
    ]
    manager.resolved_servers[0].env = {"API_KEY": "direct-secret"}
    manager.resolved_servers[0].url = "https://user:secret@host/?token=query-secret"
    manager.resolved_servers[0].args = ("--password=argument-secret",)
    manager.failures.add("bad")
    manager.catalog_snapshot_metadata = lambda: {
        "server_catalogs": [
            {
                "server_id": "demo",
                "exposed_tool_names": [
                    "direct-secret",
                    "https://user:secret@host/?token=query-secret",
                ],
            }
        ]
    }
    result = inspect_mcp(manager)
    assert result.rows[0].state == "connected"
    assert result.rows[1].state == "failed"
    rendered = "\n".join(snapshot_lines(result)) + repr(result)
    for secret in ("direct-secret", "query-secret", "argument-secret", "user:secret"):
        assert secret not in rendered
    assert "Check MCP configuration" in rendered


def test_catalog_failure_preserves_lifecycle_observation():
    manager = FakeManager()

    def failed():
        raise RuntimeError("Bearer secret")

    manager.catalog_snapshot_metadata = failed
    result = inspect_mcp(manager)
    assert result.rows[0].state == "connected"
    assert result.rows[0].tools == ()
    assert result.notices
    assert "Bearer secret" not in repr(result)


def test_deferred_actions_cancel_and_success(tmp_path):
    manager = FakeManager()
    controller = IntegrationController(repo_root=tmp_path, manager_provider=lambda: manager)
    action = IntegrationAction("mcp", "demo", "disable")
    assert controller.request(action).status == "deferred"
    assert controller.apply_pending_at_idle(turn_running=True).status == "deferred"
    assert manager.calls == []
    assert controller.cancel_pending().status == "cancelled"
    assert controller.apply_pending_at_idle(turn_running=False).status == "idle"
    assert manager.calls == []
    controller.request(action)
    assert controller.apply_pending_at_idle(turn_running=False).status == "applied"
    assert controller.refresh("mcp").rows[0].state == "disabled"
    controller.request(IntegrationAction("mcp", "demo", "enable"))
    assert controller.apply_pending_at_idle(turn_running=False).status == "applied"
    assert controller.refresh("mcp").rows[0].state == "connected"


def test_pending_action_cannot_cross_session(tmp_path):
    current = [FakeManager()]
    controller = IntegrationController(repo_root=tmp_path, manager_provider=lambda: current[0])
    controller.request(IntegrationAction("mcp", "demo", "disable"))
    old = current[0]
    current[0] = FakeManager()
    assert controller.apply_pending_at_idle(turn_running=False).status == "cancelled"
    assert old.calls == current[0].calls == []


def test_failed_transition_redacts_exception(tmp_path):
    manager = FakeManager()

    def failed(**kwargs):
        raise RuntimeError("authorization=secret")

    manager.restart_server = failed
    controller = IntegrationController(repo_root=tmp_path, manager_provider=lambda: manager)
    controller.request(IntegrationAction("mcp", "demo", "restart"))
    outcome = controller.apply_pending_at_idle(turn_running=False)
    assert outcome.status == "failed"
    assert "authorization=secret" not in repr(outcome)


def test_extension_partial_failure_trust_and_components(monkeypatch, tmp_path):
    record = SimpleNamespace(component_ids={"skills": ["review"], "hooks": ["before-tool"]})
    monkeypatch.setattr(
        inspector, "load_global_state", lambda: SimpleNamespace(installed={"demo": record})
    )

    def unavailable(_):
        raise RuntimeError("token=secret")

    monkeypatch.setattr(inspector, "load_project_state", unavailable)
    monkeypatch.setattr(
        inspector,
        "resolve_active_plugins",
        lambda **kwargs: SimpleNamespace(
            enabled_plugin_ids=frozenset(), untrusted_project_plugin_ids=frozenset({"demo"})
        ),
    )
    result = inspector.inspect_extensions(tmp_path)
    assert result.rows[0].state == "blocked by workspace trust"
    assert "skills: review" in result.rows[0].details
    assert "hooks: before-tool" in result.rows[0].details
    assert result.notices
    assert "token=secret" not in repr(result)


def test_extension_changes_use_existing_boundary_and_require_restart(tmp_path):
    calls = []
    snapshot = IntegrationSnapshot(
        "extensions",
        (IntegrationRow("demo", "demo", False, "disabled", actions=("enable",), scope="project"),),
    )
    controller = IntegrationController(
        repo_root=tmp_path,
        manager_provider=lambda: None,
        extension_reader=lambda path: snapshot,
        extension_enable=lambda **kwargs: calls.append(kwargs),
    )
    assert (
        controller.request(IntegrationAction("extensions", "demo", "enable", "project")).status
        == "deferred"
    )
    assert calls == []
    assert controller.apply_pending_at_idle(turn_running=False).status == "restart_required"
    assert calls == [{"plugin_id": "demo", "repo_root": tmp_path, "project": True}]


@pytest.mark.parametrize("action", ["authenticate", "configure", "install"])
def test_unsupported_actions_are_not_working_controls(action, tmp_path):
    controller = IntegrationController(repo_root=tmp_path, manager_provider=lambda: None)
    assert controller.request(IntegrationAction("mcp", "demo", action)).status == "unavailable"


def test_controller_config_only_fallback(tmp_path):
    controller = IntegrationController(
        repo_root=tmp_path,
        manager_provider=lambda: None,
        configured_servers_provider=lambda: [server()],
    )
    row = controller.refresh("mcp").rows[0]
    assert row.configured_enabled
    assert row.state == "unavailable"
    assert not row.actions


def test_duplicate_scopes_use_actual_record_and_override_state(tmp_path, monkeypatch):
    from alysis_code.extensions.models import ExtensionState, InstalledExtensionState
    from alysis_code.extensions.state import save_global_state, save_project_state

    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    save_global_state(ExtensionState(installed={"demo": InstalledExtensionState(enabled=True)}))
    save_project_state(
        tmp_path,
        ExtensionState(
            installed={"demo": InstalledExtensionState(enabled=False)}, enabled=["demo"]
        ),
    )
    snapshot = inspector.inspect_extensions(tmp_path)
    user, project = snapshot.rows
    assert user.configured_enabled is True
    assert project.configured_enabled is False
    assert "Project override: enabled." in " ".join(project.details)
    assert "ignored until workspace trust" in " ".join(project.details)
    assert "shared by plugin ID" in " ".join(user.details)
    assert user.actions == ("disable",)
    assert project.actions == ("enable", "disable")
    assert project.configuration_label == "Installed record"
    assert "Project override: enabled." in project.summary
    assert "Project overrides need workspace trust." in project.summary
    assert "Restart the session after changes." in project.summary
    assert "Installed record flag" not in " ".join(project.summary)


def test_live_manager_does_not_load_config_fallback(tmp_path):
    def unavailable():
        raise AssertionError("fallback must only be read without a live manager")

    controller = IntegrationController(
        repo_root=tmp_path, manager_provider=FakeManager, configured_servers_provider=unavailable
    )
    assert controller.refresh("mcp").rows[0].state == "connected"


def test_a_server_that_did_not_start_shows_as_failed_without_a_restart_action():
    manager = FakeManager()
    manager.server_lifecycle_status = lambda *, server_id: {
        "enabled": True,
        "connected": False,
        "connection_state": "failed",
        "startup_failed": True,
    }
    row = inspect_mcp(manager).rows[0]
    assert row.state == "failed"
    assert row.actions == ()
    assert "alysis mcp status" in row.error
    assert "start a new session" in row.error
