"""W9-F0-C1 regression: queued chat.send ACK versus the previous turn's queue drain.

Production objects only (real _chat_send, _drain_prompt_queue, _start_job_thread,
job_turn-wrapped _run_session_turn, _attribution_accept_turn, _mark_job_completed and
a real ordinary run_turn with one scripted model response). The only test control is
the thread interleaving: the previous turn finishes and drains the queue after the new
prompt is enqueued but before process_line attaches the turn collector.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from test_computer_discovery_recovery import _inventory
from test_computer_interaction_v2 import done
from test_computer_use_skill import _ordinary_session
from test_computer_use_skill import isolated_paths as isolated_paths
from test_ide_computer_use_host import _bridge_session, _lines

from alysis_code import computer_use_attribution as attribution
from alysis_code import token_budget
from alysis_code.agent.browser_start import BrowserStartHandoff
from alysis_code.ide import stdio_bridge

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

TURN = {
    "contextOrdinal": 2,
    "generationOrdinal": 1,
    "producerRole": "ordinary",
    "contextKind": "turn",
}


@pytest.fixture(autouse=True)
def inert_tokenizer(monkeypatch):
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    monkeypatch.setattr(attribution, "_encoder_prior_unowned", False)


def _run(tmp_path, monkeypatch, *, drain_before_ack):
    bridge, out, bs, previous = _bridge_session(tmp_path)
    bs.sensitive_media = False
    session, client = _ordinary_session(
        tmp_path, _inventory(), [done()], lambda args: BrowserStartHandoff("fixture")
    )
    bs.agent_session = session
    session.surface = bs.surface
    assert previous.status == "running" and bs.active_job is previous  # session busy => queued

    entered, release = threading.Event(), threading.Event()
    original_with_job = bs.surface.with_job

    def with_job(job_id):
        # First statement inside _run_session_turn, i.e. after job_turn read its collector.
        if job_id != previous.job_id:
            entered.set()
            assert release.wait(20)
        return original_with_job(job_id)

    monkeypatch.setattr(bs.surface, "with_job", with_job)

    def previous_turn_finishes_and_drains():
        with bridge._state_lock:
            stdio_bridge._mark_job_completed(previous, status="completed", exit_code=0)
            stdio_bridge._reconcile_session_job_state(bs)
        bridge._drain_prompt_queue(bs)

    drainer = threading.Thread(target=previous_turn_finishes_and_drains)
    real_chat_send = bridge._chat_send

    def chat_send(request, **kwargs):
        result, start = real_chat_send(request, **kwargs)
        assert result["status"] == "queued" and result["created"] is True
        if drain_before_ack:
            drainer.start()
            assert entered.wait(20)
        return result, start

    monkeypatch.setattr(bridge, "_chat_send", chat_send)
    wire = {
        "protocol_version": "1",
        "id": "send",
        "method": "chat.send",
        "params": {"session_id": bs.session_id, "message": "Say hello.", "idempotency_key": "race"},
        "computer_use_attribution": {"version": 1, "context": TURN},
    }
    bridge.process_line(json.dumps(wire))
    reply = next(x for x in _lines(out) if x.get("id") == "send")
    job = bridge._jobs[reply["result"]["job_id"]]
    if not drain_before_ack:
        drainer.start()
        assert entered.wait(20)
    release.set()
    deadline = time.monotonic() + 30
    while job.status not in stdio_bridge.FINAL_JOB_STATUSES and time.monotonic() < deadline:
        time.sleep(0.01)
    drainer.join(20)
    job.thread.join(20)
    assert not drainer.is_alive() and not job.thread.is_alive()
    session.close()
    events = [
        e
        for e in _lines(out)
        if e.get("type") == "computer_use_attribution" and e["job_id"] == job.job_id
    ]
    return reply, job, client, events


@pytest.mark.parametrize("drain_before_ack", [False, True])
def test_queued_send_ack_requires_collector_before_worker_entry(
    tmp_path, monkeypatch, drain_before_ack
):
    reply, job, client, events = _run(tmp_path, monkeypatch, drain_before_ack=drain_before_ack)
    assert reply["ok"] and job.status == "completed" and client.calls == 1
    if drain_before_ack:
        # The worker already captured None. Omit telemetry rather than claiming
        # complete coverage of a real model call that was never measured.
        assert "computer_use_attribution" not in reply
        assert events == []
        assert job.computer_use_attribution is None
    else:
        assert reply["computer_use_attribution"]["accepted"] is True
        assert [e["payload"]["record"]["kind"] for e in events] == ["span", "span", "summary"]
        summary = events[-1]["payload"]["record"]
        assert summary["spans"][6]["started"] == summary["spans"][6]["ended"] == 1
        assert summary["transportUnknownCalls"] == 1 and summary["coverage"] == "partial"
