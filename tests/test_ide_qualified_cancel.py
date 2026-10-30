"""LR33 on ordinary main: real bridge/queue and inert effect boundaries."""

from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest

from alysis_code.ide import stdio_bridge as sb
from alysis_code.ide.artifacts import ArtifactStore
from alysis_code.ide.event_stream import EventContext, EventSequencer, ProtocolEventSurface
from alysis_code.ide.prompt_queue import DurablePromptQueue, PromptState
from alysis_code.ide.protocol import ProtocolError, ProtocolRequest

ForgeRequestState = sb.ForgeRequestState


def harness(tmp_path):
    output = io.StringIO()
    closed = []
    bridge = sb.StdioBridge(
        stdout=output, prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3")
    )
    session = sb.BridgeSession(
        session_id="chat-one",
        root=tmp_path,
        mode="auto",
        surface=ProtocolEventSurface(
            context=EventContext(session_id="chat-one"),
            emit=bridge._record_and_write_event,
            sequencer=EventSequencer(),
        ),
        agent_session=SimpleNamespace(close=lambda: closed.append(True), messages=[]),
        artifact_store=ArtifactStore([]),
    )
    bridge._sessions[session.session_id] = session

    def call(**params):
        return bridge._session_cancel(
            ProtocolRequest(
                id="cancel",
                method="session.cancel",
                params={"session_id": session.session_id, **params},
                protocol_version="1",
            )
        )

    def wire(**params):
        bridge.process_line(
            json.dumps(
                {
                    "protocol_version": "1",
                    "id": "wire",
                    "method": "session.cancel",
                    "params": {"session_id": session.session_id, **params},
                }
            )
            + "\n"
        )
        return [
            json.loads(line)
            for line in output.getvalue().splitlines()
            if json.loads(line).get("id") == "wire"
        ][-1]

    return bridge, session, closed, call, wire


def job(bridge, session, name="A", status="running", *, active=True):
    value = sb.BridgeJob(
        job_id=name, session_id=session.session_id, created_at="now", status=status
    )
    bridge._jobs[name] = value
    if active:
        session.active_job = value
    return value


def forbidden(*args, **kwargs):
    raise AssertionError("read-only qualified rejection reached an effect")


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("closing", [False, True])
def test_terminal_A_is_read_only_even_with_B_and_close(tmp_path, monkeypatch, status, closing):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s, status=status, active=False)
    current = job(b, s, "B")
    for name in [
        "_cancel_pending_approvals",
        "_cancel_pending_host_actions",
        "_cancel_pending_computer_use",
        "_close_session",
        "_drain_prompt_queue",
        "_schedule_session_close_after_job",
    ]:
        monkeypatch.setattr(b, name, forbidden)
    monkeypatch.setattr(b._prompt_queue, "cancel_pending_for_session", forbidden)
    monkeypatch.setattr(sb, "_reconcile_session_job_state", forbidden)
    result = wire(job_id=a.job_id, close_when_idle=closing)
    assert result["ok"] is True
    assert result["result"]["job_id"] == "A" and result["result"]["status"] == status
    assert result["result"]["close_when_idle"] is False
    assert result["result"]["queued_prompts_cancelled"] == 0
    assert s.active_job is current and not current.cancellation_event.is_set()
    assert not s.close_when_idle and not s.cancellation_transitions and closed == []


@pytest.mark.parametrize("value", [None, "", " ", 0, False, [], {}, "bad id", "A\n", "a" * 257])
def test_present_invalid_never_chooses_legacy(tmp_path, monkeypatch, value):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    monkeypatch.setattr(b, "_close_session", forbidden)
    result = wire(job_id=value, close_when_idle=True)
    assert result["error"]["code"] == "invalid_field"
    assert not a.cancellation_event.is_set() and not s.close_when_idle and closed == []


@pytest.mark.parametrize("kind", ["unknown", "pruned", "foreign", "non_current", "no_active"])
def test_exact_not_found_before_effects(tmp_path, monkeypatch, kind):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    target = "missing"
    if kind in {"foreign", "non_current", "no_active"}:
        target = "C"
        other = job(b, s, target, active=False)
        if kind == "foreign":
            other.session_id = "another-session"
        if kind == "no_active":
            s.active_job = None
    if kind == "pruned":
        b._jobs.pop("A")
        target = "A"
    for name in [
        "_cancel_pending_approvals",
        "_cancel_pending_host_actions",
        "_cancel_pending_computer_use",
        "_close_session",
    ]:
        monkeypatch.setattr(b, name, forbidden)
    monkeypatch.setattr(b._prompt_queue, "cancel_pending_for_session", forbidden)
    monkeypatch.setattr(sb, "_reconcile_session_job_state", forbidden)
    result = wire(job_id=target, close_when_idle=True)
    assert result["error"]["code"] == "job_not_found"
    assert result["error"]["message"] == "Job was not found."
    assert not a.cancellation_event.is_set() and not s.close_when_idle and closed == []


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("cancellable", [False, True])
def test_current_and_legacy_active_preserve_policy(tmp_path, qualified, cancellable):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    a.cancellable = cancellable
    args = {"job_id": "A"} if qualified else {}
    result = call(**args)
    assert result["status"] == ("cancellation_requested" if cancellable else "non_cancellable")
    assert result["job_id"] == "A" and a.cancellation_event.is_set() == cancellable
    assert closed == [] and s.cancellation_transitions == 0
    assert call(**args)["status"] == result["status"]


def test_legacy_absence_closes_idle_but_qualified_does_not(tmp_path):
    b, s, closed, call, wire = harness(tmp_path)
    result = wire(job_id="A")
    assert result["error"]["code"] == "job_not_found" and closed == []
    assert call()["status"] == "closed" and closed == [True] and s.closed


@pytest.mark.parametrize("qualified", [False, True])
def test_sensitive_reason_uses_captured_binding_before_revocation(tmp_path, monkeypatch, qualified):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    s.sensitive_media = True
    binding = object()
    a.host_image_binding = binding
    seen = []

    def redact(owner, reason, *, host_image_binding):
        seen.append((owner, reason, host_image_binding))
        return "safe reason"

    monkeypatch.setattr(sb, "_redact_bridge_error", redact)
    result = call(reason="private path", **({"job_id": "A"} if qualified else {}))
    assert seen == [(s, "private path", binding)]
    assert a.cancellation_reason == "safe reason" and a.host_image_binding is None
    assert result["status"] == "cancellation_requested"


def test_callbacks_hold_A_vacancy_across_all_real_admission_joins(tmp_path, monkeypatch):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    entered = threading.Event()
    release = threading.Event()
    out = []
    effects = []
    original = b._cancel_pending_approvals

    def delayed(owner, reason):
        original(owner, reason)
        effects.append(("approval", owner.active_job.job_id))
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(b, "_cancel_pending_approvals", delayed)
    original_host = b._cancel_pending_host_actions

    def host(owner, reason):
        effects.append(("host", owner.active_job.job_id))
        original_host(owner, reason)

    monkeypatch.setattr(b, "_cancel_pending_host_actions", host)
    claim_next = b._prompt_queue.claim_next
    t = threading.Thread(target=lambda: out.append(call(job_id="A")))
    t.start()
    assert entered.wait(5)
    try:
        with b._state_lock:
            sb._mark_job_completed(a, status="cancelled", exit_code=130)
            sb._reconcile_session_job_state(s)
            assert s.active_job is a
            with pytest.raises(ProtocolError, match="still settling"):
                b._register_job_locked(
                    s, sb.BridgeJob(job_id="B", session_id=s.session_id, created_at="now")
                )
            with pytest.raises(ProtocolError, match="still settling"):
                b._attach_durable_forge_job_locked(
                    s, SimpleNamespace(state=ForgeRequestState.RUNNING)
                )
        monkeypatch.setattr(b._prompt_queue, "claim_next", forbidden)
        b._drain_prompt_queue(s)
        for method in ["_session_clear", "_session_resume"]:
            with pytest.raises(ProtocolError, match="still settling"):
                getattr(b, method)(
                    ProtocolRequest(
                        id="other",
                        method=method[1:].replace("_", "."),
                        params={"session_id": s.session_id},
                        protocol_version="1",
                    )
                )
        with pytest.raises(sb.BridgeSessionCleanupError):
            b._close_session(s)
        assert call(job_id="A", close_when_idle=True)["status"] == "cancelled"
        assert not s.close_when_idle
        assert s.cancellation_transitions == 1 and "B" not in b._jobs
    finally:
        monkeypatch.setattr(b._prompt_queue, "claim_next", claim_next)
        release.set()
        t.join(5)
    assert not t.is_alive() and out[0]["job_id"] == "A"
    assert effects == [("approval", "A"), ("host", "A")]
    assert not s.cancellation_transitions and s.active_job is None
    with b._state_lock:
        b._register_job_locked(
            s, sb.BridgeJob(job_id="B", session_id=s.session_id, created_at="now")
        )
    assert s.active_job.job_id == "B" and not s.active_job.cancellation_event.is_set()


def test_callback_uncertainty_retains_admission_fence(tmp_path, monkeypatch):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    error = RuntimeError("original callback failure")

    def fail(*args):
        raise error

    monkeypatch.setattr(b, "_cancel_pending_host_actions", fail)
    with pytest.raises(RuntimeError) as caught:
        call(job_id="A")
    assert caught.value is error and s.cancellation_transitions == 1 and s.cancellation_uncertain
    with b._state_lock:
        sb._mark_job_completed(a, status="cancelled", exit_code=130)
        sb._reconcile_session_job_state(s)
        with pytest.raises(ProtocolError):
            b._register_job_locked(
                s, sb.BridgeJob(job_id="B", session_id=s.session_id, created_at="now")
            )
    assert s.active_job is a and call(job_id="A")["status"] == "cancelled"
    with pytest.raises(sb.BridgeSessionCleanupError):
        b._close_session(s)
    assert closed == []


@pytest.mark.parametrize("method", ["clear", "resume"])
def test_cancel_cannot_enter_an_out_of_lock_reset(tmp_path, monkeypatch, method):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    entered = threading.Event()
    release = threading.Event()
    out = []

    def inner(request):
        entered.set()
        assert release.wait(5)
        return {"done": True}

    monkeypatch.setattr(b, f"_session_{method}_unfenced", inner)
    request = ProtocolRequest(
        id="reset",
        method=f"session.{method}",
        params={"session_id": s.session_id},
        protocol_version="1",
    )
    t = threading.Thread(target=lambda: out.append(getattr(b, f"_session_{method}")(request)))
    t.start()
    assert entered.wait(5)
    try:
        result = wire(job_id="A", close_when_idle=True)
        assert result["error"]["code"] == "session_busy"
        assert not a.cancellation_event.is_set() and not s.close_when_idle
    finally:
        release.set()
        t.join(5)
    assert out == [{"done": True}] and not s.mutation_in_progress


def test_close_when_idle_cancels_already_admitted_queue_and_blocks_replacement(
    tmp_path, monkeypatch
):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    pending = b._prompt_queue.enqueue(
        session_id=s.session_id, idempotency_key="B", payload={"message": "B"}
    )
    schedules = []
    monkeypatch.setattr(
        b,
        "_schedule_session_close_after_job",
        lambda owner, target: schedules.append((owner, target)),
    )
    result = call(job_id="A", close_when_idle=True)
    assert result["queued_prompts_cancelled"] == 1
    assert (
        b._prompt_queue.get(session_id=s.session_id, prompt_id=pending.item.prompt_id).state
        is PromptState.CANCELLED
    )
    assert schedules == [(s, a)] and s.close_when_idle
    with b._state_lock:
        with pytest.raises(ProtocolError):
            b._register_job_locked(
                s, sb.BridgeJob(job_id="B", session_id=s.session_id, created_at="now")
            )
    assert closed == []


def test_enqueue_admitted_before_close_is_in_the_original_cancel_cut(tmp_path, monkeypatch):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    entered = threading.Event()
    release = threading.Event()
    enqueued = []
    cancelled = []
    errors = []
    original = b._prompt_queue.enqueue

    def paused(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(b._prompt_queue, "enqueue", paused)
    schedules = []
    monkeypatch.setattr(
        b,
        "_schedule_session_close_after_job",
        lambda owner, target: schedules.append((owner, target)),
    )

    def enqueue():
        try:
            response, start = b._chat_send(
                ProtocolRequest(
                    id="B",
                    method="chat.send",
                    params={"session_id": s.session_id, "message": "B", "idempotency_key": "B"},
                    protocol_version="1",
                )
            )
            enqueued.append(response)
            start()
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=enqueue)
    t.start()
    assert entered.wait(5)
    admitted = threading.Event()
    request_cancel = sb._request_job_cancellation

    def cancel_admitted(target, reason):
        result = request_cancel(target, reason)
        admitted.set()
        return result

    monkeypatch.setattr(sb, "_request_job_cancellation", cancel_admitted)
    c = threading.Thread(target=lambda: cancelled.append(call(job_id="A", close_when_idle=True)))
    c.start()
    assert admitted.wait(5)
    try:
        assert s.cancellation_transitions == 1 and s.close_when_idle
        assert not cancelled
    finally:
        release.set()
        t.join(5)
        c.join(5)
    assert not errors and not t.is_alive() and not c.is_alive()
    assert cancelled[0]["queued_prompts_cancelled"] == 1 and schedules == [(s, a)]
    pending = b._prompt_queue.get(session_id=s.session_id, prompt_id=enqueued[0]["job_id"])
    assert pending.state is PromptState.CANCELLED
    assert s.active_job is a and b._jobs[enqueued[0]["job_id"]].thread is None


def test_completed_worker_retains_admission_until_its_actual_finally_returns(tmp_path):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    published = threading.Event()
    release = threading.Event()
    worker_ids = []

    def worker():
        worker_ids.append(threading.get_ident())
        with b._state_lock:
            sb._mark_job_completed(a, status="completed", exit_code=0)
            sb._reconcile_session_job_state(s)
        published.set()
        assert release.wait(5)

    thread = threading.Thread(target=worker)
    assert b._start_job_thread(s, a, thread)
    assert published.wait(5)
    try:
        assert a.thread is thread and a.worker_reserved and s.active_job is a
        assert call(job_id="A", close_when_idle=True)["status"] == "completed"
        assert not s.close_when_idle and closed == []
        with b._state_lock:
            with pytest.raises(ProtocolError):
                b._register_job_locked(
                    s, sb.BridgeJob(job_id="B", session_id=s.session_id, created_at="now")
                )
        b._drain_prompt_queue(s)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not a.worker_reserved and s.active_job is None
    assert worker_ids == [thread.ident]


def test_close_requested_before_worker_start_rejoins_the_original_worker(tmp_path):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    result = call(job_id="A", close_when_idle=True)
    first = s.close_worker
    if first is not None:
        first.join(5)
    assert result["status"] == "cancellation_requested" and closed == []

    def worker():
        assert a.cancellation_event.is_set()
        with b._state_lock:
            sb._mark_job_completed(a, status="cancelled", exit_code=130)

    original = threading.Thread(target=worker)
    assert b._start_job_thread(s, a, original)
    original.join(5)
    final = s.close_worker
    if final is not None:
        final.join(5)
    assert not original.is_alive() and a.thread is original
    assert s.closed and closed == [True] and not a.worker_reserved


def test_protocol_documentation_records_optional_not_legacy_fallback():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    entry = json.loads((root / "docs/generated/ide_protocol_methods.json").read_text())["methods"][
        "session.cancel"
    ]
    assert entry["required_params"] == ["session_id"]
    assert entry["optional_params"] == ["job_id", "reason", "close_when_idle"]
    assert entry["qualified_job_id"]["retained_terminal"] == "read_only_existing_summary"
    assert entry["qualified_job_id"]["present_invalid"] == "invalid_field"


@pytest.mark.parametrize("checkpoint", ["provider", "persistence"])
def test_real_turn_serializes_stop_with_final_persistence_decision(tmp_path, checkpoint):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    a.prompt_id = "A"
    entered = threading.Event()
    release = threading.Event()
    events = []

    def append(kind, payload):
        events.append(kind)
        if checkpoint == "persistence" and kind == "ide_prompt_completed":
            entered.set()
            assert release.wait(5)

    def run_turn(message, **kwargs):
        if checkpoint == "provider":
            entered.set()
            assert release.wait(5)
        return 0

    s.agent_session.run_turn = run_turn
    s.agent_session.store = SimpleNamespace(append=append, events_snapshot=lambda: [])
    s.checkpoint_guard = object()  # No tool is dispatched: no baseline is needed.
    thread = threading.Thread(target=b._run_session_turn, args=(s, a, "hello", [], []))
    assert b._start_job_thread(s, a, thread)
    assert entered.wait(5)
    try:
        result = call(job_id="A")
        assert result["status"] == (
            "cancellation_requested" if checkpoint == "provider" else "non_cancellable"
        )
        assert a.worker_reserved and s.active_job is a
        assert a.cancellation_event.is_set() == (checkpoint == "provider")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not a.worker_reserved
    assert a.status == ("cancelled" if checkpoint == "provider" else "completed")
    assert ("ide_prompt_completed" in events) == (checkpoint == "persistence")


def test_close_in_progress_does_not_admit_new_qualified_effects(tmp_path):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    with s.close_lock:
        result = wire(job_id="A", close_when_idle=True)
    assert result["error"]["code"] == "session_busy"
    assert not a.cancellation_event.is_set() and not s.close_when_idle and closed == []


def test_successful_resume_gate_releases_a_real_queued_turn(tmp_path, monkeypatch):
    b, s, closed, call, wire = harness(tmp_path)
    ran = []
    s.agent_session.run_turn = lambda message, **kwargs: ran.append(message) or 0
    pending = b._prompt_queue.enqueue(
        session_id=s.session_id,
        idempotency_key="B",
        payload={"message": "B", "image_paths": [], "basket_image_paths": [], "context_prompt": ""},
    )

    def restore(request):
        assert s.mutation_in_progress
        b._drain_prompt_queue(s)
        assert ran == []
        return {"restored": True}

    monkeypatch.setattr(b, "_session_resume_unfenced", restore)
    request = ProtocolRequest(
        id="resume",
        method="session.resume",
        params={"session_id": s.session_id},
        protocol_version="1",
    )
    assert b._session_resume(request) == {"restored": True}
    target = b._jobs[pending.item.prompt_id]
    assert target.thread is not None
    target.thread.join(5)
    assert not target.thread.is_alive() and ran == ["B"] and target.status == "completed"
    assert not s.mutation_in_progress


def test_clear_serializes_existing_queue_cancellation_before_releasing_admission(tmp_path):
    b, s, closed, call, wire = harness(tmp_path)
    s.agent_session.messages[:] = ["original"]
    pending = b._prompt_queue.enqueue(
        session_id=s.session_id, idempotency_key="B", payload={"message": "B"}
    )
    result = b._session_clear(
        ProtocolRequest(
            id="clear",
            method="session.clear",
            params={"session_id": s.session_id},
            protocol_version="1",
        )
    )
    assert result["queued_prompts_cancelled"] == 1 and result["messages_after"] == 0
    assert (
        b._prompt_queue.get(session_id=s.session_id, prompt_id=pending.item.prompt_id).state
        is PromptState.CANCELLED
    )
    assert s.active_job is None and not s.mutation_in_progress


def test_running_queue_recovery_cannot_replace_an_unfinished_original_worker(tmp_path):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    pending = b._prompt_queue.enqueue(
        session_id=s.session_id, idempotency_key="B", payload={"message": "B"}
    )
    lease = b._prompt_queue.claim_next(
        session_id=s.session_id, owner_id="other-owner", lease_seconds=30
    )
    assert lease.item.prompt_id == pending.item.prompt_id
    published = threading.Event()
    release = threading.Event()

    def worker():
        with b._state_lock:
            sb._mark_job_completed(a, status="completed", exit_code=0)
        published.set()
        assert release.wait(5)

    thread = threading.Thread(target=worker)
    assert b._start_job_thread(s, a, thread)
    assert published.wait(5)
    try:
        b._resume_prompt_queue(s)
        assert s.active_job is a and pending.item.prompt_id not in b._jobs
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not a.worker_reserved


@pytest.mark.parametrize("closing", [False, True])
def test_legacy_close_refuses_terminal_original_worker_custody(tmp_path, closing):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    published = threading.Event()
    release = threading.Event()

    def worker():
        with b._state_lock:
            sb._mark_job_completed(a, status="completed", exit_code=0)
        published.set()
        assert release.wait(5)

    original = threading.Thread(target=worker)
    assert b._start_job_thread(s, a, original) and published.wait(5)
    try:
        result = wire(close_when_idle=closing)
        assert result["ok"] is False and result["error"]["code"] == "session_busy"
        assert original.is_alive() and a.worker_reserved and closed == [] and not s.closed
        with pytest.raises(sb.BridgeSessionCleanupError, match="worker"):
            b._close_session(s, cancellation_owner=True)
        assert closed == []
    finally:
        release.set()
        original.join(5)
    assert not original.is_alive() and not a.worker_reserved
    assert call(close_when_idle=closing)["status"] == "closed"
    assert closed == [True] and s.closed


@pytest.mark.parametrize("failure", ["construction", "start"])
def test_prestart_close_known_failure_at_actual_review_entry(tmp_path, monkeypatch, failure):
    b, s, closed, call, wire = harness(tmp_path)
    result, start = b._code_review_start(
        ProtocolRequest(
            id="start",
            method="code.review.start",
            params={"session_id": s.session_id, "workspace_trusted": True},
            protocol_version="1",
        )
    )
    a = b._jobs[result["job_id"]]
    assert call(job_id=a.job_id, close_when_idle=True)["status"] == "cancellation_requested"
    original_thread = threading.Thread
    constructed = []
    joins = []
    original_error = RuntimeError("can't start new thread")

    def factory(*args, **kwargs):
        if kwargs.get("target") == b._run_code_review_job:
            if failure == "construction":
                raise original_error
            t = original_thread(*args, **kwargs)
            constructed.append(t)

            def join(*args, **kwargs):
                joins.append(True)
                raise AssertionError("unstarted join")

            t.join = join
            return t
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(threading, "Thread", factory)
    if failure == "start":
        start_name = (
            "_start_joinable_thread"
            if hasattr(threading, "_start_joinable_thread")
            else "_start_new_thread"
        )
        native_start = getattr(threading, start_name)

        def fail_native(function, *args, **kwargs):
            target = getattr(function, "__self__", None)
            if target in constructed:
                raise original_error
            return native_start(function, *args, **kwargs)

        monkeypatch.setattr(threading, start_name, fail_native)
    start()
    observer = s.close_worker
    if observer is not None:
        observer.join(5)
    assert a.status == "failed" and a.error == "job_start_failed: can't start new thread"
    assert not a.worker_reserved and not s.cancellation_uncertain
    assert not joins and all(t.ident is None for t in constructed)
    assert s.closed and closed == [True] and s.active_job is None


def test_last_transition_hands_close_back_before_observer_retires(tmp_path, monkeypatch):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    finish = threading.Event()
    blocked = threading.Event()
    release_callback = threading.Event()
    leaving = threading.Event()
    retire = threading.Event()
    armed = set()
    gate_used = set()
    responses = []
    original_lock = b._state_lock

    class ExitGate:
        def __enter__(self):
            original_lock.acquire()
            return self

        def __exit__(self, *args):
            ident = threading.get_ident()
            pause = ident in armed and ident not in gate_used
            if pause:
                gate_used.add(ident)
            original_lock.release()
            if pause:
                leaving.set()
                assert retire.wait(5)

        def acquire(self, *args, **kwargs):
            return original_lock.acquire(*args, **kwargs)

        def release(self):
            return original_lock.release()

    b._state_lock = ExitGate()
    pending = sb._session_transition_pending

    def observe_pending(owner):
        result = pending(owner)
        if result and threading.current_thread() is owner.close_worker:
            armed.add(threading.get_ident())
        return result

    monkeypatch.setattr(sb, "_session_transition_pending", observe_pending)

    def worker():
        assert finish.wait(5)
        with b._state_lock:
            sb._mark_job_completed(a, status="completed", exit_code=0)

    original = threading.Thread(target=worker)
    assert b._start_job_thread(s, a, original)
    assert call(job_id="A", close_when_idle=True)["status"] == "cancellation_requested"
    old_observer = s.close_worker
    assert old_observer is not None
    real_cancel = b._cancel_pending_approvals

    def delay(*args):
        blocked.set()
        assert release_callback.wait(5)
        return real_cancel(*args)

    monkeypatch.setattr(b, "_cancel_pending_approvals", delay)
    second = threading.Thread(
        target=lambda: responses.append(call(job_id="A", close_when_idle=True))
    )
    second.start()
    assert blocked.wait(5)
    try:
        finish.set()
        original.join(5)
        assert not original.is_alive()
        assert leaving.wait(5) and old_observer.is_alive()
        release_callback.set()
        second.join(5)
        assert not second.is_alive()
        assert responses and not s.cancellation_transitions
        new_observer = s.close_worker
    finally:
        finish.set()
        release_callback.set()
        retire.set()
        original.join(5)
        second.join(5)
        old_observer.join(5)
    if new_observer is not None and new_observer is not old_observer:
        new_observer.join(5)
    assert s.closed and closed == [True] and not s.cancellation_uncertain
    assert not original.is_alive() and not old_observer.is_alive()


@pytest.mark.parametrize("started", [False, True])
def test_uncertain_start_never_uses_no_start_close_path(tmp_path, started):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    release = threading.Event()
    entered = threading.Event()
    attempts = []

    def worker():
        entered.set()
        assert release.wait(5)
        with b._state_lock:
            sb._mark_job_completed(a, status="completed", exit_code=0)

    original = threading.Thread(target=worker)
    real_start = original.start

    def uncertain():
        attempts.append(True)
        if started:
            real_start()
        raise RuntimeError("can't start new thread")

    original.start = uncertain
    assert call(job_id="A", close_when_idle=True)["status"] == "cancellation_requested"
    try:
        assert not b._start_job_thread(s, a, original)
        assert s.cancellation_uncertain and not a.worker_never_started
        assert a.error == "job_start_failed: can't start new thread"
        assert closed == [] and not s.closed and attempts == [True]
        b._schedule_session_close_after_job(s, a)
        assert s.close_worker is None
        with pytest.raises(sb.BridgeSessionCleanupError):
            b._close_session(s, cancellation_owner=True)
    finally:
        release.set()
        if started:
            original.join(5)
    assert closed == [] and s.cancellation_uncertain


def test_close_cleanup_error_is_one_consumed_attempt_not_a_retry_loop(tmp_path):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    failure = RuntimeError("retained cleanup failure")
    attempts = []

    def close():
        attempts.append(True)
        raise failure

    s.agent_session.close = close
    assert call(job_id="A", close_when_idle=True)["status"] == "cancellation_requested"

    def worker():
        with b._state_lock:
            sb._mark_job_completed(a, status="completed", exit_code=0)

    original = threading.Thread(target=worker)
    assert b._start_job_thread(s, a, original)
    original.join(5)
    observer = s.close_worker
    if observer is not None:
        observer.join(5)
    assert not s.closed and s.close_attempted and attempts == [True]
    assert (
        isinstance(s.close_error, sb.BridgeSessionCleanupError)
        and s.close_error.__cause__ is failure
    )
    for _ in range(3):
        b._schedule_session_close_after_job(s, a)
    assert s.close_worker is None and attempts == [True] and s.close_when_idle


def test_rejected_resume_keeps_original_worker_queue_wakeup(tmp_path, monkeypatch):
    from alysis_code.cli_impl.commands import chat_resume_helpers as resume

    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    a.prompt_id = "A"
    running = threading.Event()
    finish_a = threading.Event()
    resolving = threading.Event()
    finish_lookup = threading.Event()
    ran_b = threading.Event()
    ran = []
    errors = []

    def run_turn(message, **kwargs):
        ran.append(message)
        if message == "A":
            running.set()
            assert finish_a.wait(5)
        else:
            ran_b.set()
        return 0

    s.agent_session.run_turn = run_turn
    s.agent_session.store = SimpleNamespace(
        sessions_dir=tmp_path / "retained", append=lambda *args: None, events_snapshot=lambda: []
    )
    s.checkpoint_guard = object()
    worker = threading.Thread(target=b._run_session_turn, args=(s, a, "A", [], []))
    assert b._start_job_thread(s, a, worker)
    assert running.wait(5)
    queued = b._prompt_queue.enqueue(
        session_id=s.session_id,
        idempotency_key="B",
        payload={"message": "B", "image_paths": [], "basket_image_paths": [], "context_prompt": ""},
    )
    original_lookup = resume._resolve_chat_resume_session_path

    def held_lookup(**kwargs):
        resolving.set()
        assert finish_lookup.wait(5)
        return original_lookup(**kwargs)  # Real missing-file refusal, no mutation.

    monkeypatch.setattr(resume, "_resolve_chat_resume_session_path", held_lookup)
    request = ProtocolRequest(
        id="resume",
        method="session.resume",
        params={"session_id": s.session_id, "target_session_id": "missing-retained"},
        protocol_version="1",
    )

    def restore():
        try:
            b._session_resume(request)
        except BaseException as exc:
            errors.append(exc)

    request_thread = threading.Thread(target=restore)
    request_thread.start()
    try:
        assert resolving.wait(5)
        finish_a.set()
        worker.join(5)
        assert not worker.is_alive()
        finish_lookup.set()
        request_thread.join(5)
        assert not request_thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], ProtocolError)
        assert errors[0].code == "resume_session_not_found"
        assert not getattr(s, "mutation_in_progress", False)
        reached = ran_b.wait(1)
        queued_job = b._jobs.get(queued.item.prompt_id)
        if queued_job is not None and queued_job.thread is not None:
            queued_job.thread.join(5)
        item = b._prompt_queue.get(session_id=s.session_id, prompt_id=queued.item.prompt_id)
        print(
            "RESUME_FAILURE_FACTS "
            + json.dumps(
                {
                    "ran": ran,
                    "error": errors[0].code,
                    "mutation": getattr(s, "mutation_in_progress", False),
                    "queue_state": item.state.value,
                    "active_job": s.active_job.job_id if s.active_job else None,
                }
            )
        )
        assert reached, (
            "a pre-effect missing-history refusal must not erase B's original queue-drain wakeup"
        )
    finally:
        finish_a.set()
        finish_lookup.set()
        worker.join(5)
        request_thread.join(5)


def test_clear_pre_effect_busy_refusal_releases_original_queue_wakeup(tmp_path, monkeypatch):
    b, s, closed, call, wire = harness(tmp_path)
    a = job(b, s)
    a.prompt_id = "A"
    running = threading.Event()
    finish = threading.Event()
    refused = threading.Event()
    release = threading.Event()
    ran_b = threading.Event()
    ran = []
    errors = []
    original_errors = []

    def run_turn(message, **kwargs):
        ran.append(message)
        if message == "A":
            running.set()
            assert finish.wait(5)
        else:
            ran_b.set()
        return 0

    s.agent_session.run_turn = run_turn
    s.agent_session.store = SimpleNamespace(append=lambda *args: None, events_snapshot=lambda: [])
    s.checkpoint_guard = object()
    worker = threading.Thread(target=b._run_session_turn, args=(s, a, "A", [], []))
    assert b._start_job_thread(s, a, worker) and running.wait(5)
    item = b._prompt_queue.enqueue(
        session_id=s.session_id, idempotency_key="B", payload={"message": "B"}
    )
    real_clear = b._session_clear_unfenced

    def hold_actual_refusal(request):
        try:
            return real_clear(request)
        except ProtocolError as exc:
            original_errors.append(exc)
            refused.set()
            assert release.wait(5)
            raise

    monkeypatch.setattr(b, "_session_clear_unfenced", hold_actual_refusal)

    def clear():
        try:
            b._session_clear(
                ProtocolRequest(
                    id="clear",
                    method="session.clear",
                    params={"session_id": s.session_id},
                    protocol_version="1",
                )
            )
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=clear)
    caller.start()
    try:
        assert refused.wait(5)
        finish.set()
        worker.join(5)
        assert not worker.is_alive() and ran == ["A"]
        release.set()
        caller.join(5)
        assert not caller.is_alive()
        assert errors == original_errors and len(errors) == 1 and errors[0].code == "session_busy"
        assert ran_b.wait(5)
        next_job = b._jobs[item.item.prompt_id]
        next_job.thread.join(5)
        assert ran == ["A", "B"] and not s.mutation_in_progress and s.mutation_error is None
        assert (
            b._prompt_queue.get(session_id=s.session_id, prompt_id=item.item.prompt_id).state
            is PromptState.COMPLETED
        )
    finally:
        finish.set()
        release.set()
        worker.join(5)
        caller.join(5)


def test_invalid_resume_validation_refusal_releases_pending_queue(tmp_path):
    b, s, closed, call, wire = harness(tmp_path)
    ran = []
    s.agent_session.run_turn = lambda message, **kwargs: ran.append(message) or 0
    item = b._prompt_queue.enqueue(
        session_id=s.session_id, idempotency_key="B", payload={"message": "B"}
    )
    with pytest.raises(ProtocolError) as caught:
        b._session_resume(
            ProtocolRequest(
                id="resume",
                method="session.resume",
                params={"session_id": s.session_id, "target_session_id": "../bad"},
                protocol_version="1",
            )
        )
    assert caught.value.code == "invalid_session_id"
    target = b._jobs[item.item.prompt_id]
    target.thread.join(5)
    assert ran == ["B"] and not target.thread.is_alive() and not s.mutation_in_progress


def test_resume_error_after_checkpoint_effect_retains_original_error_and_queue_fence(
    tmp_path, monkeypatch
):
    b, s, closed, call, wire = harness(tmp_path)
    ran = []
    effects = []
    s.agent_session.run_turn = lambda message, **kwargs: ran.append(message) or 0
    retained = tmp_path / "retained"
    retained.mkdir()
    (retained / "old.jsonl").write_text("")
    s.agent_session.store = SimpleNamespace(sessions_dir=retained)
    pending = b._prompt_queue.enqueue(
        session_id=s.session_id, idempotency_key="B", payload={"message": "B"}
    )
    failure = sb.ChangeLedgerError("uncertain adoption")

    def adopt(*args):
        effects.append(args)
        s.agent_session.messages.append("partial checkpoint context")
        raise failure

    monkeypatch.setattr(
        b, "_change_ledger_for_session", lambda owner: SimpleNamespace(adopt_session=adopt)
    )
    monkeypatch.setattr(sb.shutil, "which", lambda name: "/usr/bin/git")
    with pytest.raises(ProtocolError) as caught:
        b._session_resume(
            ProtocolRequest(
                id="resume",
                method="session.resume",
                params={"session_id": s.session_id, "target_session_id": "old"},
                protocol_version="1",
            )
        )
    assert caught.value.code == "checkpoint_recovery_failed" and caught.value.__cause__ is failure
    assert (
        s.mutation_error is caught.value and s.mutation_effects_started and s.mutation_in_progress
    )
    b._drain_prompt_queue(s)
    assert (
        effects == [(s.session_id, "old")]
        and ran == []
        and s.agent_session.messages == ["partial checkpoint context"]
    )
    assert (
        b._prompt_queue.get(session_id=s.session_id, prompt_id=pending.item.prompt_id).state
        is PromptState.PENDING
    )
    with b._state_lock:
        with pytest.raises(ProtocolError):
            b._register_job_locked(
                s, sb.BridgeJob(job_id="new", session_id=s.session_id, created_at="now")
            )
    with pytest.raises(sb.BridgeSessionCleanupError):
        b._close_session(s)
    assert closed == []


def test_clear_error_after_real_queue_cancellation_retains_mutation_fence(tmp_path, monkeypatch):
    b, s, closed, call, wire = harness(tmp_path)
    ran = []
    s.agent_session.messages[:] = ["original"]
    s.agent_session.run_turn = lambda message, **kwargs: ran.append(message) or 0
    pending = b._prompt_queue.enqueue(
        session_id=s.session_id, idempotency_key="B", payload={"message": "B"}
    )
    real_cancel = b._prompt_queue.cancel_pending_for_session
    failure = sb.PromptQueueError("outcome acknowledgement lost")

    def lose_reply(**kwargs):
        real_cancel(**kwargs)
        raise failure

    monkeypatch.setattr(b._prompt_queue, "cancel_pending_for_session", lose_reply)
    with pytest.raises(ProtocolError) as caught:
        b._session_clear(
            ProtocolRequest(
                id="clear",
                method="session.clear",
                params={"session_id": s.session_id},
                protocol_version="1",
            )
        )
    assert caught.value.code == "prompt_queue_error" and caught.value.__cause__ is failure
    assert (
        s.mutation_error is caught.value and s.mutation_in_progress and s.mutation_effects_started
    )
    b._drain_prompt_queue(s)
    assert ran == [] and s.agent_session.messages == ["original"]
    assert (
        b._prompt_queue.get(session_id=s.session_id, prompt_id=pending.item.prompt_id).state
        is PromptState.CANCELLED
    )
    with pytest.raises(ProtocolError):
        b._session_clear(
            ProtocolRequest(
                id="retry",
                method="session.clear",
                params={"session_id": s.session_id},
                protocol_version="1",
            )
        )
    assert s.mutation_error is caught.value and closed == []


def test_no_effect_refusal_keeps_its_identity_if_deferred_wakeup_fails(tmp_path, monkeypatch):
    b, s, closed, call, wire = harness(tmp_path)
    s.agent_session.store = SimpleNamespace(sessions_dir=tmp_path / "missing")
    seen = []
    real_resume = b._session_resume_unfenced
    failure = RuntimeError("unknown queue wakeup")

    def observe(request):
        try:
            return real_resume(request)
        except ProtocolError as exc:
            seen.append(exc)
            raise

    monkeypatch.setattr(b, "_session_resume_unfenced", observe)

    def uncertain(owner):
        raise failure

    monkeypatch.setattr(b, "_drain_prompt_queue", uncertain)
    with pytest.raises(ProtocolError) as caught:
        b._session_resume(
            ProtocolRequest(
                id="resume",
                method="session.resume",
                params={"session_id": s.session_id, "target_session_id": "missing"},
                protocol_version="1",
            )
        )
    assert seen == [caught.value] and caught.value.code == "resume_session_not_found"
    assert s.mutation_error is failure and s.mutation_in_progress and closed == []
