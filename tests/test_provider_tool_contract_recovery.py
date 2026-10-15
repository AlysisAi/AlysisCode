from __future__ import annotations

import copy
import json
import threading

import pytest

from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent.sensitive_output import canonicalize_computer_use_response_for_dispatch
from alysis_code.agent_loop import ToolDef
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.hooks import HookDispatchResult
from alysis_code.llm.metadata import OPENAI_RESPONSES_PROVIDER_METADATA_KEY
from alysis_code.llm.types import LLMError, LLMResponse, ToolCall
from tests.test_execution_deadline import _FakeClock
from tests.test_sensitive_tool_output_boundary import (
    _computer_use_durable_boundary_bundle,
    _make_computer_use_session,
    _ScriptedClient,
)

NAME = "mcp__private_contract__inspect"
WRITE = "mcp__private_contract__change"
SECRET = "REJECTED_PROVIDER_PAYLOAD_8f19f0"


def response(calls, *, mismatch=False):
    return LLMResponse(
        content="",
        tool_calls=calls,
        raw={"private": SECRET},
        provider_metadata={
            OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                "output_items": [
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(
                            {"private": SECRET} if mismatch else call.arguments
                        ),
                    }
                    for call in calls
                ]
            }
        },
    )


@pytest.mark.parametrize("fault", ["unknown", "missing", "duplicate", "reused", "metadata"])
def test_rejected_batch_is_transactional_for_seen_ids(fault):
    calls = [ToolCall(id="fresh", name=NAME, arguments={})]
    seen = {"completed"}
    if fault == "unknown":
        calls.append(ToolCall(id=SECRET, name=SECRET, arguments={"private": SECRET}))
    elif fault == "missing":
        calls.append(ToolCall(id="", name=NAME, arguments={}))
    elif fault == "duplicate":
        calls.append(ToolCall(id="fresh", name=NAME, arguments={}))
    elif fault == "reused":
        calls.append(ToolCall(id="completed", name=NAME, arguments={}))
    with pytest.raises(ValueError):
        canonicalize_computer_use_response_for_dispatch(
            response(calls, mismatch=fault == "metadata"),
            used_protocol_ids=seen,
            allowed_tool_names=[NAME],
        )
    assert seen == {"completed"}
    good = response([ToolCall(id="fresh", name=NAME, arguments={})])
    accepted = canonicalize_computer_use_response_for_dispatch(
        good, used_protocol_ids=seen, allowed_tool_names=[NAME]
    )
    assert "fresh" in seen and accepted.tool_calls[0].id in seen


def session_for(tmp_path, responses, *, client=None, max_steps=5, uncertain=False):
    dispatched = []
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=responses,
        host_request=lambda *_: pytest.fail("No Computer Use host"),
        client=client,
        max_steps=max_steps,
    )
    session.sensitive_media = False
    session.mode = "fullaccess"
    session.tools = {
        name: ToolDef(
            name=name,
            description="Controlled ephemeral capability",
            parameters={"type": "object", "properties": {"value": {"type": "string"}}},
            run=lambda args, n=name: (
                dispatched.append((n, copy.deepcopy(args)))
                or (
                    {"error": "uncertain_write", "retryable": False}
                    if uncertain and n == WRITE
                    else {"ok": True}
                )
            ),
            metadata={"tool_type": "mcp", "ephemeral_io": True},
        )
        for name in (NAME, WRITE)
    }
    session.tool_list = [t.as_openai_tool() for t in session.tools.values()]
    return session, captures, dispatched


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("unknown", "unknown_computer_use_tool"),
        ("missing", "missing_tool_call_id"),
        ("duplicate", "reused_tool_call_id"),
        ("metadata", "provider_tool_metadata_mismatch"),
    ],
)
def test_real_ephemeral_turn_recovers_before_dispatch_and_persists_only_safe_diagnostics(
    tmp_path, fault, expected
):
    calls = [ToolCall(id="valid-sibling", name=WRITE, arguments={"value": SECRET})]
    if fault == "unknown":
        calls.append(ToolCall(id=SECRET, name=SECRET, arguments={"private": SECRET}))
    elif fault == "missing":
        calls.append(ToolCall(id="", name=NAME, arguments={}))
    elif fault == "duplicate":
        calls.append(ToolCall(id="valid-sibling", name=NAME, arguments={}))
    invalid = response(calls, mismatch=fault == "metadata")
    responses = [
        invalid,
        response([ToolCall(id="fresh-valid", name=NAME, arguments={})]),
        LLMResponse(content="Inspection complete.", tool_calls=[], raw={}),
    ]
    session, captures, dispatched = session_for(tmp_path, responses)
    client, surface, hooks, diagnostics, store = captures
    try:
        assert session.run_turn("Inspect the controlled record.") == 0
        messages = copy.deepcopy(session.messages)
    finally:
        session.close()
    assert dispatched == [(NAME, {})]
    assert client.calls == 3
    events = [
        e for e in diagnostics.events if e["event_type"] == "computer_use_provider_contract_failure"
    ]
    assert events[0]["payload"]["reason"] == expected
    assert events[0]["payload"]["scope"] == "ephemeral_tools"
    assert events[0]["payload"]["retry"] is True
    bundle = _computer_use_durable_boundary_bundle(
        client=client,
        surface=surface,
        hooks=hooks,
        diagnostics=diagnostics,
        messages=messages,
        log_path=store.path,
    )
    assert SECRET not in bundle
    assert "No calls from the rejected response were executed" in client.request_snapshots[1]


@pytest.mark.parametrize("uncertain", [False, True])
def test_recovery_never_replays_an_already_dispatched_or_uncertain_write(tmp_path, uncertain):
    calls = [ToolCall(id="completed-write", name=WRITE, arguments={"value": "once"})]
    responses = [
        response(calls),
        response(calls),
        response([ToolCall(id="fresh-read", name=NAME, arguments={})]),
        LLMResponse(content="Inspection complete.", tool_calls=[], raw={}),
    ]
    session, captures, dispatched = session_for(tmp_path, responses, uncertain=uncertain)
    try:
        assert session.run_turn("Change the controlled record once and inspect it.") == 0
    finally:
        session.close()
    assert dispatched == [(WRITE, {"value": "once"}), (NAME, {})]
    assert captures[0].calls == 4


def test_repeated_invalid_response_stops_after_one_regeneration_without_dispatch(tmp_path):
    invalid = response([ToolCall(id=SECRET, name=SECRET, arguments={"private": SECRET})])
    session, captures, dispatched = session_for(tmp_path, [invalid])
    try:
        with pytest.raises((LLMError, AgentRuntimeError), match="malformed tool call"):
            session.run_turn("Inspect the controlled record.")
    finally:
        session.close()
    assert captures[0].calls == 2
    assert dispatched == []
    failures = [
        e for e in captures[3].events if e["event_type"] == "computer_use_provider_contract_failure"
    ]
    assert [e["payload"]["retry"] for e in failures] == [True, False]
    assert SECRET not in json.dumps(failures)


def test_step_budget_refuses_regeneration_and_dispatch(tmp_path):
    invalid = response([ToolCall(id="", name=NAME, arguments={})])
    session, captures, dispatched = session_for(tmp_path, [invalid], max_steps=1)
    try:
        with pytest.raises((LLMError, AgentRuntimeError), match="malformed tool call"):
            session.run_turn("Inspect the controlled record.")
    finally:
        session.close()
    assert captures[0].calls == 1 and dispatched == []


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("delay_with_read", [False, True])
def test_fresh_id_write_resubmission_is_blocked_throughout_recovery(
    tmp_path, uncertain, delay_with_read
):
    first = response([ToolCall(id="first-write", name=WRITE, arguments={"value": "once"})])
    rejected = response([ToolCall(id="invalid-read", name="unavailable", arguments={})])
    repeated = ToolCall(
        id="fresh-write",
        name=WRITE,
        arguments={"value": "once"},
        provider_metadata={"read_only": True},
    )
    scripted = [first, rejected]
    if delay_with_read:
        scripted.append(response([ToolCall(id="fresh-read", name=NAME, arguments={})]))
    scripted.extend(
        [
            response([repeated]),
            LLMResponse(content="Prior outcome retained.", tool_calls=[], raw={}),
        ]
    )
    session, captures, dispatched = session_for(
        tmp_path, scripted, uncertain=uncertain, max_steps=6
    )
    try:
        assert session.run_turn("Change the record exactly once and inspect its result.") == 0
        assert session.mode == "fullaccess"
    finally:
        session.close()
    expected = [(WRITE, {"value": "once"})]
    if delay_with_read:
        expected.append((NAME, {}))
    assert dispatched == expected
    assert captures[0].calls == 4 + int(delay_with_read)
    blocked = [
        e for e in captures[3].events if e["event_type"] == "tool_contract_recovery_batch_blocked"
    ]
    assert len(blocked) == 1
    assert blocked[0]["payload"]["reason"] == "recovery_operation_repeat"


@pytest.mark.parametrize("uncertain", [False, True])
def test_recovery_repeat_rejects_all_siblings_before_hooks(tmp_path, uncertain):
    scripted = [
        response([ToolCall(id="first-write", name=WRITE, arguments={"value": "once"})]),
        response([ToolCall(id="invalid-read", name="unavailable", arguments={})]),
        response(
            [
                ToolCall(id="new-write", name=WRITE, arguments={"value": "different"}),
                ToolCall(id="fresh-duplicate", name=WRITE, arguments={"value": "once"}),
                ToolCall(id="fresh-read", name=NAME, arguments={}),
            ]
        ),
        LLMResponse(content="Prior outcome retained.", tool_calls=[], raw={}),
    ]
    session, captures, dispatched = session_for(tmp_path, scripted, uncertain=uncertain)
    try:
        assert session.run_turn("Change the record once and inspect its result.") == 0
    finally:
        session.close()
    assert dispatched == [(WRITE, {"value": "once"})]
    assert (
        len([p for p in captures[2].payloads if "tool_input" in p and "tool_response" not in p])
        == 1
    )


def test_previously_rejected_write_has_one_legitimate_first_execution(tmp_path):
    write = ToolCall(id="previously-rejected", name=WRITE, arguments={"value": "once"})
    scripted = [
        response([write, ToolCall(id="bad-sibling", name="unavailable", arguments={})]),
        response([write]),
        response([ToolCall(id="inspect", name=NAME, arguments={})]),
        LLMResponse(content="Change inspected.", tool_calls=[], raw={}),
    ]
    session, captures, dispatched = session_for(tmp_path, scripted)
    try:
        assert session.run_turn("Change the record once and inspect its result.") == 0
    finally:
        session.close()
    assert dispatched == [(WRITE, {"value": "once"}), (NAME, {})]
    assert captures[0].calls == 4


def test_new_distinct_write_and_repeated_bound_read_remain_available(tmp_path):
    scripted = [
        response([ToolCall(id="first-write", name=WRITE, arguments={"value": "first"})]),
        response([ToolCall(id="first-read", name=NAME, arguments={})]),
        response([ToolCall(id="invalid-read", name="unavailable", arguments={})]),
        response(
            [
                ToolCall(id="fresh-read", name=NAME, arguments={}),
                ToolCall(id="new-write", name=WRITE, arguments={"value": "second"}),
            ]
        ),
        LLMResponse(content="Changes inspected.", tool_calls=[], raw={}),
    ]
    session, captures, dispatched = session_for(tmp_path, scripted, max_steps=6)
    session.tools[NAME].metadata["read_only"] = True
    try:
        assert session.run_turn("Apply the requested changes and inspect each result.") == 0
    finally:
        session.close()
    assert dispatched == [
        (WRITE, {"value": "first"}),
        (NAME, {}),
        (NAME, {}),
        (WRITE, {"value": "second"}),
    ]
    assert captures[0].calls == 5


def test_repeated_computer_observation_is_available_after_recovery(tmp_path):
    dispatched = []
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[
            response([ToolCall(id="first-observation", name="computer_state", arguments={})]),
            response([ToolCall(id="bad", name="unavailable", arguments={})]),
            response([ToolCall(id="fresh-observation", name="computer_state", arguments={})]),
            LLMResponse(content="Observation complete.", tool_calls=[], raw={}),
        ],
        host_request=lambda name, args: (
            dispatched.append((name, copy.deepcopy(args)))
            or {"snapshot_id": "controlled-snapshot", "elements": []}
        ),
    )
    try:
        assert session.run_turn("Observe the approved surface again to confirm its state.") == 0
    finally:
        session.close()
    assert dispatched == [("computer_state", {}), ("computer_state", {})]
    assert captures[0].calls == 4


def test_duplicate_operations_in_a_regenerated_batch_never_start(tmp_path):
    session, captures, dispatched = session_for(
        tmp_path,
        [
            response([ToolCall(id="bad", name="unavailable", arguments={})]),
            response(
                [
                    ToolCall(id="write-a", name=WRITE, arguments={"value": "once"}),
                    ToolCall(id="write-b", name=WRITE, arguments={"value": "once"}),
                ]
            ),
            LLMResponse(content="No change was made.", tool_calls=[], raw={}),
        ],
    )
    try:
        assert session.run_turn("Change the record once.") == 0
    finally:
        session.close()
    assert dispatched == [] and captures[0].calls == 3
    assert not [p for p in captures[2].payloads if "tool_input" in p and "tool_response" not in p]


def test_normal_full_access_and_next_turn_allow_requested_repetition(tmp_path):
    class NextTurnClient(_ScriptedClient):
        def chat(self, **kwargs):
            # A new session turn clears previous private provider replay. The
            # controller ledger must also be turn-local.
            return super().chat(**kwargs)

    client = NextTurnClient(
        [
            response([ToolCall(id="bad", name="unavailable", arguments={})]),
            response([ToolCall(id="first-write", name=WRITE, arguments={"value": "once"})]),
            LLMResponse(content="First change done.", tool_calls=[], raw={}),
            response([ToolCall(id="second-write", name=WRITE, arguments={"value": "once"})]),
            response([ToolCall(id="third-write", name=WRITE, arguments={"value": "once"})]),
            LLMResponse(content="Requested repetition done.", tool_calls=[], raw={}),
        ]
    )
    session, _, dispatched = session_for(tmp_path, [], client=client)
    try:
        assert session.run_turn("Change the record once.") == 0
        assert session.run_turn("Repeat that change twice now.") == 0
        assert session.mode == "fullaccess"
    finally:
        session.close()
    assert dispatched == [(WRITE, {"value": "once"})] * 3


def test_dispatch_guard_catches_a_hook_rewriting_new_intent_to_prior_operation(tmp_path):
    scripted = [
        response([ToolCall(id="first", name=WRITE, arguments={"value": "once"})]),
        response([ToolCall(id="bad", name="unavailable", arguments={})]),
        response([ToolCall(id="fresh", name=WRITE, arguments={"value": "different"})]),
        LLMResponse(content="Prior outcome retained.", tool_calls=[], raw={}),
    ]
    session, captures, dispatched = session_for(tmp_path, scripted)
    # These controlled tools are ordinary bound handlers, so hooks may edit
    # input. The dispatch guard must check the effective input again.
    session.tools[WRITE].metadata["ephemeral_io"] = False
    original = captures[2].fire_pre_tool_use

    def rewrite(**payload):
        original(**payload)
        return HookDispatchResult(modified_input={"value": "once"})

    captures[2].fire_pre_tool_use = rewrite
    try:
        assert session.run_turn("Change the record once and inspect its result.") == 0
    finally:
        session.close()
    assert dispatched == [(WRITE, {"value": "once"})]


def test_response_construction_failure_never_commits_protocol_ids(monkeypatch):
    from alysis_code.agent import sensitive_output

    seen = {"already-accepted"}
    good = response([ToolCall(id="fresh", name=NAME, arguments={})])

    def fail(*args, **kwargs):
        raise RuntimeError("Controlled construction failure")

    with monkeypatch.context() as patch:
        patch.setattr(sensitive_output, "_replace_computer_use_response", fail)
        with pytest.raises(RuntimeError, match="Controlled construction failure"):
            canonicalize_computer_use_response_for_dispatch(
                good, used_protocol_ids=seen, allowed_tool_names=[NAME]
            )
    assert seen == {"already-accepted"}


def test_builtin_coercion_and_declared_default_do_not_hide_recovery_repeat(tmp_path):
    scripted = [
        response([ToolCall(id="first", name=WRITE, arguments={"value": "once"})]),
        response([ToolCall(id="bad", name="unavailable", arguments={})]),
        response([ToolCall(id="fresh", name=WRITE, arguments={"count": "1", "value": "once"})]),
        LLMResponse(content="Prior outcome retained.", tool_calls=[], raw={}),
    ]
    session, _, dispatched = session_for(tmp_path, scripted)
    session.tools[WRITE].metadata["tool_type"] = "builtin"
    session.tools[WRITE].parameters["properties"]["count"] = {"type": "integer", "default": 1}
    try:
        assert session.run_turn("Change the record exactly once and inspect its result.") == 0
    finally:
        session.close()
    assert dispatched == [(WRITE, {"value": "once"})]


def test_computer_use_uses_the_same_safe_pre_dispatch_recovery(tmp_path):
    dispatched = []
    invalid = response([ToolCall(id=SECRET, name=SECRET, arguments={"private": SECRET})])
    valid = response([ToolCall(id="fresh-observation", name="computer_state", arguments={})])
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[
            invalid,
            valid,
            LLMResponse(content="Observation complete.", tool_calls=[], raw={}),
        ],
        host_request=lambda name, args: (
            dispatched.append((name, copy.deepcopy(args)))
            or {"snapshot_id": "controlled-snapshot", "elements": []}
        ),
    )
    try:
        assert session.run_turn("Observe the approved surface safely.") == 0
        messages = copy.deepcopy(session.messages)
    finally:
        session.close()
    assert dispatched == [("computer_state", {})]
    assert captures[0].calls == 3
    failures = [
        e for e in captures[3].events if e["event_type"] == "computer_use_provider_contract_failure"
    ]
    assert failures[0]["payload"]["scope"] == "computer_use"
    bundle = _computer_use_durable_boundary_bundle(
        client=captures[0],
        surface=captures[1],
        hooks=captures[2],
        diagnostics=captures[3],
        messages=messages,
        log_path=captures[4].path,
    )
    assert SECRET not in bundle


def test_untrusted_error_code_and_prose_are_not_diagnostics_or_regeneration_context(
    tmp_path, monkeypatch
):
    from alysis_code.agent.turn import core

    validate = core.canonicalize_computer_use_response_for_dispatch
    attempts = []

    def reject_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            error = ValueError(SECRET)
            error.computer_use_error_code = SECRET
            raise error
        return validate(*args, **kwargs)

    monkeypatch.setattr(core, "canonicalize_computer_use_response_for_dispatch", reject_once)
    good = response([ToolCall(id="fresh-valid", name=NAME, arguments={})])
    session, captures, dispatched = session_for(
        tmp_path, [good, good, LLMResponse(content="Inspection complete.", tool_calls=[], raw={})]
    )
    try:
        assert session.run_turn("Inspect the controlled record.") == 0
        messages = copy.deepcopy(session.messages)
    finally:
        session.close()
    assert dispatched == [(NAME, {})]
    failures = [
        e for e in captures[3].events if e["event_type"] == "computer_use_provider_contract_failure"
    ]
    assert failures[0]["payload"]["reason"] == "invalid_tool_linkage"
    bundle = _computer_use_durable_boundary_bundle(
        client=captures[0],
        surface=captures[1],
        hooks=captures[2],
        diagnostics=captures[3],
        messages=messages,
        log_path=captures[4].path,
    )
    assert SECRET not in bundle


def test_cancellation_between_regeneration_and_dispatch_never_calls_a_tool(tmp_path):
    event = threading.Event()
    invalid = response([ToolCall(id="", name=NAME, arguments={})])
    good = response([ToolCall(id="fresh-valid", name=NAME, arguments={})])

    class CancellingClient(_ScriptedClient):
        def chat(self, **kwargs):
            result = super().chat(**kwargs)
            if self.calls == 2:
                event.set()
            return result

    client = CancellingClient([invalid, good])
    session, captures, dispatched = session_for(tmp_path, [], client=client)
    try:
        with pytest.raises(CooperativeCancellationError):
            session.run_turn(
                "Inspect the controlled record.", cancellation_token=EventCancellationToken(event)
            )
        assert session.last_turn_outcome["reason"] == "cancelled"
    finally:
        session.close()
    assert captures[0].calls == 2 and dispatched == []


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_cancellation_or_deadline_during_validation_prevents_regeneration(
    tmp_path, monkeypatch, stop
):
    event = threading.Event()
    clock = _FakeClock()
    invalid = response([ToolCall(id="", name=NAME, arguments={})])
    session, captures, dispatched = session_for(tmp_path, [invalid])
    if stop == "deadline":
        session.execution_deadline = ExecutionDeadline.from_duration(60, clock=clock)
    from alysis_code.agent.turn import core

    validate = core.canonicalize_computer_use_response_for_dispatch

    def reject(*args, **kwargs):
        if stop == "cancel":
            event.set()
        else:
            clock.advance(61)
        return validate(*args, **kwargs)

    monkeypatch.setattr(core, "canonicalize_computer_use_response_for_dispatch", reject)
    try:
        if stop == "cancel":
            with pytest.raises(CooperativeCancellationError):
                session.run_turn(
                    "Inspect the controlled record.",
                    cancellation_token=EventCancellationToken(event),
                )
            assert session.last_turn_outcome["reason"] == "cancelled"
        else:
            assert session.run_turn("Inspect the controlled record.") == 0
            assert session.stop_reason == "run_budget_exhausted"
            assert session.last_turn_outcome["reason"] == "deadline_exhausted"
    finally:
        session.close()
    assert captures[0].calls == 1 and dispatched == []
