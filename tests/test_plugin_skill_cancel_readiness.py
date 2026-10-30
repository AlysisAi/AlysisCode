"""Stop owns its active turn; it must not retire another queued selection."""

import json
import threading
import time

import pytest

from tests.test_custom_plugins import root as root
from tests.test_ide_host_actions import _request, _respond, _response_for
from tests.test_plugin_skill_admission import create, finish, send, wait_for
from tests.test_plugin_skill_admission import setup as setup


def admission_for(bridge, identifier):
    end = time.monotonic() + 2
    while time.monotonic() < end:
        events = [json.loads(line) for line in bridge.stdout.getvalue().splitlines()]
        event = next(
            (
                e
                for e in events
                if e.get("type") == "host_action_requested"
                and e["payload"]["arguments"].get("admission_id") == identifier
            ),
            None,
        )
        if event is not None:
            return event
        time.sleep(0.005)
    raise AssertionError("Queued selection never requested admission")


@pytest.mark.parametrize("first", ["stop", "refuse", "close"])
def test_stop_of_a_preserves_acknowledged_selection_b(setup, first):
    bridge, _, _, selected, calls = setup
    bridge._host_action_timeout_seconds = 30
    sid = create(setup)
    ack_a = send(setup, sid)
    event_a = wait_for(bridge, "host_action_requested")
    selected_b = {**selected, "admission_id": "c" * 32}
    bridge.process_line(
        _request(
            "chat.send",
            dict(session_id=sid, message="Second selected turn.", explicit_plugin_skill=selected_b),
            "send-b",
        )
    )
    ack_b = _response_for(bridge.stdout, "send-b")
    pid_b = ack_b["result"]["prompt_id"]
    assert bridge._p2_readiness[pid_b].ready
    if first == "refuse":
        _respond(bridge, event_a, request_id="refuse-a", result={"admit": False, "reason": "stale"})
    else:
        bridge.process_line(
            _request(
                "session.cancel", dict(session_id=sid, close_when_idle=first == "close"), "stop"
            )
        )
        assert _response_for(bridge.stdout, "stop")["ok"]
    assert finish(bridge, ack_a).status == ("failed" if first == "refuse" else "cancelled")
    if first == "close":
        assert pid_b not in bridge._p2_readiness
        assert calls == []
        return
    event_b = admission_for(bridge, selected_b["admission_id"])
    _respond(bridge, event_b, request_id="admit-b", result={"admit": True})
    assert finish(bridge, ack_b).status == "completed"
    assert len(calls) == 1 and not bridge._p2_readiness


def hold_ack(bridge, monkeypatch):
    waiting = threading.Event()
    enqueue = bridge._p2_enqueue
    reply = bridge._p2_reply_written

    def observed_enqueue(*args):
        queued = enqueue(*args)
        ready = bridge._p2_readiness[queued.item.prompt_id]
        wait = ready.event.wait

        def observed_wait(timeout):
            waiting.set()
            return wait(timeout)

        ready.event.wait = observed_wait
        return queued

    monkeypatch.setattr(bridge, "_p2_enqueue", observed_enqueue)
    monkeypatch.setattr(
        bridge,
        "_p2_reply_written",
        lambda request, result: None if request.id == "send" else reply(request, result),
    )
    return waiting


@pytest.mark.parametrize("close", [False, True])
def test_stop_during_held_ack_wins_before_any_retirement(setup, monkeypatch, close):
    bridge, _, _, _, calls = setup
    bridge._host_action_timeout_seconds = 30
    sid = create(setup)
    waiting = hold_ack(bridge, monkeypatch)
    ack = send(setup, sid)
    assert waiting.wait(2)
    job = bridge._jobs[ack["result"]["job_id"]]
    retire = bridge._p2_retire

    def widen_retirement_gap(**kwargs):
        retire(**kwargs)
        if kwargs.get("session_id") == sid and threading.current_thread() is not job.thread:
            job.thread.join(0.5)

    monkeypatch.setattr(bridge, "_p2_retire", widen_retirement_gap)
    try:
        bridge.process_line(
            _request("session.cancel", dict(session_id=sid, close_when_idle=close), "stop")
        )
        job.thread.join(2)
        assert not job.thread.is_alive(), "Stop must not wait for the 30-second host timeout"
        assert job.status == "cancelled" and job.exit_code == 130
        assert "error_code" not in job.result and calls == []
    finally:
        job.cancellation_event.set()
        for ready in list(bridge._p2_readiness.values()):
            ready.failed = True
            ready.event.set()
        job.thread.join(2)
