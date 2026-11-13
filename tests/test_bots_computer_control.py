"""Control authority/real Code loop tests; scripted executors and models."""

from dataclasses import replace
from uuid import uuid4

import pytest
from test_bots_browser_runtime import ClientFixture, response
from test_bots_desktop import Executor
from test_bots_desktop_runtime import run_task
from test_bots_desktop_runtime import study as execution_study

from alysis_code.bots.computer import ComputerControlPause, ComputerViews
from alysis_code.bots.computer_control import ComputerControls, ControlRequest
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound

study = execution_study


def test_stream_disconnect_revokes_input_and_explicit_take_prepares_a_new_link(study, monkeypatch):
    runtime, request, inputs, _ = setup(study, monkeypatch)
    store, task, manager, _ = study
    view, _, _ = manager.views.current("owner", task["bot_id"])
    ready = [True]
    preparations = []
    view.input_ready = lambda: ready[0]

    def prepare():
        preparations.append(True)
        ready[0] = True

    view.prepare_input = prepare
    assert request("take")["state"] == "pausing"
    runtime.computer_controls.park(task, [{"role": "user", "content": "Work"}])
    assert request("heartbeat")["state"] == "controlling"
    ready[0] = False
    assert runtime.computer_controls.status(task["bot_id"])["state"] == "paused"
    with pytest.raises(Conflict, match="expired"):
        request("input", sequence=1, event={"kind": "text", "text": "must not arrive"})
    assert not inputs
    assert request("take")["state"] == "controlling"
    assert len(preparations) == 2
    assert (
        request("input", sequence=1, event={"kind": "text", "text": "new gesture"})["sequence"] == 1
    )
    assert inputs == [{"kind": "text", "text": "new gesture"}]


def setup(study, monkeypatch):
    store, task, manager, instances = study
    inputs = []
    monkeypatch.setattr(
        Executor,
        "human_input",
        lambda self, event: inputs.append(event) if event.get("kind") != "release" else None,
        raising=False,
    )
    runtime = BotRuntime(store, "owner", lambda _: None, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    manager.call(
        task,
        "computer_observe",
        {},
        runtime.task_token(task["id"]),
        approved=True,
        image_input=True,
    )
    session = manager.views.frame("owner", task["bot_id"])["session_id"]
    identity = uuid4().hex

    def request(action, **kw):
        return runtime.computer_controls.request(
            task["bot_id"],
            ControlRequest(action=action, client_id=identity, session_id=session, **kw),
        )

    return runtime, request, inputs, session


def test_control_drains_parks_fences_and_invalidates(study, monkeypatch):
    runtime, request, inputs, session = setup(study, monkeypatch)
    store, task, manager, instances = study
    old_frame = manager._computers[task["bot_id"]].frame
    assert request("take")["state"] == "pausing"
    with pytest.raises(Conflict):
        request("input", sequence=1, event={"kind": "text", "text": "secret"})
    with pytest.raises(ComputerControlPause):
        manager.call(
            task,
            "computer_run",
            {"command": "old command"},
            runtime.task_token(task["id"]),
            approved=True,
        )
    runtime.computer_controls.park(task, [{"role": "user", "content": "Keep working"}])
    assert request("heartbeat")["state"] == "controlling"
    assert (
        request("input", sequence=1, event={"kind": "text", "text": "synthetic-private-α"})[
            "sequence"
        ]
        == 1
    )
    with pytest.raises(Conflict):
        request("input", sequence=1, event={"kind": "text", "text": "duplicate"})
    assert len(inputs) == 1
    request("resume")
    assert manager._computers[task["bot_id"]].frame is None and old_frame
    claimed = runtime.background.claim()
    assert claimed["id"] == task["id"] and runtime.background.claim() is None
    assert "synthetic-private" not in str(store.task("owner", task["id"]))
    with store.connect() as db:
        checkpoint = db.execute(
            "SELECT messages FROM background_turns WHERE task_id=?", (task["id"],)
        ).fetchone()[0]
    assert "synthetic-private" not in checkpoint and "Human actions may have changed" in checkpoint


def test_other_client_disconnect_expiry_and_stop(study, monkeypatch):
    runtime, request, inputs, session = setup(study, monkeypatch)
    store, task, manager, _ = study
    now = [100.0]
    runtime.computer_controls.clock = lambda: now[0]
    request("take")
    runtime.computer_controls.park(task, [])
    for action in ("take", "input", "resume", "heartbeat"):
        with pytest.raises(Conflict):
            runtime.computer_controls.request(
                task["bot_id"],
                ControlRequest(action=action, client_id=uuid4().hex, session_id=session),
            )
    now[0] += 16
    assert runtime.computer_controls.status(task["bot_id"])["state"] == "paused"
    with pytest.raises(Conflict):
        request("input", sequence=1, event={"kind": "key", "keys": ["Return"]})
    assert runtime.background.claim() is None
    request("take")
    request("disconnect")
    assert runtime.computer_controls.status(task["bot_id"])["state"] == "paused"
    request("take")
    runtime.cancel(task["id"])
    with pytest.raises(Conflict):
        request("input", sequence=1, event={"kind": "text", "text": "after Stop"})
    assert inputs == []
    manager.reap()
    assert not manager._computers


def test_read_only_status_does_not_reject_current_control_heartbeat(study, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    runtime, request, _, _ = setup(study, monkeypatch)
    _, task, manager, _ = study
    request("take")
    runtime.computer_controls.park(task, [])
    view, _, _ = manager.views.current("owner", task["bot_id"])
    alive = view.alive
    entered, release, requested = Event(), Event(), Event()

    def slow_liveness():
        entered.set()
        assert release.wait(5)
        return alive()

    view.alive = slow_liveness
    with ThreadPoolExecutor(max_workers=2) as pool:
        checking = pool.submit(runtime.computer_controls.reap)
        assert entered.wait(5)

        def heartbeat():
            requested.set()
            return request("heartbeat")

        waiting = pool.submit(heartbeat)
        assert requested.wait(5)
        try:
            with pytest.raises(TimeoutError):
                waiting.result(timeout=0.05)
        finally:
            release.set()
        checking.result(timeout=5)
        assert waiting.result(timeout=5)["state"] == "controlling"


@pytest.mark.parametrize("revocation", ["expiry", "stop", "new_session"])
def test_waiting_input_rechecks_authority_before_dispatch(study, monkeypatch, revocation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    runtime, request, inputs, _ = setup(study, monkeypatch)
    _, task, manager, _ = study
    now = [100.0]
    runtime.computer_controls.clock = lambda: now[0]
    request("take")
    runtime.computer_controls.park(task, [])
    requested = Event()

    def input_request():
        requested.set()
        return request("input", sequence=1, event={"kind": "text", "text": "synthetic"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        with runtime.computer_controls._operations.hold(task["bot_id"]):
            waiting = pool.submit(input_request)
            assert requested.wait(5)
            with pytest.raises(TimeoutError):
                waiting.result(timeout=0.05)
            if revocation == "expiry":
                now[0] += 16
            elif revocation == "stop":
                runtime.computer_controls.stop()
            else:
                view, _, _ = manager.views.current("owner", task["bot_id"])
                view.generation = uuid4().hex
        with pytest.raises(Conflict):
            waiting.result(timeout=5)
    assert inputs == []


def test_acknowledged_slow_input_keeps_current_grant(study, monkeypatch):
    runtime, request, _, _ = setup(study, monkeypatch)
    _, task, manager, _ = study
    now = [100.0]
    runtime.computer_controls.clock = lambda: now[0]
    request("take")
    runtime.computer_controls.park(task, [])
    view, _, _ = manager.views.current("owner", task["bot_id"])
    original = view.human_input

    def slow_input(event):
        original(event)
        now[0] += 20

    view.human_input = slow_input
    assert (
        request("input", sequence=1, event={"kind": "text", "text": "synthetic"})["sequence"] == 1
    )
    assert request("heartbeat")["state"] == "controlling"


def test_screen_status_does_not_report_agent_ownership_during_human_input(study, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    runtime, request, inputs, _ = setup(study, monkeypatch)
    _, task, manager, _ = study
    now = [100.0]
    runtime.computer_controls.clock = lambda: now[0]
    request("take")
    runtime.computer_controls.park(task, [])
    view, _, _ = manager.views.current("owner", task["bot_id"])
    original = view.human_input
    entered, release = Event(), Event()

    def slow_input(event):
        now[0] += 20  # An admitted gesture stays active through its receipt.
        entered.set()
        assert release.wait(5)
        return original(event)

    view.human_input = slow_input
    with ThreadPoolExecutor(max_workers=2) as pool:
        gesture = pool.submit(
            request, "input", sequence=1, event={"kind": "text", "text": "synthetic"}
        )
        assert entered.wait(5)
        screen = pool.submit(runtime.computer_controls.status, task["bot_id"])
        try:
            # A fresh frame must not expire while its status waits behind
            # queued keystrokes. This read does not authorize another input.
            assert screen.result(timeout=1)["state"] == "controlling"
        finally:
            release.set()
        assert gesture.result(timeout=5)["sequence"] == 1
        assert screen.result(timeout=5)["state"] == "controlling"
    assert inputs == [{"kind": "text", "text": "synthetic"}]


def test_contended_status_reports_expiry_without_performing_guest_cleanup(study, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    runtime, request, _, _ = setup(study, monkeypatch)
    _, task, manager, _ = study
    now = [100.0]
    runtime.computer_controls.clock = lambda: now[0]
    request("take")
    runtime.computer_controls.park(task, [])
    view, _, _ = manager.views.current("owner", task["bot_id"])
    gestures = []
    view.human_input = lambda event: gestures.append(event)
    entered, release = Event(), Event()

    def operation():
        with runtime.computer_controls._operations.hold(task["bot_id"]):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(operation)
        assert entered.wait(5)
        now[0] += 16
        try:
            state = pool.submit(runtime.computer_controls.status, task["bot_id"]).result(timeout=1)
            assert state["state"] == "paused"
            assert gestures == [] and task["bot_id"] in runtime.computer_controls._grants
        finally:
            release.set()
        worker.result(timeout=5)
    runtime.computer_controls.reap()
    assert gestures == [{"kind": "release"}]
    assert task["bot_id"] not in runtime.computer_controls._grants


def test_give_back_drains_viewer_capture_before_its_fresh_frame(study, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    runtime, request, _, _ = setup(study, monkeypatch)
    _, task, manager, _ = study
    request("take")
    runtime.computer_controls.park(task, [])
    view, _, _ = manager.views.current("owner", task["bot_id"])
    started, release, resumed = Event(), Event(), Event()
    capture = view.capture
    captures = []

    def slow_capture():
        captures.append("capture")
        if len(captures) == 1:
            started.set()
            assert release.wait(5)
        return capture()

    view.capture = slow_capture
    with ThreadPoolExecutor(max_workers=2) as pool:
        watching = pool.submit(manager.views.frame, "owner", task["bot_id"])
        assert started.wait(5)

        def give_back():
            resumed.set()
            return request("resume")

        returning = pool.submit(give_back)
        assert resumed.wait(5)
        try:
            # Return must not call the executor while the viewer owns capture.
            with pytest.raises(TimeoutError):
                returning.result(timeout=0.1)
            assert captures == ["capture"]
        finally:
            release.set()
        assert watching.result(timeout=5)["state"] == "live"
        assert returning.result(timeout=5)["state"] == "agent"
    assert captures == ["capture", "capture"]


def test_service_restart_keeps_pause_but_revokes_input(study, monkeypatch):
    runtime, request, inputs, session = setup(study, monkeypatch)
    store, task, manager, _ = study
    request("take")
    runtime.computer_controls.park(task, [])
    manager.close()
    assert store.recover("owner") == 0
    recovered = ComputerControls(store, "owner", ComputerViews())
    recovered.recover()
    assert recovered.status(task["bot_id"])["computer_lost"]
    assert runtime.background.claim() is None
    recovered.request(
        task["bot_id"], ControlRequest(action="resume", client_id=uuid4().hex, session_id=session)
    )
    assert runtime.background.claim()["id"] == task["id"]
    assert inputs == []


def test_cross_owner_and_stale_session_cannot_control(study, monkeypatch):
    runtime, request, inputs, session = setup(study, monkeypatch)
    store, task, manager, _ = study
    other = ComputerControls(store, "other", manager.views)
    with pytest.raises(NotFound):
        other.request(
            task["bot_id"], ControlRequest(action="take", client_id=uuid4().hex, session_id=session)
        )
    with pytest.raises(Conflict):
        runtime.computer_controls.request(
            task["bot_id"],
            ControlRequest(action="take", client_id=uuid4().hex, session_id=uuid4().hex),
        )
    assert not manager.views.fenced("owner", task["bot_id"])


def test_takeover_during_model_call_discards_response_and_retains_computer(study, monkeypatch):
    store, task, manager, instances = study
    monkeypatch.setattr(Executor, "human_input", lambda *_: None, raising=False)
    identity = uuid4().hex
    session = []

    def take(**kwargs):
        current = manager.views.frame("owner", task["bot_id"])["session_id"]
        session.append(current)
        runtime.computer_controls.request(
            task["bot_id"], ControlRequest(action="take", client_id=identity, session_id=current)
        )
        return response(name="computer_run", arguments={"command": "must never dispatch"})

    class TakingClient(ClientFixture):
        def chat(self, **kwargs):
            answer = super().chat(**kwargs)
            return answer(**kwargs) if callable(answer) else answer

    client = TakingClient([response(name="computer_run", arguments={"command": "inspect"}), take])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    run_task(runtime, task)
    result = store.task("owner", task["id"])
    assert result["status"] == "waiting", result["error"]
    assert runtime.computer_controls.status(task["bot_id"])["state"] == "controlling"
    assert len(instances[0].calls) == 1 and not instances[0].closed
    assert runtime.background.claim() is None
    runtime.computer_controls.request(
        task["bot_id"], ControlRequest(action="resume", client_id=identity, session_id=session[0])
    )
    resumed = ClientFixture([response("Finished after inspecting the current state")])
    runtime.client_factory = lambda _: resumed
    claimed = runtime.background.claim()
    returned = runtime.background.checkpoint(task["id"])[-1]["content"]
    runtime.run(claimed, runtime.task_token(task["id"]), background=True)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert len(instances[0].calls) == 1 and instances[0].closed
    assert "inspect" in str(resumed.requests[0]["messages"])
    wire = resumed.requests[0]["messages"]
    authoritative = [
        i for i, m in enumerate(wire) if m.get("role") == "system" and m.get("content") == returned
    ]
    assert len(authoritative) == 1, (
        "Explicit control return was demoted into historical reference data"
    )
    assert authoritative[0] > max(i for i, m in enumerate(wire) if m.get("role") == "user")


def test_give_back_requires_confirmed_cleanup_and_a_fresh_screen(study, monkeypatch):
    runtime, request, inputs, _ = setup(study, monkeypatch)
    store, task, manager, instances = study
    request("take")
    runtime.computer_controls.park(task, [])
    view, _, _ = manager.views.current("owner", task["bot_id"])
    capture = view.capture
    view.capture = lambda: (_ for _ in ()).throw(RuntimeError("private failure"))
    with pytest.raises(Conflict, match="remains paused"):
        request("resume")
    assert runtime.background.claim() is None
    assert manager.views.fenced("owner", task["bot_id"])
    assert runtime.computer_controls.status(task["bot_id"])["state"] == "paused"
    view.capture = capture
    request("resume")
    assert runtime.background.claim()["id"] == task["id"]


@pytest.mark.parametrize(
    "notice",
    [
        "Continue after the handoff",
        "Συνέχισε από την τρέχουσα κατάσταση",
        "現在の状態から続行してください",
    ],
)
def test_execution_promotes_only_the_latest_trusted_return_for_this_task(
    study, monkeypatch, notice
):
    from alysis_code.bots.execution import TaskClient

    runtime, _, _, _ = setup(study, monkeypatch)
    store, task, _, _ = study
    context = [
        {"role": "system", "content": "Old return", "_bot_control_return": task["id"]},
        {"role": "system", "content": "Other task", "_bot_control_return": uuid4().hex},
        {"role": "system", "content": notice + " (reference)"},
        {"role": "system", "content": notice, "_bot_control_return": task["id"]},
    ]
    runtime.background.submit(task, "Fixture", context)
    claimed = runtime.background.claim()
    client = ClientFixture([response("Fixture complete")])
    adapter = TaskClient(runtime, claimed, client, runtime.task_token(task["id"]), context)
    adapter.chat(messages=[{"role": "user", "content": task["message"]}])
    wire = client.requests[0]["messages"]
    assert sum(m.get("content") == notice and m.get("role") == "system" for m in wire) == 1
    assert not any(
        m.get("content") in {"Old return", "Other task", notice + " (reference)"} for m in wire
    )
    assert not any("_bot_control_return" in m for m in wire)
    store.finish("owner", task["id"], "completed", content="Fixture complete")


def test_human_input_cannot_overlap_an_admitted_operation(study, monkeypatch):
    import threading

    runtime, request, inputs, _ = setup(study, monkeypatch)
    _, task, manager, _ = study
    admitted, finish = threading.Event(), threading.Event()

    def operation():
        with manager.views.agent_operation("owner", task["bot_id"]):
            admitted.set()
            assert finish.wait(3)

    worker = threading.Thread(target=operation)
    worker.start()
    assert admitted.wait(3)
    try:
        request("take")
        runtime.computer_controls.park(task, [])
        assert runtime.computer_controls.status(task["bot_id"])["state"] == "pausing"
        with pytest.raises(Conflict):
            request("input", sequence=1, event={"kind": "key", "keys": ["Return"]})
    finally:
        finish.set()
        worker.join(3)
    assert request("heartbeat")["state"] == "controlling"
    assert inputs == []


def test_correction_during_human_control_is_seen_before_continuation(study, monkeypatch):
    from alysis_code.bots.models import TaskInput

    runtime, request, inputs, _ = setup(study, monkeypatch)
    store, task, manager, _ = study
    request("take")
    runtime.computer_controls.park(task, [])
    store.enqueue(
        "owner",
        task["chat_id"],
        TaskInput(message="Use the revised deadline: Friday.", request_id=uuid4().hex),
    )
    source = store.claim("owner")
    runtime.background.correct(source, task["id"])
    store.finish("owner", source["id"], "completed", content="Correction recorded")
    request("resume")
    client = ClientFixture([response("The revised deadline is Friday.")])
    runtime.client_factory = lambda _: client
    runtime.run(runtime.background.claim(), runtime.task_token(task["id"]), background=True)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert "Use the revised deadline: Friday." in str(client.requests[0]["messages"])


def test_bot_can_request_takeover_without_granting_itself_input(study, monkeypatch):
    store, task, manager, instances = study
    monkeypatch.setattr(Executor, "human_input", lambda *_: None, raising=False)
    client = ClientFixture(
        [
            response(name="computer_run", arguments={"command": "inspect"}),
            response(
                name="request_computer_control",
                arguments={"reason": "Complete this step in the computer."},
            ),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    run_task(runtime, task)
    assert store.task("owner", task["id"])["status"] == "waiting"
    state = runtime.computer_controls.status(task["bot_id"])
    assert state["state"] == "paused"
    assert not runtime.computer_controls._grants
    assert not instances[0].closed
    assert runtime.background.claim() is None


@pytest.mark.parametrize("decision", ["resume", "deny"])
def test_takeover_racing_action_review_preserves_stop_and_expires_old_authority(
    study, monkeypatch, decision
):
    from alysis_code.bots.action_review import ApprovalDecision

    store, task, manager, instances = study
    monkeypatch.setattr(Executor, "human_input", lambda *_: None, raising=False)
    identity = uuid4().hex
    session = []

    class Reviewer(ClientFixture):
        def chat(self, **kwargs):
            if kwargs.get("tools") is None:
                current = manager.views.frame("owner", task["bot_id"])["session_id"]
                session.append(current)
                runtime.computer_controls.request(
                    task["bot_id"],
                    ControlRequest(action="take", client_id=identity, session_id=current),
                )
                return response('{"decision":"ask_person","reason":"Please confirm"}')
            return super().chat(**kwargs)

    client = Reviewer([response(name="computer_run", arguments={"command": "write result"})])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.action_review_enabled = True
    # The owl's private computer is only reviewed when the person asked for it, here with
    # their own "ask first" rule; the race below is between that review and a takeover.
    from alysis_code.bots.action_review import RuleInput

    runtime.action_reviews.save_rule(
        task["bot_id"], RuleInput(effect="ask", description="Ask before writing results")
    )
    run_task(runtime, task)
    (review,) = runtime.action_reviews.list(task["bot_id"])
    assert review["status"] == "pending" and not instances[0].calls
    assert runtime.computer_controls.status(task["bot_id"])["state"] == "controlling"
    with pytest.raises(Conflict):
        runtime.action_reviews.decide(
            task["bot_id"],
            review["id"],
            ApprovalDecision(action="allow", expected_revision=review["revision"]),
        )
    if decision == "deny":
        runtime.action_reviews.decide(
            task["bot_id"],
            review["id"],
            ApprovalDecision(action="deny", expected_revision=review["revision"]),
        )
        runtime.computer_controls.reap()
        assert store.task("owner", task["id"])["status"] == "cancelled"
        assert not manager.views.fenced("owner", task["bot_id"])
    else:
        runtime.computer_controls.request(
            task["bot_id"],
            ControlRequest(action="resume", client_id=identity, session_id=session[0]),
        )
        assert (
            runtime.action_reviews.list(task["bot_id"], include_history=True)[0]["status"]
            == "expired"
        )
        assert runtime.background.claim()["id"] == task["id"]


def test_repeated_browser_staleness_parks_task_without_granting_input(study, monkeypatch):
    from test_bots_browser_runtime import BrowserFixture

    from alysis_code.bots.browser import BrowserInteractionStalled

    store, task, manager, instances = study
    monkeypatch.setattr(Executor, "human_input", lambda *_: None, raising=False)
    browser = BrowserFixture()
    browser.views = manager.views

    def stalled(*args, **kwargs):
        raise BrowserInteractionStalled("Browser input was blocked three times. No input was sent.")

    browser.call = stalled
    client = ClientFixture(
        [
            response(name="computer_run", arguments={"command": "inspect"}),
            response(name="browser_observe"),
        ]
    )
    runtime = BotRuntime(
        store, "owner", lambda _: client, desktop_computers=manager, browser_computers=browser
    )
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    run_task(runtime, task)
    detail = store.task("owner", task["id"])
    assert detail["status"] == "waiting", detail
    assert detail["wait_reason"] == "computer_control"
    assert any(
        event["kind"] == "computer_control_requested" and "three times" in event["data"]["message"]
        for event in detail["events"]
    )
    assert runtime.computer_controls.status(task["bot_id"])["state"] == "paused"
    assert not runtime.computer_controls._grants
    assert not instances[0].closed
    assert runtime.background.claim() is None
    assert len(client.requests) == 2
