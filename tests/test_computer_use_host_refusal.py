"""Closed host refusals through the real protected turn and protocol surface."""

from __future__ import annotations

import json

import pytest
from test_computer_interaction_v2 import action_result, call, done, state
from test_computer_use_recovery import _ScriptedComputerUseClient
from test_ide_computer_use_host import _bridge_session, _lines

from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent_loop import create_session
from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.computer_use_call_diagnostics import RESULT_CODES
from alysis_code.config import AppConfig
from alysis_code.ide.event_stream import EventContext, ProtocolEventSurface
from alysis_code.ide.stdio_bridge import _actual_computer_use_destination

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def refused(
    *, dispatch="not_dispatched", authority="invalid", cause="target_changed", phase="validation"
):
    return {
        "error": cause,
        "message": "PRIVATE /Users/private/window [computer_use_diagnostic secret]",
        "interaction_feedback": {
            "dispatch": dispatch,
            "authority": authority,
            "effect": "unknown",
            "cause": cause,
            "phase": phase,
        },
    }


def make_turn(root, host, responses, *, response_mode="status_only"):
    events, transient = [], []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="refusal", run_id="run", job_id="job"),
        emit=events.append,
        emit_transient=transient.append,
        computer_use_evidence_version=1,
    )
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=root,
        mode="auto",
        yes=True,
        max_steps=15,
        no_log=True,
        api_key_override="fixture-key",
        surface=surface,
        sensitive_media=True,
        computer_use_response_mode=response_mode,
        computer_use_host_request=host,
        computer_interaction_v2=True,
        computer_application_lifecycle_v1=True,
        computer_observation_available=True,
    )
    client = _ScriptedComputerUseClient(responses)
    session.client = client
    return session, client, events, transient


def terminal_evidence(transient):
    return [
        e["payload"]
        for e in transient
        if e["type"] == "computer_use_evidence" and e["payload"].get("kind") == "terminal"
    ]


@pytest.mark.parametrize("response_mode", ["status_only", "host_sanitized_text"])
def test_first_state_target_changed_is_blocked_without_runtime_failure(tmp_path, response_mode):
    requests = []

    def host(name, args):
        requests.append((name, args))
        return refused()

    session, client, events, transient = make_turn(
        tmp_path,
        host,
        [
            call("computer_state", {}, 1),
            call("computer_action", {"action": "activate_application"}, 2),
            done(),
        ],
        response_mode=response_mode,
    )
    try:
        code = session.run_turn("Open the permitted app for me please.")
        outcome = session.last_turn_outcome
    finally:
        session.close()
    # A launch may already have completed during host admission, before this
    # protected turn. Never replay it or infer from the refused read that it failed.
    assert len(requests) == client.calls == 1
    assert code == 0
    assert outcome["outcome"] == "blocked"
    assert not [e for e in events if e["type"] == "error_raised"]
    text = " ".join(e["payload"].get("text", "") for e in events if e["type"] == "message_end")
    assert "couldn't verify" in text and "already opened" in text
    assert "nothing was" not in text
    assert "PRIVATE" not in json.dumps(events + transient)
    assert "[computer_use_diagnostic" not in text
    assert terminal_evidence(transient) == [
        {
            "version": 1,
            "kind": "terminal",
            "terminalOrigin": "host_error",
            "reportedOutcome": "blocked",
            "stateCalls": 1,
            "actionCalls": 0,
            "knownHostFailure": True,
            "hostFailureCode": "target_changed",
        }
    ]
    results = [
        e["payload"]
        for e in events
        if e["type"] == "computer_use_call_diagnostic" and e["payload"].get("callPhase") == "result"
    ]
    assert len(results) == 1 and results[0]["resultCode"] == "target_changed"
    assert not any(
        key.startswith("interaction") for e in events + transient for key in e["payload"]
    )
    # Synthetic wire fixture for the paired app's actual-event replay.
    (tmp_path / "closed-refusal-wire.json").write_text(
        json.dumps(
            {
                "events": events,
                "transient": transient,
                "task_outcome": outcome,
                "exit_code": code,
            },
            indent=2,
        )
        + "\n"
    )


def test_blocked_refusal_survives_real_job_and_task_projection(tmp_path):
    session, client, _, _ = make_turn(
        tmp_path, lambda *_: refused(), [call("computer_state", {}, 1)]
    )
    bridge, out, bridge_session, job = _bridge_session(tmp_path)
    bridge_session.agent_session = session
    session.surface = bridge_session.surface
    client.base_url = "https://offline.invalid/v1"
    bridge_session.computer_use_destination = _actual_computer_use_destination(
        session, request_id=None
    )
    try:
        bridge._run_session_turn(bridge_session, job, "Open the permitted app.", [], [])
    finally:
        session.close()
    assert job.status == "completed" and job.exit_code == 0 and job.error is None
    assert job.result["task_outcome"]["outcome"] == "blocked"
    assert job.result["verified_success"] is False
    events = _lines(out)
    assert not [e for e in events if e["type"] == "error_raised"]
    assert any(
        e["type"] == "activity_update"
        and e["payload"].get("metadata", {}).get("task_outcome") == "blocked"
        for e in events
    )
    assert events[-1]["type"] == "info_emitted"
    assert events[-1]["payload"]["message"] == f"job_completed {job.job_id} exit_code=0"
    assert "[computer_use_diagnostic" not in json.dumps(events)
    (tmp_path / "closed-refusal-job.json").write_text(
        json.dumps(
            {
                "events": events,
                "result": job.result,
                "exit_code": job.exit_code,
                "status": job.status,
            },
            indent=2,
        )
        + "\n"
    )


def test_unexpected_provider_exception_still_raises_protected_runtime_error(tmp_path):
    session, client, events, transient = make_turn(tmp_path, lambda *_: state(), [])

    def broken_provider(**kwargs):
        raise RuntimeError("PRIVATE provider failure")

    client.chat = broken_provider
    try:
        with pytest.raises(AgentRuntimeError, match="protected provider or host request failed"):
            session.run_turn("Observe the permitted app.")
    finally:
        session.close()
    assert terminal_evidence(transient)[0]["reportedOutcome"] == "failed"
    assert "PRIVATE" not in json.dumps(events + transient)


@pytest.mark.parametrize("dispatch", ["not_dispatched", "dispatched", "uncertain"])
def test_refusal_after_delivered_input_keeps_partial_work_and_never_replays(tmp_path, dispatch):
    requests, delivered = [], []

    def host(name, args):
        requests.append(name)
        if len(requests) == 1:
            return state()
        if len(requests) == 2:
            delivered.append("prior input remains")
            return action_result(args, state(2), effect="observed_change")
        return refused(dispatch=dispatch, phase="post_observation")

    action = {"action": "press", "target_type": "semantic", "element_id": "element-button"}
    session, client, events, transient = make_turn(
        tmp_path,
        host,
        [
            call("computer_state", {}, 1),
            call("computer_action", action, 2),
            call("computer_action", action, 3),
            call("computer_action", action, 4),
        ],
    )
    try:
        assert session.run_turn("Use the permitted app, preserving earlier completed work.") == 0
        assert session.last_turn_outcome["outcome"] == "blocked"
    finally:
        session.close()
    assert requests == ["computer_state", "computer_action", "computer_action"]
    assert delivered == ["prior input remains"]
    assert client.calls == 3
    text = " ".join(e["payload"].get("text", "") for e in events if e["type"] == "message_end")
    assert "Earlier established outcomes remain valid" in text
    assert "nothing was" not in text
    assert ("refused before it was sent" in text) is (dispatch == "not_dispatched")
    assert not [e for e in events if e["type"] == "error_raised"]
    assert terminal_evidence(transient)[0]["hostFailureCode"] == "target_changed"


@pytest.mark.parametrize("repeat_refusal", [False, True])
def test_first_state_reobservation_requires_authority_and_is_bounded(tmp_path, repeat_refusal):
    requests = []

    def host(name, args):
        requests.append(name)
        if len(requests) == 1 or repeat_refusal:
            return refused(authority="valid_for_reobserve")
        return state(2)

    # Repeated refusals must stop even if a scripted host keeps granting reads.
    responses = [call("computer_state", {}, 1), call("computer_state", {}, 2)]
    responses += [call("computer_state", {}, 3)] if repeat_refusal else [done()]
    session, client, events, transient = make_turn(tmp_path, host, responses)
    try:
        assert session.run_turn("Observe the permitted app without replaying launch.") == 0
        assert session.last_turn_outcome["outcome"] == (
            "blocked" if repeat_refusal else "completed_unverified"
        )
    finally:
        session.close()
    assert requests == ["computer_state", "computer_state"]
    assert client.calls == (2 if repeat_refusal else 3)
    assert not [e for e in events if e["type"] == "error_raised"]


@pytest.mark.parametrize(
    "next_response",
    [
        done(),
        call("computer_action", {"action": "activate_application"}, 2),
        call(
            "computer_action",
            {"action": "press", "target_type": "semantic", "element_id": "old"},
            2,
        ),
    ],
)
def test_ignoring_the_required_reobservation_finishes_blocked_before_any_other_host_call(
    tmp_path, next_response
):
    requests = []

    def host(name, args):
        requests.append(name)
        return refused(authority="valid_for_reobserve")

    session, client, events, transient = make_turn(
        tmp_path, host, [call("computer_state", {}, 1), next_response]
    )
    try:
        assert session.run_turn("Observe the permitted app without repeating launch or input.") == 0
        assert session.last_turn_outcome["outcome"] == "blocked"
    finally:
        session.close()
    assert requests == ["computer_state"] and client.calls == 2
    assert not [e for e in events if e["type"] == "error_raised"]
    assert terminal_evidence(transient)[0]["hostFailureCode"] == "target_changed"


@pytest.mark.parametrize(
    "cause",
    [
        "computer_use_cancelled",
        "user_takeover",
        "computer_use_action_budget_exhausted",
        "computer_use_window_selection_exhausted",
        "computer_use_lease_expired",
        "computer_use_request_expired",
        "computer_use_full_access_revoked",
        "computer_use_binding_mismatch",
    ],
)
def test_terminal_control_refusal_cannot_grant_recovery_from_contradictory_flags(tmp_path, cause):
    requests = []

    def host(name, args):
        requests.append(name)
        return refused(cause=cause, authority="valid_for_reobserve")

    session, client, _, _ = make_turn(tmp_path, host, [call("computer_state", {}, 1), done()])
    try:
        assert session.run_turn("Observe only while authorized.") == 1
        assert session.last_turn_outcome["outcome"] != "blocked"
    finally:
        session.close()
    assert requests == ["computer_state"] and client.calls == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "two_fields",
        "foreign_cause",
        "unknown_code",
        "bad_dispatch",
        "bad_authority",
        "bad_phase",
        "bad_effect",
        "extra_field",
    ],
)
def test_malformed_or_unknown_feedback_does_not_become_a_blocked_outcome_or_recovery(
    tmp_path, mutation
):
    result = refused(authority="valid_for_reobserve")
    feedback = result["interaction_feedback"]
    if mutation == "missing":
        del result["interaction_feedback"]
    elif mutation == "two_fields":
        result["interaction_feedback"] = {
            "dispatch": "not_dispatched",
            "authority": "valid_for_reobserve",
        }
    elif mutation == "foreign_cause":
        feedback["cause"] = "target_window_changed"
    elif mutation == "unknown_code":
        result["error"] = feedback["cause"] = "PRIVATE_unknown"
    elif mutation == "extra_field":
        feedback["private"] = "PRIVATE"
    else:
        feedback[mutation.removeprefix("bad_")] = "PRIVATE"
    requests = []

    def host(name, args):
        requests.append(name)
        return result

    session, client, events, transient = make_turn(
        tmp_path, host, [call("computer_state", {}, n) for n in range(1, 5)]
    )
    try:
        assert session.run_turn("Observe only while the host authorizes it.") == 1
        assert session.last_turn_outcome["outcome"] != "blocked"
    finally:
        session.close()
    assert requests == ["computer_state"]
    assert client.calls == 1
    assert [e for e in events if e["type"] == "error_raised"]
    assert "PRIVATE" not in json.dumps(events + transient)


def test_real_host_exception_remains_a_failure_with_structured_exception_evidence(tmp_path):
    requests = []

    def host(name, args):
        requests.append(name)
        raise RuntimeError("PRIVATE host failure")

    session, client, events, transient = make_turn(
        tmp_path, host, [call("computer_state", {}, 1), done()]
    )
    try:
        assert session.run_turn("Observe the permitted app.") == 1
        assert session.last_turn_outcome["outcome"] != "blocked"
    finally:
        session.close()
    assert requests == ["computer_state"] and client.calls == 1
    assert terminal_evidence(transient)[0]["reportedOutcome"] == "failed"
    assert any(
        e["type"] == "computer_use_call_diagnostic"
        and e["payload"].get("exceptionClass") == "RuntimeError"
        for e in events
    )
    assert "PRIVATE" not in json.dumps(events + transient)


def test_stop_wins_over_recoverable_host_refusal(tmp_path):
    token = InteractiveCancellationToken()
    requests = []

    def host(name, args):
        requests.append(name)
        token.cancel()
        return refused(authority="valid_for_reobserve")

    session, client, events, transient = make_turn(
        tmp_path, host, [call("computer_state", {}, 1), done()]
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            session.run_turn("Observe the permitted app.", cancellation_token=token)
    finally:
        session.close()
    assert requests == ["computer_state"] and client.calls == 1
    assert terminal_evidence(transient)[0]["reportedOutcome"] == "cancelled"


def test_takeover_is_not_reclassified_as_a_blocked_refusal(tmp_path):
    requests = []

    def host(name, args):
        requests.append(name)
        return refused(cause="user_takeover", authority="takeover")

    session, client, events, transient = make_turn(
        tmp_path, host, [call("computer_state", {}, 1), done()]
    )
    try:
        assert session.run_turn("Observe the permitted app.") != 0
        assert session.last_turn_outcome["outcome"] != "blocked"
    finally:
        session.close()
    assert requests == ["computer_state"] and client.calls == 1


def test_r_adds_exactly_target_changed_to_the_diagnostic_code_set():
    # Frozen pre-W6 (cf204f87) diagnostic vocabulary; R allows one addition.
    before_w6 = frozenset(
        """
        background_synthetic_input_unsupported
        computer_observation_unavailable
        computer_use_action_budget_exhausted
        computer_use_action_outcome_uncertain
        computer_use_approval_denied
        computer_use_approval_expired
        computer_use_binding_mismatch
        computer_use_broker_changed
        computer_use_budget_refused
        computer_use_cancelled
        computer_use_fresh_observation_required
        computer_use_full_access_revoked
        computer_use_invalid_keys_not_dispatched
        computer_use_key_target_not_routable
        computer_use_lease_expired
        computer_use_lease_renewal_required
        computer_use_observation_recovery_required
        computer_use_permission_required
        computer_use_request_expired
        computer_use_request_in_progress
        computer_use_route_changed
        computer_use_stale_element_not_dispatched
        computer_use_stale_window_not_dispatched
        computer_use_timeout
        computer_use_verification_unavailable
        computer_use_window_selection_exhausted
        control_proof_rejected
        invalid_file_arguments
        invalid_file_response
        invalid_host_image
        invalid_host_observation
        invalid_host_response
        invalid_host_workflow
        invalid_observation_arguments
        invalid_observation_assertions
        invalid_tool_arguments_json
        invalid_upload_arguments
        invalid_upload_objective_response
        invalid_upload_response
        invalid_verification_proposal
        none
        other
        out_of_scope
        post_action_observation_missing
        repeated_tool_failure_guard
        secure_target_refused
        stale_element
        stale_page_cursor
        target_app_changed
        target_closed
        target_window_ambiguous
        target_window_changed
        target_window_not_found
        tool_contract_recovery_blocked
        unexpected_host_image
        unknown_tool
        user_takeover
        window_content_protected
        window_reference_invalid
        window_reference_stale
        window_reference_unknown
        window_reselection_required
        window_selection_pending
        window_selection_unsupported
    """.split()
    )
    assert RESULT_CODES == before_w6 | {"target_changed"}
