"""Production RPC/turn paths with synthetic responses, no live provider or desktop."""

import io
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from test_computer_interaction_v2 import call, done
from test_computer_use_attribution_contract import B
from test_computer_use_guidance import _protected, _scope_pair
from test_computer_use_host_refusal import make_turn, refused
from test_computer_use_skill import _ordinary_session
from test_computer_use_skill import isolated_paths as isolated_paths
from test_ide_computer_use_host import _bridge_session, _lines

from alysis_code import computer_use_attribution as a
from alysis_code import token_budget
from alysis_code.agent.browser_start import BrowserStartHandoff
from alysis_code.ide import stdio_bridge
from alysis_code.ide.protocol import ProtocolError, ProtocolRequest
from alysis_code.llm.cancellable_http import _transport_entry

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


@pytest.fixture(autouse=True)
def inert_tokenizer(monkeypatch):
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    monkeypatch.setattr(a, "_encoder_prior_unowned", False)


def send(bridge, out, method, context, params=None):
    wire = {
        "protocol_version": "1",
        "id": "synthetic-request",
        "method": method,
        "params": params or {},
    }
    if context is not None:
        wire["computer_use_attribution"] = {"version": 1, "context": context}
    bridge.process_line(json.dumps(wire))
    return next(x for x in reversed(_lines(out)) if x.get("id") == wire["id"])


def records(out):
    return [e for e in _lines(out) if e.get("type") == "computer_use_attribution"]


@pytest.mark.parametrize("opt_in", [False, True])
def test_management_real_rpc_config_boundary(monkeypatch, opt_in):
    out = io.StringIO()
    bridge = stdio_bridge.StdioBridge(stdout=out)
    result = send(bridge, out, "config.get", B["managementContext"] if opt_in else None)
    assert result["ok"] is True
    assert ("computer_use_attribution" in result) is opt_in
    if opt_in:
        sidecar = result["computer_use_attribution"]
        assert [r["phase"] for r in sidecar["records"]] == ["begin", "end"]
        assert sidecar["summary"]["spans"][0]["ended"] == 1
        assert a.valid_sidecar(
            result,
            {"requestId": result["id"], "method": "config.get", "context": B["managementContext"]},
        )
    assert records(out) == []


@pytest.mark.parametrize("opt_in", [False, True])
def test_startup_rpc_error_preserves_original_error_and_config_span(tmp_path, monkeypatch, opt_in):
    out = io.StringIO()
    bridge = stdio_bridge.StdioBridge(stdout=out)

    def broken_config():
        raise ProtocolError("fixture_rejection", "synthetic failure")

    monkeypatch.setattr(stdio_bridge, "load_config", broken_config)
    reply = send(
        bridge,
        out,
        "session.create",
        B["startupContext"] if opt_in else None,
        {"workspace": str(tmp_path), "mode": "review"},
    )
    assert reply["ok"] is False and reply["error"] == {
        "code": "fixture_rejection",
        "message": "synthetic failure",
    }
    if opt_in:
        assert reply["computer_use_attribution"]["records"][-1]["outcome"] == "failed"
        assert reply["computer_use_attribution"]["summary"]["closeReason"] == "failed"
    else:
        assert "computer_use_attribution" not in reply
    assert not records(out)


def test_real_protected_startup_boundaries(tmp_path, monkeypatch):
    c = a.Collector(B["protectedStartupContext"])
    with a.bind_collector(c):
        session, _ = _protected(tmp_path, monkeypatch, lambda *_: {}, [done()])
    session.close()
    summary = c.finish("returned")
    by_metric = {s["metric"]: s for s in summary["spans"]}
    for metric in ["model_metadata", "client_construction", "prompt_assembly", "fixed_tools"]:
        assert by_metric[metric]["ended"] >= 1
    assert by_metric["model_call"]["availability"] == "not_applicable"
    assert all(a.valid_record(r, c.context) for r in c.records)


@pytest.mark.parametrize("family", a.FAMILIES)
def test_ordinary_real_turn_classifies_and_correlates_actual_refusal(tmp_path, family):
    inventory, bad, good = _scope_pair(family)
    calls = []
    session, client = _ordinary_session(
        tmp_path,
        inventory,
        [
            call("computer_discover", {}, 1),
            call("computer_start", bad, 2),
            call("computer_start", good, 3),
        ],
        lambda args: calls.append(args) or BrowserStartHandoff("synthetic"),
    )
    c = a.Collector(B["turnContext"])
    try:
        with a.bind_collector(c):
            assert session.run_turn("Read the synthetic app. PRIVATE_SENTINEL") == 0
    finally:
        session.close()
    summary = c.finish("returned")
    assert summary["modelCategories"] == {"discover": 1, "start": 2, "other": 0, "pending": 0}
    assert summary["scopeRefusals"][family] == 1 and sum(summary["scopeRefusals"].values()) == 1
    refusal = next(r for r in c.records if r["kind"] == "scope_refusal")
    assert refusal["providerOrdinal"] == 2 and refusal["callOrdinal"] == 2
    assert summary["transportUnknownCalls"] == client.calls == 3 and len(calls) == 1
    assert "PRIVATE_SENTINEL" not in json.dumps([c.records, summary])
    assert "computer_use_attribution" not in json.dumps(client.call_records)


@pytest.mark.parametrize("opt_in", [False, True])
def test_real_bridge_turn_transient_owner_finalization_and_no_replay(tmp_path, opt_in):
    session, client, _, _ = make_turn(
        tmp_path, lambda *_: refused(), [call("computer_state", {}, 1)]
    )
    bridge, out, bs, job = _bridge_session(tmp_path)
    bs.agent_session = session
    session.surface = bs.surface
    client.base_url = "https://offline.invalid/v1"
    bs.computer_use_destination = stdio_bridge._actual_computer_use_destination(
        session, request_id=None
    )
    c = None
    if opt_in:
        request = ProtocolRequest(
            id="q",
            method="chat.send",
            params={"session_id": bs.session_id},
            computer_use_attribution=B["protectedTurnContext"],
        )
        c = bridge._attribution_rpc_owner(request)
        ack = bridge._attribution_accept_turn(
            request, {"session_id": bs.session_id, "job_id": job.job_id, "created": True}, c
        )
        assert ack["accepted"] is True
    try:
        bridge._run_session_turn(bs, job, "Observe PRIVATE_SENTINEL", [], [])
    finally:
        session.close()
    assert job.status == "completed" and job.result["task_outcome"]["outcome"] == "blocked"
    events = records(out)
    if not opt_in:
        assert events == []
        return
    assert [e["payload"]["record"]["kind"] for e in events] == ["span", "span", "summary"]
    assert all(e["job_id"] == job.job_id and e["session_id"] == bs.session_id for e in events)
    assert events[-1]["payload"]["record"]["modelCategories"]["other"] == 1
    assert events[-1]["payload"]["record"]["closeReason"] == "returned"
    assert "PRIVATE_SENTINEL" not in json.dumps(events)
    assert job.computer_use_attribution is None and c.closed
    before = deepcopy(c.summary)
    c.begin("model_call")
    assert c.summary == before
    assert "computer_use_attribution" not in json.dumps(job.result)
    assert not any(
        e.get("type") == "computer_use_attribution"
        for e in bridge._event_buffers.get(bs.session_id, [])
    )


@pytest.mark.parametrize("status", ["running", "completed"])
def test_C05_C06_repeated_send_never_rebinds(tmp_path, status):
    bridge, out, bs, job = _bridge_session(tmp_path)
    job.status = status
    old = a.Collector(B["protectedTurnContext"])
    job.computer_use_attribution = old
    replacement = a.Collector({**B["protectedTurnContext"], "contextOrdinal": 3})
    assert (
        bridge._attribution_accept_turn(
            None, {"session_id": bs.session_id, "job_id": job.job_id, "created": False}, replacement
        )
        is None
    )
    assert job.computer_use_attribution is old and records(out) == []


def test_C07_queued_job_captures_owner_and_stop_closes_once(tmp_path):
    bridge, out, bs, executing = _bridge_session(tmp_path)
    queued = stdio_bridge.BridgeJob(
        job_id="queued-j", session_id=bs.session_id, created_at="synthetic", status="queued"
    )
    bridge._jobs[queued.job_id] = queued
    c = a.Collector(B["protectedTurnContext"])
    assert bridge._attribution_accept_turn(
        None, {"session_id": bs.session_id, "job_id": queued.job_id, "created": True}, c
    )
    assert queued.computer_use_attribution is c and executing.computer_use_attribution is None
    queued.cancellation_event.set()
    stdio_bridge._mark_job_completed(queued, status="cancelled", exit_code=130)
    events = records(out)
    assert len(events) == 1 and events[0]["job_id"] == queued.job_id and events[0]["run_id"] is None
    assert events[0]["payload"]["record"]["closeReason"] == "cancelled"
    assert queued.computer_use_attribution is None
    stdio_bridge._mark_job_completed(queued, status="cancelled", exit_code=130)
    assert len(records(out)) == 1


def test_real_transport_callback_independent_of_evidence_and_retries():
    c = a.Collector(B["protectedTurnContext"])

    @a.model_call
    def invoke(**kwargs):
        request = httpx.Request("POST", "https://offline.invalid", content=b"PRIVATE_SENTINEL")
        for _ in range(3):
            assert _transport_entry(request) is None  # evidence v1 is not enabled
        return SimpleNamespace(tool_calls=[])

    with a.bind_collector(c):
        invoke(tools=[])
    summary = c.finish("returned")
    assert summary["transportAttempts"] == 3 and summary["transportUnknownCalls"] == 0
    assert c.records[-1]["transportAttempts"] == 3 and summary["modelCategories"]["other"] == 1
    assert "PRIVATE_SENTINEL" not in json.dumps(c.records)


def test_real_send_ack_precedes_deferred_start_and_idempotency_preserves_owner(
    tmp_path, monkeypatch
):
    bridge, out, bs, _ = _bridge_session(tmp_path)
    bs.sensitive_media = False
    bs.active_job = None
    session_id = bs.session_id
    observed = []

    def deferred(session):
        # Common dispatch must already have written the authenticated ACK.
        reply = next(x for x in reversed(_lines(out)) if x.get("id") == "synthetic-request")
        observed.append(reply)

    monkeypatch.setattr(bridge, "_drain_prompt_queue", deferred)
    params = {
        "session_id": session_id,
        "message": "Synthetic read only task",
        "idempotency_key": "synthetic-key",
    }
    reply = send(bridge, out, "chat.send", B["turnContext"], params)
    assert (
        reply["ok"] and reply["result"]["created"] and reply["computer_use_attribution"]["accepted"]
    )
    job = bridge._jobs[reply["result"]["job_id"]]
    owner = job.computer_use_attribution
    repeated = send(bridge, out, "chat.send", {**B["turnContext"], "contextOrdinal": 3}, params)
    assert repeated["result"]["created"] is False and "computer_use_attribution" not in repeated
    assert job.computer_use_attribution is owner and observed[0] == reply
    assert "computer_use_attribution" not in json.dumps(
        bridge._prompt_queue.list(session_id=session_id)[0].payload
    )
    bad = send(
        bridge, out, "chat.send", B["turnContext"], {"session_id": session_id, "message": ""}
    )
    assert not bad["ok"] and "computer_use_attribution" not in bad


@pytest.mark.parametrize("replacement", ["session", "job", "run", "closed"])
def test_late_owner_never_rebinds_or_emits(tmp_path, replacement):
    bridge, out, bs, job = _bridge_session(tmp_path)
    c = a.Collector(B["protectedTurnContext"])
    bridge._attribution_accept_turn(None, {"session_id": bs.session_id, "job_id": job.job_id}, c)
    bs.surface._thread_context.run_id = "original-run"
    span = c.begin("model_call")
    count = len(records(out))
    if replacement == "session":
        bridge._sessions.pop(bs.session_id)
    elif replacement == "job":
        bridge._jobs.pop(job.job_id)
    elif replacement == "run":
        bs.surface._thread_context.run_id = "replacement-run"
    else:
        bs.closed = True
    c.end(span)
    c.finish("retired")
    assert len(records(out)) == count


def test_protected_stop_keeps_business_cancellation_and_one_summary(tmp_path):
    from alysis_code.cancellation import InteractiveCancellationToken

    token = InteractiveCancellationToken()

    def host(*_):
        token.cancel()
        return refused()

    session, client, _, _ = make_turn(tmp_path, host, [call("computer_state", {}, 1), done()])
    c = a.Collector(B["protectedTurnContext"])
    try:
        with a.bind_collector(c), pytest.raises(KeyboardInterrupt):
            session.run_turn("Synthetic observe", cancellation_token=token)
    finally:
        session.close()
    summary = c.finish("cancelled")
    assert client.calls == 1 and summary["closeReason"] == "cancelled"
    assert c.finish("returned") is None


def test_startup_success_and_malformed_opt_in_preserve_business(tmp_path, monkeypatch):
    from test_ide_stdio_bridge import _create_review_session, _isolate_alysis_state

    _isolate_alysis_state(tmp_path, monkeypatch)
    out, bridge, _ = _create_review_session(tmp_path, monkeypatch, lambda *_: 0)
    params = {"workspace": str(tmp_path), "mode": "review", "model": "test-model"}
    reply = send(bridge, out, "session.create", B["startupContext"], params)
    assert reply["ok"], reply
    assert a.valid_sidecar(
        reply,
        {"requestId": reply["id"], "method": "session.create", "context": B["startupContext"]},
    )
    assert reply["computer_use_attribution"]["summary"]["spans"][0]["ended"] == 1
    malformed = {**B["startupContext"], "contextOrdinal": 257}
    reply = send(bridge, out, "session.create", malformed, params)
    assert reply["ok"] and "computer_use_attribution" not in reply
    duplicate = json.dumps(
        {"protocol_version": "1", "id": "duplicate", "method": "session.create", "params": params}
    )
    duplicate = (
        duplicate[:-1]
        + ', "computer_use_attribution":{"version":1,"version":2,"context":'
        + json.dumps(B["startupContext"])
        + "}}"
    )
    bridge.process_line(duplicate)
    reply = next(e for e in _lines(out) if e.get("id") == "duplicate")
    assert reply["ok"] and "computer_use_attribution" not in reply
