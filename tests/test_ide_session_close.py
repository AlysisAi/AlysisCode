"""A retained engine must not retain a retired computer-control session."""

from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest

from alysis_code.ide.artifacts import ArtifactStore
from alysis_code.ide.event_stream import EventContext, EventSequencer, ProtocolEventSurface
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.prompt_queue import DurablePromptQueue, PromptQueueError, PromptState
from alysis_code.ide.stdio_bridge import (
    BridgeJob,
    BridgeSession,
    BridgeSessionCleanupError,
    StdioBridge,
)


def harness(tmp_path, *, sensitive=True, cleanup=lambda: None, grace=2.0):
    out = io.StringIO()
    bridge = StdioBridge(
        stdout=out,
        shutdown_timeout_seconds=grace,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    session = BridgeSession(
        session_id="chat-one",
        root=tmp_path,
        mode="auto",
        surface=ProtocolEventSurface(
            context=EventContext(session_id="chat-one"),
            emit=bridge._record_and_write_event,
            sequencer=EventSequencer(),
        ),
        agent_session=SimpleNamespace(close=cleanup),
        artifact_store=ArtifactStore([]),
        sensitive_media=sensitive,
    )
    bridge._sessions[session.session_id] = session

    def request(method, **params):
        bridge.process_line(
            json.dumps({"protocol_version": "1", "id": "check", "method": method, "params": params})
            + "\n"
        )
        return [
            json.loads(line)
            for line in out.getvalue().splitlines()
            if json.loads(line).get("id") == "check"
        ][-1]

    return bridge, session, request


@pytest.mark.parametrize("sensitive", [True, False])
def test_close_forgets_the_session_jobs_and_approval_authority(tmp_path, sensitive):
    closed = []
    bridge, session, request = harness(
        tmp_path, sensitive=sensitive, cleanup=lambda: closed.append(True)
    )
    job = BridgeJob(
        job_id="old-job", session_id=session.session_id, created_at="now", status="completed"
    )
    bridge._jobs[job.job_id] = job
    session.last_job = job
    session.resolved_computer_use_ids.add("old-request")
    assert "session.close" in capabilities_payload()["methods"]
    reply = request("session.close", session_id=session.session_id)
    assert reply["result"] == {"session_id": "chat-one", "status": "closed", "worker_settled": True}
    assert closed == [True]
    assert session.closed
    assert session.session_id not in bridge._sessions
    assert job.job_id not in bridge._jobs
    assert request("job.status", job_id=job.job_id)["error"]["code"] == "job_not_found"
    assert request("chat.send", session_id=session.session_id, message="stale")["ok"] is False


def test_close_waits_for_terminal_workers_before_returning_process_to_pool(tmp_path):
    finished = threading.Event()
    cleanup_saw_finished = []
    bridge, session, request = harness(
        tmp_path, cleanup=lambda: cleanup_saw_finished.append(finished.is_set())
    )
    job = BridgeJob(
        job_id="finishing", session_id=session.session_id, created_at="now", status="completed"
    )

    def worker():
        # Model the finally block that runs after the terminal event.
        while not session.close_when_idle:
            finished.wait(0.001)
        session.resolved_computer_use_ids.add("last-worker-write")
        finished.set()

    job.thread = threading.Thread(target=worker)
    session.last_job = job
    bridge._jobs[job.job_id] = job
    job.thread.start()
    try:
        assert request("session.close", session_id=session.session_id)["ok"] is True
        assert not job.thread.is_alive()
        assert cleanup_saw_finished == [True]
    finally:
        session.close_when_idle = True
        job.thread.join(2)


@pytest.mark.parametrize("slot", ["active_job", "last_job"])
def test_shutdown_joins_terminal_worker_before_session_cleanup(tmp_path, monkeypatch, slot):
    release = threading.Event()
    finished = threading.Event()
    cleanup_saw_finished = []
    bridge, session, _ = harness(
        tmp_path, cleanup=lambda: cleanup_saw_finished.append(finished.is_set())
    )
    job = BridgeJob(
        job_id="finishing", session_id=session.session_id, created_at="now", status="completed"
    )

    def worker():
        release.wait()
        finished.set()

    job.thread = threading.Thread(target=worker)
    setattr(session, slot, job)
    original_join = job.thread.join

    def join(*, timeout):
        assert timeout > 0
        release.set()
        original_join(timeout)

    monkeypatch.setattr(job.thread, "join", join)
    job.thread.start()
    try:
        bridge.close()
        assert cleanup_saw_finished == [True]
        assert session.closed
        assert not job.cancellation_event.is_set()
    finally:
        release.set()
        original_join(2)


def test_shutdown_keeps_cleanup_blocked_when_terminal_worker_outlives_deadline(tmp_path):
    release = threading.Event()
    closed = []
    bridge, session, _ = harness(tmp_path, grace=0, cleanup=lambda: closed.append(True))
    job = BridgeJob(
        job_id="stuck-finally", session_id=session.session_id, created_at="now", status="completed"
    )
    job.thread = threading.Thread(target=release.wait)
    session.last_job = job
    job.thread.start()
    try:
        with pytest.raises(BridgeSessionCleanupError):
            bridge.close()
        assert not closed and not session.closed
    finally:
        release.set()
        job.thread.join(2)


def test_close_refuses_live_jobs_without_cancelling_them(tmp_path):
    bridge, session, request = harness(tmp_path)
    job = BridgeJob(
        job_id="active", session_id=session.session_id, created_at="now", status="running"
    )
    session.active_job = job
    bridge._jobs[job.job_id] = job
    assert (
        request("session.close", session_id=session.session_id)["error"]["code"] == "session_busy"
    )
    assert not job.cancellation_event.is_set()
    assert not session.closed


def test_close_timeout_never_confirms_reuse(tmp_path):
    release = threading.Event()
    bridge, session, request = harness(tmp_path, grace=0)
    job = BridgeJob(
        job_id="stuck-finally", session_id=session.session_id, created_at="now", status="completed"
    )
    job.thread = threading.Thread(target=release.wait)
    session.last_job = job
    job.thread.start()
    try:
        assert (
            request("session.close", session_id=session.session_id)["error"]["code"]
            == "session_busy"
        )
        assert session.close_when_idle and not session.closed
    finally:
        release.set()
        job.thread.join(2)


def test_close_cleanup_failure_never_confirms_reuse(tmp_path):
    def fail():
        raise RuntimeError("cleanup failed")

    bridge, session, request = harness(tmp_path, cleanup=fail)
    assert (
        request("session.close", session_id=session.session_id)["error"]["code"]
        == "session_cleanup_incomplete"
    )
    assert not session.closed
    assert session.session_id in bridge._sessions


def test_close_cancels_queued_followups_before_the_chat_id_can_be_reused(tmp_path):
    bridge, session, request = harness(tmp_path, sensitive=False)
    queued = bridge._prompt_queue.enqueue(
        session_id=session.session_id,
        idempotency_key="old-followup",
        payload={"message": "old task follow-up"},
    )
    assert request("session.close", session_id=session.session_id)["ok"] is True
    item = bridge._prompt_queue.get(session_id=session.session_id, prompt_id=queued.item.prompt_id)
    assert item is not None and item.state == PromptState.CANCELLED


def test_close_queue_cleanup_failure_never_confirms_reuse(tmp_path, monkeypatch):
    bridge, session, request = harness(tmp_path)

    def fail(**_kwargs):
        raise PromptQueueError("queue unavailable")

    monkeypatch.setattr(bridge._prompt_queue, "cancel_pending_for_session", fail)
    assert (
        request("session.close", session_id=session.session_id)["error"]["code"]
        == "prompt_queue_error"
    )
    assert session.close_when_idle and not session.closed
