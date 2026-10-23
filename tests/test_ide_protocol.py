from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from alysis_code.approval_scope import (
    exact_command_scope,
    exact_file_set_scope,
)
from alysis_code.ide import management_protocol
from alysis_code.ide.approvals import ApprovalBroker
from alysis_code.ide.artifacts import ArtifactRoot, ArtifactStore
from alysis_code.ide.event_stream import (
    EventContext,
    EventSequencer,
    ProtocolEventSurface,
)
from alysis_code.ide.health import SUPPORTED_METHODS, capabilities_payload
from alysis_code.ide.protocol import (
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    parse_request_line,
    response_message,
)
from alysis_code.surface.events import MessageDelta, ToolCallStarted
from alysis_code.surface.types import (
    ApprovalDecision,
    ApprovalRequest,
    SubagentEndEvent,
    SubagentStartEvent,
    ToolEndEvent,
    ToolOutputEvent,
    ToolStartEvent,
)


def test_protocol_request_parses_jsonl() -> None:
    req = parse_request_line(
        '{"protocol_version":"1","id":"req-1","method":"health","params":{"x":1}}\n'
    )

    assert req.protocol_version == PROTOCOL_VERSION
    assert req.id == "req-1"
    assert req.method == "health"
    assert req.params == {"x": 1}


def test_protocol_rejects_unsupported_version() -> None:
    with pytest.raises(ProtocolError) as exc:
        parse_request_line('{"protocol_version":"2","id":"req-1","method":"health"}\n')

    assert exc.value.code == "unsupported_protocol_version"
    assert exc.value.request_id == "req-1"


def test_protocol_rejects_requests_over_size_limit() -> None:
    oversized = "x" * (MAX_REQUEST_BYTES + 1)

    with pytest.raises(ProtocolError) as exc:
        parse_request_line(oversized)

    assert exc.value.code == "request_too_large"


def test_protocol_version_remains_v1() -> None:
    assert PROTOCOL_VERSION == "1"
    assert capabilities_payload()["protocol_version"] == "1"


def test_capabilities_list_approval_respond_and_round_trip_shape() -> None:
    capabilities = capabilities_payload()

    assert "approval.respond" in capabilities["methods"]
    assert capabilities["features"]["approvals"] == {
        "round_trip": True,
        "default_deny": True,
        "session_scoped_allow": True,
        "supported_kinds_for_session_allow": [
            "shell_run",
            "shell_background",
            "custom_tool_run",
            "mcp_tool_run",
            "fs_write",
            "fs_edit",
            "fs_move",
            "fs_copy",
            "fs_delete",
            "fs_mkdir",
            "git_apply_patch",
            "verify_run",
        ],
        "timeout_seconds_default": 300,
    }


def test_capabilities_advertise_fenced_host_action_protocol() -> None:
    capabilities = capabilities_payload()
    host_actions = capabilities["features"]["host_actions"]

    assert "host.action.respond" in capabilities["methods"]
    assert host_actions == {
        "supported": True,
        "protocol_version": "1",
        "session_negotiated": True,
        "workspace_trust_required": True,
        "workspace_fenced": True,
        "capability_fenced": True,
        "approval_authority": "engine",
        "approval_protocol": 1,
        "request_event": "host_action_requested",
        "cancellation_event": "host_action_cancelled",
        "session_closed_event": "session_closed",
        "response_method": "host.action.respond",
        "actions": [
            "tasks.list",
            "tasks.run",
            "tasks.status",
            "tasks.terminate",
            "debug.list",
            "debug.start",
            "debug.stop",
            "debug.status",
            "github.repo.read",
            "github.pr.list",
            "github.pr.create",
            "github.git.fetch",
            "github.git.pull",
            "github.git.push",
            "mcp.github.call",
            "mcp.github.discover",
            "mcp.github.describe",
            "mcp.google.call",
            "terminal.list",
            "terminal.open",
            "terminal.read",
            "terminal.write",
            "terminal.close",
            "browser.list",
            "browser.open",
            "browser.read",
            "browser.navigate",
            "browser.back",
            "browser.forward",
            "browser.reload",
            "browser.close",
            "browser.snapshot",
            "browser.click",
            "browser.type",
            "browser.select",
            "browser.scroll",
            "browser.key",
            "browser.screenshot",
            "connectors.status",
            "connectors.github.repositories",
            "panels.list",
            "panels.read",
            "panels.screenshot",
        ],
        "max_argument_bytes": 8 * 1024,
        "max_result_bytes": 64 * 1024,
        "late_responses_rejected": True,
        "duplicate_responses_rejected": True,
    }


def test_capabilities_advertise_management_methods_and_trust_flags() -> None:
    capabilities = capabilities_payload()
    methods = set(capabilities["methods"])

    for method in (
        "config.get",
        "config.set",
        "profile.list",
        "profile.preset",
        "session.show",
        "session.score",
        "tools.catalog",
        "tool.trust",
        "skill.install",
        "doctor.bundle",
        "sandbox.pull",
        "update.check",
        "report.create",
        "mcp.status",
        "mcp.auth.login.start",
        "mcp.auth.login.status",
        "mcp.auth.login.cancel",
        "hooks.list",
        "hooks.disable",
        "conventions.list",
        "ext.search",
        "ext.install",
    ):
        assert method in methods
    assert "hooks.watch" not in methods

    management = capabilities["features"]["management"]
    assert management["methods"]["config.get"]["mutates"] is False
    assert management["methods"]["config.set"]["mutates"] is True
    assert management["methods"]["config.set"]["trust_required"] is True
    assert management["methods"]["tool.trust"]["secret_values_in_params"] is False
    assert management["methods"]["ext.install"]["workspace_required"] is True
    assert management["methods"]["ext.search"]["workspace_required"] is False
    assert management["methods"]["mcp.auth.login.start"]["supported"] is True
    assert management["methods"]["mcp.auth.login.start"]["callable"] is True
    assert management["methods"]["mcp.auth.login.start"]["passive_safe"] is False
    assert management["methods"]["mcp.auth.login.start"]["trust_required"] is True
    assert management["methods"]["mcp.auth.login.start"]["browser_opened_by_bridge"] is False
    assert management["methods"]["mcp.auth.login.start"]["tokens_in_protocol_params"] is False
    assert (
        "mcp.auth.login.status"
        in management["methods"]["mcp.auth.login.start"]["lifecycle_methods"]
    )
    assert management["methods"]["hooks.disable"]["mutates"] is True
    assert management["methods"]["ext.install"]["trust_required"] is True
    assert management["methods"]["ext.install"]["yes_alone_auto_trusts_package"] is False
    assert management["methods"]["skill.install"]["remote_source_policy"] == (
        "https_remote_requires_allow_remote_and_confirmation"
    )
    assert management["methods"]["session.show"]["bounded"] is True
    assert management["methods"]["conventions.render"]["redacted"] is True
    assert management["methods"]["update.check"]["network_default"] is False
    assert management["methods"]["update.check"]["passive_safe"] is True
    assert management["hooks"]["watch"]["supported"] is False
    assert management["hooks"]["watch"]["advertised_method"] is False
    assert management["hooks"]["watch"]["advertised_lifecycle_methods"] is False
    assert management["hooks"]["watch"]["workspace_trust_required"] is True
    assert management["hooks"]["watch"]["passive_safe"] is True
    assert "hooks.watch.start" in management["hooks"]["watch"]["proposed_methods"]
    assert "bounded_event_buffer" in management["hooks"]["watch"]["missing_lifecycle_primitives"]
    assert management["mcp"]["auth_login"]["supported"] is True
    assert management["mcp"]["auth_login"]["advertised_lifecycle_methods"] is True
    assert management["mcp"]["auth_login"]["tokens_in_protocol_params"] is False
    assert management["mcp"]["auth_login"]["cancel_cleanup"] is True
    assert management["mcp"]["auth_login"]["encrypted_token_store"] is True
    assert management["secret_policy"]["inline_secrets_rejected"] is True
    assert management["secret_policy"]["management_responses_redacted"] is True


def test_doctor_providers_live_is_advertised_as_explicit_intent_network_method() -> None:
    capabilities = capabilities_payload()

    assert "doctor.providers.live" in set(capabilities["methods"])
    method = capabilities["features"]["management"]["methods"]["doctor.providers.live"]
    assert method["mutates"] is False
    assert method["trust_required"] is False
    assert method["workspace_required"] is False
    assert method["network_default"] is False
    assert method["network_requires_explicit_user_intent"] is True
    assert method["network_params"] == ["allow_live"]
    assert method["live_provider_request"] is True
    assert method["passive_safe"] is False
    assert method["secret_values_in_params"] is False


def test_doctor_providers_live_fails_closed_without_allow_live() -> None:
    with pytest.raises(ProtocolError) as excinfo:
        management_protocol.handle_management_method(
            "doctor.providers.live",
            {},
            request_id="live-1",
        )

    assert excinfo.value.code == "missing_param"
    assert "allow_live" in excinfo.value.message


def test_doctor_providers_live_returns_redacted_validation(monkeypatch) -> None:
    from alysis_code.provider_diagnostics import ProviderLiveValidation

    captured: dict[str, object] = {}

    def fake_validate(cfg, *, timeout_s):
        captured["timeout_s"] = timeout_s
        return ProviderLiveValidation(
            profile_name="openai",
            provider_key="openai",
            protocol="openai_responses",
            model="gpt-test",
            status="failed",
            message="Provider rejected the API key (HTTP 401). api_key=sk-super-secret",
        )

    monkeypatch.setattr(management_protocol, "validate_active_provider_live", fake_validate)

    result = management_protocol.handle_management_method(
        "doctor.providers.live",
        {"allow_live": True, "timeout_s": 20},
        request_id="live-2",
    )

    assert captured["timeout_s"] == 20.0
    assert result["ok"] is False
    assert result["network_used"] is True
    assert result["secret_values_included"] is False
    validation = result["validation"]
    assert validation["profile"] == "openai"
    assert validation["status"] == "failed"
    assert "sk-super-secret" not in json.dumps(result)


def test_doctor_providers_live_reports_passed_validation(monkeypatch) -> None:
    from alysis_code.provider_diagnostics import ProviderLiveValidation

    monkeypatch.setattr(
        management_protocol,
        "validate_active_provider_live",
        lambda cfg, *, timeout_s: ProviderLiveValidation(
            profile_name="anthropic",
            provider_key="anthropic",
            protocol="anthropic_messages",
            model="claude-test",
            status="passed",
            message="Live provider check passed.",
        ),
    )

    result = management_protocol.handle_management_method(
        "doctor.providers.live",
        {"allow_live": True},
        request_id="live-3",
    )

    assert result["ok"] is True
    assert result["validation"]["status"] == "passed"
    assert result["validation"]["model"] == "claude-test"


def test_management_capabilities_match_mutating_method_trust_policy() -> None:
    management = capabilities_payload()["features"]["management"]
    methods = management["methods"]

    for method in management_protocol.MUTATING_MANAGEMENT_METHODS:
        assert methods[method]["mutates"] is True, method
        assert methods[method]["trust_required"] is True, method
    for method in set(management_protocol.MANAGEMENT_METHODS) - set(
        management_protocol.MUTATING_MANAGEMENT_METHODS
    ):
        assert methods[method]["mutates"] is False, method


def test_capabilities_advertise_safe_forge_assets_and_blocked_unsafe_modes() -> None:
    capabilities = capabilities_payload()
    methods = set(capabilities["methods"])

    for method in (
        "forge.attach",
        "forge.show",
        "forge.review",
        "forge.assets.list",
        "forge.assets.show",
        "forge.assets.add",
        "forge.assets.delete",
        "forge.assets.edit",
        "forge.assets.refresh",
        "forge.assets.cancelPending",
        "forge.assets.checkPlan",
        "forge.assets.pruneLegacy",
    ):
        assert method in methods
    for method in (
        "forge.swarm.start",
        "forge.swarm.resume",
        "forge.swarm.list",
        "forge.swarm.status",
        "forge.swarm.result",
        "forge.swarm.cancel",
        "forge.swarm.reconcile",
        "forge.review.start",
        "forge.review.result",
    ):
        assert method in methods

    forge = capabilities["features"]["forge"]
    assert forge["plan"]["durable_acceptance"] is True
    assert forge["plan"]["payload_conflict_detection"] is True
    assert forge["plan"]["worker_heartbeat"] is True
    assert forge["plan"]["expired_running_lease"] == ("indeterminate_no_automatic_reexecution")
    assert forge["review"]["mutates"] is True
    assert forge["review"]["trust_required"] is True
    assert forge["assets"]["rejects_symlinks"] is True
    assert forge["assets"]["methods"]["forge.assets.delete"]["confirmation_required"] is True
    assert forge["cancel"]["supported"] is True
    assert forge["cancel"]["callable"] is True
    assert forge["cancel"]["behavior"] == "cooperative_checkpoint_cancellation"
    assert forge["cancel"]["interrupt_kind"] == "cooperative"
    assert forge["cancel"]["hard_interrupt"] is False
    assert forge["cancel"]["must_not_mark_cancelled_without_interrupt"] is False
    assert forge["cancel"]["covered_job_kinds"] == [
        "forge_plan",
        "forge_plan_regenerate",
        "forge_review",
        "forge_execute",
        "forge_swarm",
    ]
    # The swarm gate lifted deliberately: jobs are trust-gated, cooperatively
    # cancellable, scope-guarded, and reconcilable; approval routing is still
    # a reserved placeholder.
    assert forge["swarm"]["supported"] is True
    assert forge["swarm"]["workspace_trust_required"] is True
    assert forge["swarm"]["cancellation"] == "cooperative_checkpoint_cancellation"
    assert forge["swarm"]["interrupted_task_status"] == "interrupted"
    assert forge["swarm"]["interrupted_worktrees_preserved"] is True
    assert forge["swarm"]["disjoint_write_scopes"] == "guaranteed_by_scheduler_invariant"
    assert forge["swarm"]["approvals"]["yes_auto_approval"] is False
    assert forge["swarm"]["approvals"]["timeout_behavior"] == "task_stays_paused_never_auto_denied"
    assert forge["swarm"]["approval_scope_grants"]["supported"] is True
    assert forge["swarm"]["merge_behavior"] == "review_only_per_task_apply_discard"
    assert forge["swarm"]["review"]["never_commits"] is True
    assert forge["swarm"]["review"]["working_tree_touched_only_on_apply"] is True
    assert forge["execute"]["job_status_model"] == "data_outcome_completed_with_result"
    assert forge["swarm"]["reconcile"]["read_only_report"] is True
    assert forge["swarm"]["reconcile"]["discard_confirmation_required"] is True
    assert "approval_pending" in forge["swarm"]["task_event_states"]
    resumable_swarm = capabilities["features"]["resumable_swarm"]
    assert resumable_swarm == {
        "supported": True,
        "durable": True,
        "external_storage": True,
        "workspace_and_session_scoped": True,
        "idempotent_start": True,
        "explicit_resume": True,
        "fresh_permission_fingerprint_required": True,
        "fenced_worker_leases": True,
        "restart_recovery": True,
        "atomic_cancellation": True,
        "exactly_once_usage_events": True,
        "methods": [
            "forge.swarm.start",
            "forge.swarm.resume",
            "forge.swarm.list",
            "forge.swarm.status",
            "forge.swarm.result",
            "forge.swarm.cancel",
        ],
    }
    assert forge["review"]["async"] is True
    assert forge["review"]["start_method"] == "forge.review.start"
    assert forge["execute"]["unsafe_modes"]["supported"] is False
    assert forge["execute"]["unsafe_modes"]["experimental_flags"] == {
        "auto": False,
        "fullaccess": False,
        "forge.swarm": False,
    }
    assert (
        "approval_ux_for_broad_mutation"
        in forge["execute"]["unsafe_modes"]["required_before_reassessment"]
    )
    assert forge["execute"]["max_steps_param"] is True
    assert forge["execute"]["no_log_param"] is True
    assert forge["execute"]["subagents_supported"] is False
    assert forge["execute"]["active_cancellation_supported"] is True
    assert forge["execute"]["cancellation"] == "cooperative_checkpoint_cancellation"


def test_capabilities_advertise_limited_session_method_semantics() -> None:
    capabilities = capabilities_payload()
    session_capabilities = capabilities["features"]["run_chat_options"][
        "session_method_capabilities"
    ]

    assert session_capabilities["session.history"]["supported"] is True
    assert session_capabilities["session.history"]["redacted"] is True
    assert session_capabilities["session.history"]["secret_values_included"] is False
    assert session_capabilities["session.history"]["bounded_results"] is True
    assert session_capabilities["session.context"]["supported"] is True
    assert session_capabilities["session.context"]["token_accounting"] == "tokenizer_estimate"
    assert session_capabilities["session.context"]["token_accounting_approximate"] is True
    assert session_capabilities["session.compact"]["supported"] is True
    assert session_capabilities["session.compact"]["real_compaction_supported"] is True
    assert session_capabilities["session.compact"]["mutates_context"] is True
    assert session_capabilities["session.resume"]["supported"] is True
    assert session_capabilities["session.resume"]["model_context_replay_supported"] is True
    assert session_capabilities["session.resume"]["bounded_replay"] is True
    assert session_capabilities["session.modelInfo"]["supported"] is True
    assert session_capabilities["session.modelInfo"]["secret_values_included"] is False
    assert session_capabilities["session.subagents"]["supported"] is True
    assert session_capabilities["session.subagents"]["status_toggle_only"] is True
    assert session_capabilities["session.subagents"]["explicit_execution_supported"] is False
    assert session_capabilities["session.subagents"]["lifecycle_event"] == "subagent_state_changed"
    assert (
        session_capabilities["session.subagents"]["execution_lifecycle"] == "in_turn_parent_owned"
    )
    assert session_capabilities["session.subagents"]["cancellation"] == "parent_job"
    assert session_capabilities["session.subagents"]["independently_resumable"] is False
    assert session_capabilities["session.subagents"]["background_worker_surface"] == "forge.swarm"
    assert session_capabilities["session.trace"]["supported"] is True
    assert session_capabilities["session.trace"]["full_trace_requires_confirmation"] is True
    assert session_capabilities["session.trace"]["redacted"] is True
    assert session_capabilities["session.trace"]["secret_values_included"] is False
    assert session_capabilities["session.terminals"]["supported"] is True
    assert session_capabilities["session.terminals"]["manager_required"] is True
    assert session_capabilities["session.terminals"]["arbitrary_shell_execution"] is False
    assert session_capabilities["session.terminals"]["interactive_pty_streaming"] is False
    assert session_capabilities["session.terminals"]["kill_trust_required"] is True
    assert session_capabilities["session.images"]["supported"] is True
    assert session_capabilities["session.images"]["binary_jsonl"] is False
    assert session_capabilities["session.images"]["paste_image"]["clipboard_binary_jsonl"] is False
    assert "session.modelInfo" in capabilities["methods"]
    assert "session.subagents.status" in capabilities["methods"]
    assert "session.subagents.setEnabled" in capabilities["methods"]
    assert "session.trace.status" in capabilities["methods"]
    assert "session.trace.setLevel" in capabilities["methods"]
    assert "session.trace.listEvents" in capabilities["methods"]
    assert "session.trace.readArtifact" in capabilities["methods"]
    assert "session.trace.clear" in capabilities["methods"]
    assert "session.terminals.list" in capabilities["methods"]
    assert "session.terminals.show" in capabilities["methods"]
    assert "session.terminals.kill" in capabilities["methods"]
    assert "session.terminals.clear" in capabilities["methods"]
    assert "session.images.add" in capabilities["methods"]

    forge = capabilities["features"]["forge"]
    for method in (
        "forge.plan.getState",
        "forge.plan.setAssistant",
        "forge.plan.setGoal",
        "forge.plan.updateTask",
        "forge.plan.validate",
        "forge.plan.regenerate",
    ):
        assert method in capabilities["methods"]
        assert forge["plan_editing"]["methods"][method]["supported"] is True
    assert forge["plan_editing"]["methods"]["forge.plan.regenerate"]["mutates"] is True
    assert forge["plan_editing"]["methods"]["forge.plan.regenerate"]["trust_required"] is True
    assert forge["plan_editing"]["methods"]["forge.plan.regenerate"]["optimistic_revision"] is True
    assert forge["plan_editing"]["terminal_output_scraping"] is False
    assert forge["plan_editing"]["unsafe_execution_exposed"] is False


def test_forge_execute_contract_matches_subagent_capability() -> None:
    contract_path = (
        Path(__file__).resolve().parents[1] / "docs" / "generated" / "ide_protocol_methods.json"
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    methods = contract["methods"]
    forge_execute = capabilities_payload()["features"]["forge"]["execute"]

    assert forge_execute["subagents_supported"] is False
    assert "subagents_enabled" not in methods["forge.executePreview"]["optional_params"]
    assert "subagents_enabled" not in methods["forge.execute"]["optional_params"]


def test_all_current_ide_bridge_methods_are_documented() -> None:
    docs = (Path(__file__).resolve().parents[1] / "docs" / "ide_protocol.md").read_text(
        encoding="utf-8"
    )

    missing = [method for method in SUPPORTED_METHODS if method not in docs]
    assert missing == []


def test_protocol_redacts_secret_strings_without_redacting_capability_booleans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "test-secret-value")

    payload = response_message(
        "req-1",
        {
            "message": "value=test-secret-value",
            "api_key": "test-secret-value",
            "secret_redaction": True,
        },
    )

    assert payload["result"]["message"] == "value=<redacted>"
    assert payload["result"]["api_key"] == "<redacted>"
    assert payload["result"]["secret_redaction"] is True


def test_protocol_redacts_bearer_and_authorization_strings() -> None:
    payload = response_message(
        "req-1",
        {
            "bearer": "Bearer abcdefghijklmnop",
            "header": "Authorization: Bearer abcdefghijklmnop",
            "assignment": "authorization=abcdefghijklmnop",
        },
    )

    assert payload["result"]["bearer"] == "Bearer <redacted>"
    assert payload["result"]["header"] == "Authorization: <redacted>"
    assert payload["result"]["assignment"] == "authorization=<redacted>"


def test_protocol_redacts_secret_assignments_and_url_userinfo() -> None:
    payload = response_message(
        "req-1",
        {
            "nested": [
                {"content": "secret_token=must-not-leak"},
                {"content": "password: must-not-leak-too"},
                {"content": "DEMO_API_KEY=abc123456789"},
                {"content": "https://user:pass@example.test/path"},
            ],
        },
    )
    rendered = json.dumps(payload, sort_keys=True)

    assert "must-not-leak" not in rendered
    assert "must-not-leak-too" not in rendered
    assert "abc123456789" not in rendered
    assert "user:pass" not in rendered
    assert "secret_token=<redacted>" in rendered
    assert "password: <redacted>" in rendered
    assert "DEMO_API_KEY=<redacted>" in rendered
    assert "https://<redacted>@example.test/path" in rendered


def test_protocol_redacts_pem_and_putty_private_key_blocks() -> None:
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\npem-canary\n-----END OPENSSH PRIVATE KEY-----"
    putty = (
        "PuTTY-User-Key-File-3: ssh-ed25519\n"
        "Encryption: none\n"
        "Comment: test\n"
        "Public-Lines: 1\npublic-canary\n"
        "Private-Lines: 1\nputty-private-canary\n"
        "Private-MAC: mac-canary"
    )

    rendered = json.dumps(response_message("req-1", {"pem": pem, "putty": putty}))

    assert "pem-canary" not in rendered
    assert "putty-private-canary" not in rendered
    assert rendered.count("<redacted-private-key>") == 2


def test_event_envelope_sequence_and_payload_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "event-secret-value")
    sequencer = EventSequencer()
    context = EventContext(session_id="session-1", job_id="job-1")

    first = sequencer.envelope(
        MessageDelta(text="hello event-secret-value"),
        context=context,
    )
    second = sequencer.envelope(
        ToolCallStarted(call_id="call-1", name="fs_read", arguments_preview='{"path":"README.md"}'),
        context=context,
    )

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert first["protocol_version"] == "1"
    assert first["type"] == "message_delta"
    assert first["payload"]["text"] == "hello <redacted>"
    assert second["type"] == "tool_call_started"
    assert second["session_id"] == "session-1"
    assert second["job_id"] == "job-1"


def test_protocol_surface_wraps_existing_surface_events() -> None:
    emitted: list[dict[str, object]] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
    )

    surface.emit_message_delta("hello")
    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call-1",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
        )
    )

    assert [event["type"] for event in emitted] == ["message_delta", "tool_call_started"]
    assert emitted[0]["payload"] == {"text": "hello", "worker_id": None, "role": None}
    assert emitted[1]["payload"]["name"] == "fs_read"


def test_protocol_surface_emits_correlated_subagent_lifecycle_without_authority() -> None:
    emitted: list[dict[str, object]] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1", job_id="job-parent"),
        emit=emitted.append,
    )

    surface.on_subagent_start(
        SubagentStartEvent(
            name="explorer",
            mode="readonly",
            subagent_run_id="child-run-1",
            description="Inspect the retry boundary.",
            label="retry-boundary",
        )
    )
    surface.on_subagent_end(
        SubagentEndEvent(
            name="explorer",
            mode="readonly",
            status="success",
            elapsed_ms=1250,
            steps_completed=3,
            subagent_run_id="child-run-1",
            subagent_session_id="child-session-1",
            label="retry-boundary",
        )
    )

    assert [event["type"] for event in emitted] == [
        "subagent_state_changed",
        "subagent_state_changed",
    ]
    assert all(event["job_id"] == "job-parent" for event in emitted)
    started = emitted[0]["payload"]
    completed = emitted[1]["payload"]
    assert isinstance(started, dict)
    assert isinstance(completed, dict)
    assert started == {
        "subagent_run_id": "child-run-1",
        "name": "explorer",
        "mode": "readonly",
        "state": "running",
        "label": "retry-boundary",
        "subagent_session_id": None,
        "description": "Inspect the retry boundary.",
        "elapsed_ms": None,
        "steps_completed": None,
        "error": None,
    }
    assert completed["state"] == "success"
    assert completed["label"] == "retry-boundary"
    assert completed["subagent_session_id"] == "child-session-1"
    assert completed["elapsed_ms"] == 1250
    assert completed["steps_completed"] == 3
    assert "lease_token" not in completed
    assert "permission" not in completed


def test_protocol_surface_standalone_emit_and_legacy_callbacks_both_deliver() -> None:
    emitted: list[dict[str, object]] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
    )

    surface.emit_message_delta("hello")
    surface.on_assistant_token("hello")
    surface.emit_tool_call_started("call-1", "fs_read", '{"path":"README.md"}')
    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call-1",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
        )
    )
    surface.emit_tool_call_progress("call-1", '{"content":"ok"}')
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="call-1",
            name="fs_read",
            chunk='{"content":"ok"}',
        )
    )
    surface.emit_tool_call_completed("call-1", True, '{"content":"ok"}')
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call-1",
            name="fs_read",
            status="done",
            elapsed_ms=5,
        )
    )
    surface.emit_message_end("finished")
    surface.on_assistant_message_done("finished")

    assert [event["type"] for event in emitted] == [
        "message_delta",
        "message_delta",
        "tool_call_started",
        "tool_call_started",
        "tool_call_progress",
        "tool_call_progress",
        "tool_call_completed",
        "tool_call_completed",
        "message_end",
        "message_end",
    ]
    canonical_completed = emitted[6]["payload"]
    legacy_completed = emitted[7]["payload"]
    assert isinstance(canonical_completed, dict)
    assert isinstance(legacy_completed, dict)
    assert canonical_completed["success"] is True
    assert canonical_completed["result_preview"] == '{"content":"ok"}'
    assert legacy_completed["success"] is True


def test_protocol_surface_legacy_done_event_is_successful() -> None:
    emitted: list[dict[str, object]] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
    )

    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call-1",
            name="fs_read",
            status="done",
            elapsed_ms=5,
        )
    )

    assert len(emitted) == 1
    assert emitted[0]["type"] == "tool_call_completed"
    assert emitted[0]["payload"]["success"] is True


def test_protocol_surface_job_context_is_thread_local() -> None:
    emitted: list[dict[str, object]] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
    )
    job_b_started = threading.Event()
    job_a_cleared = threading.Event()

    def job_a() -> None:
        surface.with_job("job-a")
        surface.emit_message_delta("a1")
        assert job_b_started.wait(timeout=1.0)
        surface.with_job(None)
        job_a_cleared.set()

    def job_b() -> None:
        surface.with_job("job-b")
        surface.emit_message_delta("b1")
        job_b_started.set()
        assert job_a_cleared.wait(timeout=1.0)
        surface.emit_message_delta("b2")
        surface.with_job(None)

    first = threading.Thread(target=job_a)
    second = threading.Thread(target=job_b)
    first.start()
    second.start()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    by_text = {event["payload"]["text"]: event["job_id"] for event in emitted}
    assert by_text["a1"] == "job-a"
    assert by_text["b1"] == "job-b"
    assert by_text["b2"] == "job-b"


def test_artifact_store_lists_reads_and_blocks_escapes(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "note.txt").write_bytes(b"hello\n")
    (root / "large.txt").write_bytes(b"abcdef")
    store = ArtifactStore([ArtifactRoot("session", root)])

    listed = store.list()
    assert listed["truncated"] is False
    assert listed["artifacts"] == [
        {
            "artifact_id": "session:large.txt",
            "root": "session",
            "path": "large.txt",
            "size_bytes": 6,
        },
        {
            "artifact_id": "session:note.txt",
            "root": "session",
            "path": "note.txt",
            "size_bytes": 6,
        },
    ]
    assert store.read("session:note.txt")["content"] == "hello\n"
    limited = store.read("session:large.txt", max_bytes=3)
    assert limited["content"] == "abc"
    assert limited["size_bytes"] == 6
    assert limited["truncated"] is True
    with pytest.raises(ProtocolError):
        store.read("session:../note.txt")


def test_artifact_store_list_is_bounded(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    for idx in range(3):
        (root / f"{idx}.txt").write_text(str(idx), encoding="utf-8")
    store = ArtifactStore([ArtifactRoot("session", root)])

    listed = store.list(max_items=2)

    assert listed["truncated"] is True
    assert len(listed["artifacts"]) == 2


def test_approval_broker_downgrades_unscoped_shell_session_allowance() -> None:
    broker = ApprovalBroker(timeout_seconds=1)
    emitted: list[object] = []

    thread, decisions, first_event = _start_approval_request(
        broker,
        ApprovalRequest(kind="shell_run", reason="run", preview="echo hi", command="echo hi"),
        emitted,
    )
    response = broker.resolve(
        session_id="session-1",
        approval_id=first_event.approval_id or first_event.prompt_id,
        allow=True,
        allow_for_session=True,
    )
    thread.join(timeout=1.0)

    assert decisions == [ApprovalDecision(allow=True, allow_for_session=False)]
    assert response["allow_for_session"] is False
    assert response["allow_for_session_supported"] is False
    assert "exact safe scope" in str(response["allow_for_session_warning"])

    thread, decisions, second_event = _start_approval_request(
        broker,
        ApprovalRequest(kind="shell_run", reason="run again", preview="echo hi", command="echo hi"),
        emitted,
        start_index=len(emitted),
    )
    assert second_event.approval_id != first_event.approval_id
    broker.resolve(
        session_id="session-1",
        approval_id=second_event.approval_id or second_event.prompt_id,
        allow=False,
        allow_for_session=False,
    )
    thread.join(timeout=1.0)
    assert decisions == [ApprovalDecision(allow=False, allow_for_session=False)]


def test_approval_broker_fs_write_session_allowance_requires_exact_file_scope() -> None:
    broker = ApprovalBroker(timeout_seconds=1)
    emitted: list[object] = []
    request = ApprovalRequest(
        kind="fs_write",
        reason="write",
        preview="write a.txt",
        files=["a.txt"],
    )
    thread, decisions, event = _start_approval_request(broker, request, emitted)

    response = broker.resolve(
        session_id="session-1",
        approval_id=event.approval_id or event.prompt_id,
        allow=True,
        allow_for_session=True,
    )
    thread.join(timeout=1.0)

    assert decisions == [ApprovalDecision(allow=True, allow_for_session=False)]
    assert response["allow_for_session"] is False
    assert event.allow_for_session_supported is False


def test_approval_broker_exact_command_hash_allows_only_same_command() -> None:
    broker = ApprovalBroker(timeout_seconds=1)
    emitted: list[object] = []
    request = ApprovalRequest(
        kind="shell_run",
        reason="run",
        preview="echo hi",
        command="echo hi",
        allow_for_session_scope=exact_command_scope("echo hi", kind="shell_run"),
    )
    thread, decisions, event = _start_approval_request(broker, request, emitted)
    assert event.allow_for_session_supported is True
    broker.resolve(
        session_id="session-1",
        approval_id=event.approval_id or event.prompt_id,
        allow=True,
        allow_for_session=True,
    )
    thread.join(timeout=1.0)
    assert decisions == [ApprovalDecision(allow=True, allow_for_session=True)]

    emitted_before = len(emitted)
    auto_decision = broker.request(
        session_id="session-1",
        request=request,
        emit_event=emitted.append,
    )
    assert auto_decision == ApprovalDecision(allow=True, allow_for_session=True)
    assert len(emitted) == emitted_before

    different = ApprovalRequest(
        kind="shell_run",
        reason="run",
        preview="echo bye",
        command="echo bye",
        allow_for_session_scope=exact_command_scope("echo bye", kind="shell_run"),
    )
    thread, decisions, different_event = _start_approval_request(
        broker,
        different,
        emitted,
        start_index=len(emitted),
    )
    assert different_event.approval_id != event.approval_id
    broker.resolve(
        session_id="session-1",
        approval_id=different_event.approval_id or different_event.prompt_id,
        allow=False,
        allow_for_session=False,
    )
    thread.join(timeout=1.0)
    assert decisions == [ApprovalDecision(allow=False, allow_for_session=False)]


def test_approval_broker_exact_file_set_allows_only_same_file_set() -> None:
    broker = ApprovalBroker(timeout_seconds=1)
    emitted: list[object] = []
    request = ApprovalRequest(
        kind="fs_write",
        reason="write",
        preview="write a",
        files=["a.txt"],
        allow_for_session_scope=exact_file_set_scope(["a.txt"], operation="fs_write"),
    )
    thread, decisions, event = _start_approval_request(broker, request, emitted)
    broker.resolve(
        session_id="session-1",
        approval_id=event.approval_id or event.prompt_id,
        allow=True,
        allow_for_session=True,
    )
    thread.join(timeout=1.0)
    assert decisions == [ApprovalDecision(allow=True, allow_for_session=True)]

    emitted_before = len(emitted)
    auto_decision = broker.request(
        session_id="session-1",
        request=request,
        emit_event=emitted.append,
    )
    assert auto_decision == ApprovalDecision(allow=True, allow_for_session=True)
    assert len(emitted) == emitted_before

    different = ApprovalRequest(
        kind="fs_write",
        reason="write",
        preview="write b",
        files=["b.txt"],
        allow_for_session_scope=exact_file_set_scope(["b.txt"], operation="fs_write"),
    )
    thread, decisions, different_event = _start_approval_request(
        broker,
        different,
        emitted,
        start_index=len(emitted),
    )
    broker.resolve(
        session_id="session-1",
        approval_id=different_event.approval_id or different_event.prompt_id,
        allow=False,
        allow_for_session=False,
    )
    thread.join(timeout=1.0)
    assert decisions == [ApprovalDecision(allow=False, allow_for_session=False)]


def test_approval_broker_clear_session_removes_all_retained_approvals() -> None:
    broker = ApprovalBroker(timeout_seconds=1)
    emitted: list[object] = []
    request = ApprovalRequest(
        kind="fs_write",
        reason="write",
        preview="write a",
        files=["a.txt"],
        allow_for_session_scope=exact_file_set_scope(["a.txt"], operation="fs_write"),
    )
    thread, _, event = _start_approval_request(broker, request, emitted)
    broker.resolve(
        session_id="session-1",
        approval_id=event.approval_id or event.prompt_id,
        allow=True,
        allow_for_session=True,
    )
    thread.join(timeout=1.0)
    assert broker._approvals
    assert broker._session_allowances

    broker.clear_session("session-1")

    assert broker._approvals == {}
    assert broker._session_allowances == set()


def test_approval_scope_events_redact_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "scope-secret-value")
    broker = ApprovalBroker(timeout_seconds=1)
    emitted: list[object] = []
    request = ApprovalRequest(
        kind="fs_write",
        reason="write scope-secret-value",
        preview="write Bearer abcdefghijklmnop",
        files=["scope-secret-value.txt"],
        command="echo Authorization: Bearer abcdefghijklmnop",
        metadata={"api_key": "scope-secret-value"},
        allow_for_session_scope=exact_file_set_scope(
            ["scope-secret-value.txt"],
            operation="fs_write",
        ),
    )
    thread, _, event = _start_approval_request(broker, request, emitted)
    broker.resolve(
        session_id="session-1",
        approval_id=event.approval_id or event.prompt_id,
        allow=True,
        allow_for_session=True,
    )
    thread.join(timeout=1.0)

    rendered = repr([item.to_dict() for item in emitted])
    assert "scope-secret-value" not in rendered
    assert "abcdefghijklmnop" not in rendered
    assert "<redacted>" in rendered


def _start_approval_request(
    broker: ApprovalBroker,
    request: ApprovalRequest,
    emitted: list[object],
    *,
    start_index: int = 0,
) -> tuple[threading.Thread, list[ApprovalDecision], object]:
    decisions: list[ApprovalDecision] = []

    def run() -> None:
        decisions.append(
            broker.request(
                session_id="session-1",
                request=request,
                emit_event=emitted.append,
            )
        )

    thread = threading.Thread(target=run)
    thread.start()
    event = _wait_for_approval_event(emitted, start_index=start_index)
    return thread, decisions, event


def _wait_for_approval_event(emitted: list[object], *, start_index: int = 0) -> object:
    for _ in range(100):
        for event in emitted[start_index:]:
            if getattr(event, "kind", None) == "approval":
                return event
        threading.Event().wait(0.01)
    raise AssertionError("timed out waiting for approval event")
