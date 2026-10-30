"""LR-43: original owned refusals through the real bounded protocol surface."""

import copy
import json

import pytest
from test_computer_start_resolved_contract import entry
from test_computer_start_resolved_turn import CORPUS, body, session_for

from alysis_code.agent.computer_start_resolved import FRESH, HOST_CODES, RETAIN, Refusal
from alysis_code.computer_use_call_diagnostics import (
    RESOLVED_START_REFUSAL_CODES,
    sanitize_computer_use_evidence,
)
from alysis_code.ide.event_stream import EventContext, ProtocolEventSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def terminal(code):
    return dict(
        version=1,
        kind="terminal",
        terminalOrigin="host_error",
        reportedOutcome="blocked",
        stateCalls=0,
        actionCalls=0,
        knownHostFailure=False,
        resolvedStartRefusalCode=code,
    )


@pytest.mark.parametrize("protocol", ["compat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "case,expected",
    [
        ("incomplete", "computer_start_inventory_incomplete"),
        ("unavailable", "computer_use_inventory_unavailable"),
        ("proof", "computer_start_response_invalid"),
        ("complete", None),
    ],
)
def test_original_turn_to_protocol_evidence(
    tmp_path, monkeypatch, protocol, stream, case, expected
):
    descriptor = copy.deepcopy(CORPUS["bases"]["descriptor"])
    if case in {"incomplete", "unavailable"}:
        descriptor["discovery"].update(coverage=case, targets=[])
    monkeypatch.setitem(CORPUS["bases"], "descriptor", descriptor)
    if case == "proof":
        from alysis_code.llm import resolved_entry

        # Original raw parser runs, but cannot mint the call's lexical proof.
        monkeypatch.setattr(resolved_entry, "arguments", lambda raw, **kw: (json.loads(raw), None))
    session, calls, requests = session_for(
        tmp_path,
        monkeypatch,
        protocol,
        stream,
        [body(protocol, stream, json.dumps(CORPUS["bases"]["plan"]))],
    )
    events, transient = [], []
    session.surface = ProtocolEventSurface(
        context=EventContext(
            session_id="synthetic-session", run_id="synthetic-run", job_id="synthetic-job"
        ),
        emit=events.append,
        emit_transient=transient.append,
        computer_use_evidence_version=1,
    )
    try:
        assert session.run_turn("Read the synthetic authorized view.") == 0
        assert len(requests) == 1
        terminals = [
            e["payload"]
            for e in transient
            if e["type"] == "computer_use_evidence" and e["payload"].get("kind") == "terminal"
        ]
        if expected:
            assert len(terminals) == 1
            payload = terminals[0]
            assert payload["stateCalls"] == payload["actionCalls"] == 0
            assert payload["resolvedStartRefusalCode"] == expected
            assert session.last_turn_outcome["reason"] == expected and not calls
            assert payload == terminal(expected)
        else:
            assert terminals == [] and len(calls) == 1
        if expected:
            assert sanitize_computer_use_evidence(payload) == payload
            assert len(json.dumps(payload).encode()) < 512
    finally:
        session.close()


@pytest.mark.parametrize("code", sorted(RESOLVED_START_REFUSAL_CODES))
def test_projection_requires_original_unchanged_current_receipt(code):
    e, _, _ = entry()
    try:
        value = e.refusal(code)
        assert e.terminal_refusal_evidence_code(value) == code
        assert e.terminal_refusal_evidence_code(dict(value)) is None
        assert e.terminal_refusal_evidence_code(Refusal(code, "not_dispatched")) is None
        value["dispatch"] = "PRIVATE /path target nonce prose"
        assert e.terminal_refusal_evidence_code(value) is None
        value["dispatch"] = "not_dispatched"
        e.retire("new_owner")
        assert e.terminal_refusal_evidence_code(value) is None
    finally:
        e.client._transport.close()


@pytest.mark.parametrize("code", [*sorted(RETAIN), "unknown", "PRIVATE /path window", None, {}, 1])
def test_unknown_and_nonterminal_values_have_no_evidence(code):
    e, _, _ = entry()
    try:
        if type(code) is str:
            assert e.terminal_refusal_evidence_code(e.refusal(code)) is None
        assert sanitize_computer_use_evidence(terminal(code)) is None
    finally:
        e.client._transport.close()


def test_closed_enum_matches_original_terminal_issuance():
    assert (
        RESOLVED_START_REFUSAL_CODES
        == (
            HOST_CODES
            | FRESH
            | {
                "computer_start_response_invalid",
                "browser_start_timeout",
                "computer_selection_recovery_exhausted",
            }
        )
        - RETAIN
    )


@pytest.mark.parametrize("code", sorted(RESOLVED_START_REFUSAL_CODES))
def test_closed_surface_enum_and_unknown_drop(code):
    transient = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="synthetic"),
        emit=lambda _: None,
        emit_transient=transient.append,
        computer_use_evidence_version=1,
    )
    emit = surface.bind_computer_use_evidence_emitter()
    payload = terminal(code)
    emit(payload)
    assert transient[-1]["payload"] == payload
    count = len(transient)
    emit(terminal("PRIVATE /path target"))
    emit(dict(payload, unexpected="PRIVATE"))
    emit(dict(payload, kind="provider"))
    assert len(transient) == count
    legacy = dict(payload)
    legacy.pop("resolvedStartRefusalCode")
    emit(legacy)
    assert transient[-1]["payload"] == legacy


@pytest.mark.parametrize("fault", ["copy", "mutated", "retired", "version_absent"])
def test_real_turn_does_not_publish_unowned_or_unnegotiated_code(tmp_path, monkeypatch, fault):
    from alysis_code.agent.computer_start_resolved import Entry

    descriptor = copy.deepcopy(CORPUS["bases"]["descriptor"])
    descriptor["discovery"].update(coverage="incomplete", targets=[])
    monkeypatch.setitem(CORPUS["bases"], "descriptor", descriptor)
    session, calls, _ = session_for(
        tmp_path,
        monkeypatch,
        "compat",
        False,
        [body("compat", False, json.dumps(CORPUS["bases"]["plan"]))],
    )
    original = Entry.dispatch

    def dispatch(self, *args):
        result = original(self, *args)
        if fault == "copy":
            return dict(result)
        if fault == "mutated":
            result["code"] = "PRIVATE /path window"
        elif fault == "retired":
            self.retire("different_owner")
        return result

    if fault != "version_absent":
        monkeypatch.setattr(Entry, "dispatch", dispatch)
    transient = []
    session.surface = ProtocolEventSurface(
        context=EventContext(session_id="synthetic"),
        emit=lambda _: None,
        emit_transient=transient.append,
        computer_use_evidence_version=None if fault == "version_absent" else 1,
    )
    try:
        if fault == "version_absent":
            assert session.run_turn("Read the synthetic authorized view.") == 0
        else:
            from alysis_code.llm.types import LLMError

            # Unowned tool output cannot finish the turn. The next original
            # entry request still refuses its retired capability.
            with pytest.raises(LLMError, match="^computer_start_capability_lost$"):
                session.run_turn("Read the synthetic authorized view.")
        assert not calls
        assert not [e for e in transient if e["type"] == "computer_use_evidence"]
    finally:
        session.close()


def test_actual_dispatch_currentness_refusal_projects_original_code():
    from test_computer_start_resolved_parser import parse, response

    e, calls, cfg = entry()
    try:
        with e.model_request(e.client, False, cfg):
            call = parse(
                "compat", response("compat", json.dumps(CORPUS["bases"]["plan"]))
            ).tool_calls[0]
        cfg.model = "changed-model"
        result = e.dispatch(call, call.arguments, e.client, False, cfg)
        assert not calls and result["dispatch"] == "not_dispatched"
        assert e.terminal_refusal_evidence_code(result) == "computer_start_capability_lost"
    finally:
        e.client._transport.close()


@pytest.mark.parametrize("protocol", ["compat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("evidence_version", [1, None, True])
def test_repeat_guard_owned_exhaustion_evidence(
    tmp_path, monkeypatch, protocol, stream, evidence_version
):
    """SOL-1: two hook refusals, one correction, then original retry guard."""
    from alysis_code.agent.computer_start_resolved import Entry
    from alysis_code.hooks.models import HookDispatchResult

    plan = copy.deepcopy(CORPUS["bases"]["plan"])
    other = copy.deepcopy(plan)
    other["app_steps"][0]["app"] = "Unknown"
    outputs = [body(protocol, stream, json.dumps(value)) for value in [plan, plan, other, plan]]
    session, calls, requests = session_for(tmp_path, monkeypatch, protocol, stream, outputs)

    class InertHookPort:
        def __init__(self):
            self.pre_calls = 0

        def fire_pre_tool_use(self, **kwargs):
            self.pre_calls += 1
            return HookDispatchResult(blocked=self.pre_calls <= 2, reason="Synthetic hook refusal")

        def __getattr__(self, name):
            return lambda **kwargs: HookDispatchResult()

    hook = InertHookPort()
    session.hook_dispatcher = hook
    transient = []
    session.surface = ProtocolEventSurface(
        context=EventContext(
            session_id="synthetic-session", run_id="synthetic-run", job_id="synthetic-job"
        ),
        emit=lambda _: None,
        emit_transient=transient.append,
        computer_use_evidence_version=evidence_version,
    )
    issued = []
    original = Entry.refusal

    def observe(self, code, *args, **kwargs):
        value = original(self, code, *args, **kwargs)
        if self is session.resolved_entry:
            issued.append(
                (self.owns_terminal_refusal(value), self.terminal_refusal_evidence_code(value))
            )
        return value

    monkeypatch.setattr(Entry, "refusal", observe)
    try:
        assert session.run_turn("Read the synthetic authorized view.") == 0
        assert not calls and len(requests) == 4 and hook.pre_calls == 3
        assert session.last_turn_outcome["reason"] == "computer_selection_recovery_exhausted"
        assert issued == [(True, "computer_selection_recovery_exhausted")]
    finally:
        session.close()
    # Includes final retirement/close: no second event or fallback provider call.
    terminals = [
        e["payload"]
        for e in transient
        if e["type"] == "computer_use_evidence" and e["payload"].get("kind") == "terminal"
    ]
    if type(evidence_version) is int and evidence_version == 1:
        assert terminals == [terminal("computer_selection_recovery_exhausted")]
        assert sanitize_computer_use_evidence(terminals[0]) == terminals[0]
        assert len(json.dumps(terminals[0]).encode()) < 512
    else:
        assert terminals == []
    assert len(requests) == 4 and not calls
