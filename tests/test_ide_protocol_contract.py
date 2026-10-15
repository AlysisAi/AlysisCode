from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from alysis_code.ide import management_protocol
from alysis_code.ide.health import SUPPORTED_METHODS, capabilities_payload
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.stdio_bridge import StdioBridge
from scripts.qa import check_ide_cli_parity as parity

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "docs" / "generated" / "ide_protocol_methods.json"


def _request(method: str, params: dict[str, Any] | None = None, request_id: str = "req") -> str:
    return json.dumps(
        {
            "protocol_version": "1",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
        sort_keys=True,
    )


def _json_lines(buffer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def _contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _expanded_contract_methods() -> dict[str, dict[str, Any]]:
    contract = _contract()
    defaults = dict(contract["method_defaults"])
    methods: dict[str, dict[str, Any]] = {}
    for group in contract.get("method_groups", []):
        group_values = {
            **defaults,
            **{key: value for key, value in group.items() if key != "methods"},
        }
        for method in group["methods"]:
            methods[method] = {**group_values, "method": method}
    for method, values in contract.get("methods", {}).items():
        methods[method] = {**methods.get(method, defaults), **values, "method": method}
    return methods


def test_protocol_method_contract_covers_every_advertised_method() -> None:
    contract_methods = _expanded_contract_methods()

    assert set(contract_methods) == set(SUPPORTED_METHODS)
    for method, entry in contract_methods.items():
        assert entry["method"] == method
        assert isinstance(entry.get("required_params", []), list), method
        assert isinstance(entry.get("optional_params", []), list), method
        assert isinstance(entry.get("secret_forbidden_fields", []), list), method
        assert isinstance(entry.get("mutates"), bool), method
        assert isinstance(entry.get("workspace_trust_required"), bool), method
        assert isinstance(entry.get("workspace_required"), bool), method


def test_protocol_method_contract_matches_management_capabilities() -> None:
    contract_methods = _expanded_contract_methods()
    management_methods = capabilities_payload()["features"]["management"]["methods"]

    for method in management_protocol.MANAGEMENT_METHODS:
        contract = contract_methods[method]
        capability = management_methods[method]
        assert contract["mutates"] is capability["mutates"], method
        assert contract["workspace_trust_required"] is capability["trust_required"], method
        assert contract["workspace_required"] is capability["workspace_required"], method
        assert contract["secret_forbidden_fields"], method
        assert capability["secret_values_in_params"] is False, method


def test_protocol_method_contract_captures_durable_swarm_recovery() -> None:
    methods = _expanded_contract_methods()

    assert "idempotency_key" in methods["forge.swarm.start"]["optional_params"]
    resume = methods["forge.swarm.resume"]
    assert resume["required_params"] == [
        "session_id",
        "plan_id",
        "job_id",
        "workspace_trusted",
    ]
    assert "expected_revision" in resume["optional_params"]
    assert resume["mutates"] is True
    assert resume["workspace_trust_required"] is True
    assert methods["forge.swarm.list"]["required_params"] == ["session_id"]
    assert "session_id" in methods["forge.swarm.result"]["optional_params"]
    cancel = methods["forge.swarm.cancel"]
    assert "expected_revision" in cancel["optional_params"]
    assert cancel["mutates"] is True


def test_protocol_method_contract_captures_durable_forge_plan_acceptance() -> None:
    methods = _expanded_contract_methods()

    assert "idempotency_key" in methods["forge.plan.start"]["optional_params"]
    assert "idempotency_key" not in methods["forge.plan"]["optional_params"]
    plan = capabilities_payload()["features"]["forge"]["plan"]
    assert plan["durable_acceptance"] is True
    assert plan["idempotency_key_and_payload_hashed"] is True
    assert plan["acknowledgement_after_durable_acceptance"] is True
    assert plan["stable_job_id_across_bridge_restart"] is True
    assert plan["job_status_restart_recovery"] is True
    assert plan["result_restart_recovery"] is True
    assert plan["fenced_worker_leases"] is True
    assert plan["expired_running_lease"] == "indeterminate_no_automatic_reexecution"
    assert plan["legacy_sync_exactly_once"] is False


def test_protocol_method_contract_captures_managed_browser_security_boundary() -> None:
    methods = _expanded_contract_methods()

    expected = {
        "browser.start",
        "browser.navigate",
        "browser.snapshot",
        "browser.screenshot",
        "browser.artifact.read",
        "browser.diagnostics",
        "browser.click",
        "browser.type",
        "browser.status",
        "browser.list",
        "browser.close",
    }
    assert expected <= methods.keys()

    start = methods["browser.start"]
    assert start["required_params"] == ["session_id", "workspace_trusted"]
    assert start["workspace_trust_required"] is True
    assert start["public_destinations_by_default"] is True
    assert "network_scope" in start["optional_params"]
    assert start["network_scopes"] == ["public", "public_loopback"]
    assert start["confirmation_required_when"].startswith("network_scope=public_loopback")
    assert "direct IDE only" in start["public_loopback_actor_scope"]
    assert "LAN" in start["public_loopback_policy"]
    for method in ("browser.start", "browser.status", "browser.list"):
        assert methods[method]["result_redaction"] == {
            "redacted": True,
            "bounded": True,
            "secret_values_included": False,
        }

    navigate = methods["browser.navigate"]
    assert navigate["workspace_trust_required"] is True
    assert "DNS-aware public-only" in navigate["url_policy"]
    assert "CDP Fetch" in navigate["redirect_and_subresource_policy"]

    artifact = methods["browser.artifact.read"]
    assert artifact["encoding"] == "base64"
    assert artifact["maximum_chunk_bytes"] == 1024 * 1024
    assert artifact["result_redaction"]["bounded"] is True

    assert methods["browser.type"]["result_echoes_input_text"] is False
    close = methods["browser.close"]
    assert close["workspace_trust_required"] is False
    assert close["confirmation_required"] is True
    assert "exact owned process tree" in close["cleanup"]
    assert "ephemeral screenshots" in close["cleanup"]
    assert "delete_artifacts=false is rejected" in close["cleanup"]


def test_protocol_method_contract_captures_live_mcp_server_lifecycle() -> None:
    methods = _expanded_contract_methods()
    capabilities = capabilities_payload()["features"]["live_mcp_lifecycle"]

    expected = {
        "mcp.server.status",
        "mcp.server.enable",
        "mcp.server.disable",
        "mcp.server.restart",
    }
    assert expected <= methods.keys()
    assert capabilities["methods"] == [
        "mcp.server.status",
        "mcp.server.enable",
        "mcp.server.disable",
        "mcp.server.restart",
    ]
    assert capabilities["session_owned_manager"] is True
    assert capabilities["single_client_lease_per_server"] is True
    assert capabilities["status_materializes_connection"] is False
    assert capabilities["status_checks_stdio_process_liveness"] is True
    assert capabilities["reconnect_catalog_fenced"] is True
    assert capabilities["reconnect_atomic"] is True
    assert capabilities["server_diagnostics_in_protocol"] is False

    status = methods["mcp.server.status"]
    assert status["required_params"] == ["session_id", "server_id"]
    assert status["temporary_manager"] is False
    assert status["materializes_connection"] is False
    assert status["stdio_process_liveness"] is True
    assert status["mutates"] is False

    for method in ("mcp.server.enable", "mcp.server.disable", "mcp.server.restart"):
        entry = methods[method]
        assert entry["required_params"] == [
            "session_id",
            "server_id",
            "workspace_trusted",
        ]
        assert entry["mutates"] is True
        assert entry["workspace_trust_required"] is True
        assert entry["idle_session_required"] is True
        assert entry["temporary_manager"] is False
        assert entry["server_diagnostics_in_error"] is False
    assert methods["mcp.server.enable"]["atomic_replacement"] is True
    assert methods["mcp.server.restart"]["atomic_replacement"] is True


def test_advertised_methods_have_dispatch_or_documented_fail_closed_behavior() -> None:
    health_methods = parity.extract_ide_method_features()
    dispatch_methods = parity.extract_stdio_dispatch_method_features()
    health_method_names = {method.name for method in health_methods}

    assert health_methods <= dispatch_methods

    management_handlers = parity.extract_management_handler_features()
    assert parity.extract_management_method_features() == management_handlers

    oauth_login = capabilities_payload()["features"]["management"]["methods"][
        "mcp.auth.login.start"
    ]
    assert oauth_login["supported"] is True
    assert oauth_login["callable"] is True
    assert oauth_login["behavior"] == (
        "returns_authorization_url_and_completes_via_loopback_callback"
    )
    assert oauth_login["browser_opened_by_bridge"] is False
    assert oauth_login["tokens_in_protocol_params"] is False
    assert oauth_login["authorization_code_in_protocol"] is False
    assert "mcp.auth.login.status" in health_method_names
    assert "mcp.auth.login.complete" not in health_method_names
    assert "mcp.auth.login.cancel" in health_method_names

    management = capabilities_payload()["features"]["management"]
    assert management["mcp"]["auth_login"]["advertised_lifecycle_methods"] is True
    assert management["hooks"]["watch"]["advertised_lifecycle_methods"] is False
    assert "hooks.watch" not in health_method_names
    assert not any(name.startswith("hooks.watch.") for name in health_method_names)


def test_workspace_required_management_methods_fail_closed_before_handler_params() -> None:
    for method in sorted(management_protocol.WORKSPACE_REQUIRED_MANAGEMENT_METHODS):
        try:
            management_protocol.handle_management_method(
                method,
                {"workspace_trusted": True},
                request_id=method,
            )
        except ProtocolError as exc:
            assert exc.code == "missing_param", method
            assert "workspace or path" in exc.message, method
        else:
            raise AssertionError(f"{method} did not require explicit workspace binding")


# Host responses match their pending request binding before checking credential fields,
# so an unbound request is refused with a binding error instead.
_BINDING_FIRST_RESPONSE_METHODS = frozenset({"computer.respond", "task_budget.respond"})


def test_stdio_bridge_rejects_inline_secret_params_recursively_for_every_method() -> None:
    for method in SUPPORTED_METHODS:
        out = io.StringIO()
        bridge = StdioBridge(stdout=out)

        bridge.process_line(
            _request(
                method,
                {"nested": [{"credential": "must-not-leak"}]},
                request_id=method,
            )
            + "\n"
        )

        payload = _json_lines(out)[0]
        assert payload["ok"] is False, method
        if method not in _BINDING_FIRST_RESPONSE_METHODS:
            assert payload["error"]["code"] == "inline_secret_rejected", method
        assert "must-not-leak" not in out.getvalue(), method
