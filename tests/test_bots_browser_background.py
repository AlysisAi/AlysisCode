"""Same-task browser handoff with a real store and adapter, no live provider."""

from __future__ import annotations

import copy
import json
import threading
import time
from uuid import uuid4

import pytest
from test_bots_browser import PREVIEW, PUBLIC, Harness
from test_bots_browser_runtime import ClientFixture, advertised_names, enqueue, response

from alysis_code.bots.models import TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, Store
from alysis_code.ide.managed_browser import BrowserCancelledError


class CallbackClient:
    model = "browser-background-fixture"
    supports_tool_calling = True

    def __init__(self, callback):
        self.callback, self.requests = callback, []

    def chat(self, **kwargs):
        self.requests.append(
            {
                "messages": copy.deepcopy(kwargs["messages"]),
                "tools": copy.deepcopy(kwargs["tools"]),
            }
        )
        return self.callback(len(self.requests), kwargs)


@pytest.fixture
def work(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    request = enqueue(store)
    lab = Harness()
    runtime = BotRuntime(store, "owner", lambda _: None, browser_computers=lab.adapter)
    yield store, request, lab, runtime
    runtime.stop()


def handoff():
    return response(
        name="continue_in_background", arguments={"summary": "Read the requested pages"}
    )


def prepare(work):
    store, request, lab, runtime = work
    client = ClientFixture(
        [response(name="browser_navigate", arguments={"url": PREVIEW}), handoff()]
    )
    runtime.client_factory = lambda _: client
    runtime.run(store.claim("owner"), runtime.task_token(request["id"]))
    assert store.task("owner", request["id"])["status"] == "waiting"
    assert lab.adapter._computers[request["bot_id"]].task_id == request["id"]
    source = json.loads(
        next(m for m in reversed(client.requests[1]["messages"]) if m["role"] == "tool")["content"]
    )["source_id"]
    return source, client


def followup(store, request, message):
    return store.enqueue(
        "owner", request["chat_id"], TaskInput(message=message, request_id=uuid4().hex)
    )


def test_approved_website_work_keeps_its_grant_in_background_only(work):
    from alysis_code.bots.proposals import ActionDecision, ActionProposal

    store, request, lab, runtime = work
    source_task = store.claim("owner")
    proposal = runtime.proposals.propose(
        source_task,
        ActionProposal(
            question="Allow the site?",
            instructions="Enter the supplied note once.",
            browser_origin=PREVIEW.rstrip("/"),
        ),
    )
    store.finish("owner", source_task["id"], "completed", content="Review the website permission.")
    accepted = runtime.proposals.decide(
        request["bot_id"],
        proposal["id"],
        ActionDecision(
            action="accept",
            expected_revision=1,
            browser_origin=proposal["browser_origin"],
        ),
    )
    approved = store.task("owner", accepted["task_id"])
    lab.adapter.interactive_previews = False
    source, _ = prepare((store, approved, lab, runtime))
    assert runtime.proposals.browser_origins(approved) == ()  # Queued handoff cannot input.
    claimed = runtime.background.claim()
    assert runtime.proposals.browser_origins(claimed) == (PREVIEW.rstrip("/"),)
    expected = "Σημείωση — 日本語"
    client = ClientFixture(
        [
            response(
                name="browser_type", arguments={"source_id": source, "node": 5, "text": expected}
            ),
            response("The observed input contains the exact note."),
        ]
    )
    runtime.client_factory = lambda _: client
    runtime.run(claimed, runtime.task_token(claimed["id"]), background=True)
    assert "browser_type" in advertised_names(client.requests[0])
    assert lab.executors[0].browser.data["strings"][12] == expected
    assert store.task("owner", claimed["id"])["status"] == "completed"
    assert runtime.proposals.browser_origins(claimed) == ()
    later = followup(store, request, "Another request")
    assert runtime.proposals.browser_origins(later) == ()


def test_background_claim_before_foreground_returns_keeps_owner_and_sources(work, monkeypatch):
    store, request, lab, runtime = work
    foreground = ClientFixture(
        [response(name="browser_navigate", arguments={"url": PREVIEW}), handoff()]
    )
    runtime.client_factory = lambda _: foreground
    entered, finish = threading.Event(), threading.Event()
    submit = runtime.background.submit
    threads, background_clients = [], []

    def submit_and_start(*args):
        submit(*args)
        claimed = runtime.background.claim()
        source = json.loads(
            next(m for m in reversed(foreground.requests[1]["messages"]) if m["role"] == "tool")[
                "content"
            ]
        )["source_id"]

        def reply(step, kwargs):
            if step == 1:
                return response(name="browser_source", arguments={"source_id": source})
            retained = json.loads(
                next(m for m in reversed(kwargs["messages"]) if m["role"] == "tool")["content"]
            )
            assert json.loads(retained["content"])["data"]["strings"][0] == PREVIEW
            entered.set()
            assert finish.wait(5)
            return response("Read the original observation")

        client = CallbackClient(reply)
        background_clients.append(client)
        runtime.client_factory = lambda _: client
        thread = threading.Thread(
            target=runtime.run,
            args=(claimed, runtime.task_token(request["id"])),
            kwargs={"background": True},
        )
        threads.append(thread)
        thread.start()
        assert entered.wait(5)

    monkeypatch.setattr(runtime.background, "submit", submit_and_start)
    try:
        runtime.run(store.claim("owner"), runtime.task_token(request["id"]))
        computer = lab.adapter._computers[request["bot_id"]]
        assert computer.task_id == request["id"]
        original_executor = lab.executors[0]

        plain = followup(store, request, "What are you doing?")
        runtime.client_factory = lambda _: ClientFixture([response("Still reading")])
        runtime.run(store.claim("owner"), runtime.task_token(plain["id"]))
        assert store.task("owner", plain["id"])["status"] == "completed"
        assert computer.task_id == request["id"]
        assert len(lab.executors) == 1

        another = followup(store, request, "Open another page")
        other_client = ClientFixture(
            [
                response(name="browser_navigate", arguments={"url": PUBLIC}),
                response("The computer is busy"),
            ]
        )
        runtime.client_factory = lambda _: other_client
        runtime.run(store.claim("owner"), runtime.task_token(another["id"]))
        assert "error" in json.loads(
            next(m for m in reversed(other_client.requests[-1]["messages"]) if m["role"] == "tool")[
                "content"
            ]
        )
        assert computer.task_id == request["id"]
        assert not any(
            call[0] == "navigate" and call[3] == (PUBLIC,)
            for call in original_executor.browser.calls
        )
    finally:
        finish.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert store.task("owner", request["id"])["status"] == "completed"
    assert computer.task_id is None and computer.cancelled is None
    assert runtime.task_token(request["id"]).is_cancelled
    lab.adapter.reap()
    assert not original_executor.closed  # Normal release detached the terminal token.
    assert len(background_clients[0].requests) == 2


def test_stopping_queued_handoff_reaps_claim_without_background_execution(work):
    store, request, lab, runtime = work
    _, client = prepare(work)
    runtime.cancel(request["id"])
    assert runtime.background.claim() is None
    lab.adapter.reap()
    assert lab.executors[0].closed
    assert lab.views.frame("owner", request["bot_id"])["state"] == "not_running"
    assert len(client.requests) == 2
    assert store.task("owner", request["id"])["status"] == "cancelled"


@pytest.mark.parametrize("cancel_before_run", [False, True])
def test_background_releases_inherited_claim_without_another_browser_tool(work, cancel_before_run):
    store, request, lab, runtime = work
    prepare(work)
    claimed = runtime.background.claim()
    computer = lab.adapter._computers[request["bot_id"]]
    client = ClientFixture([response("Summary from the original observation")])
    runtime.client_factory = lambda _: client
    if cancel_before_run:
        runtime.cancel(request["id"])
    runtime.run(claimed, runtime.task_token(request["id"]), background=True)
    if cancel_before_run:
        assert client.requests == []
        assert lab.executors[0].closed
        assert store.task("owner", request["id"])["status"] == "cancelled"
    else:
        assert computer.task_id is None and computer.cancelled is None
        lab.adapter.reap()
        assert not lab.executors[0].closed
        assert store.task("owner", request["id"])["status"] == "completed"


@pytest.mark.parametrize("name", ["browser_click", "browser_type"])
def test_background_cannot_use_preview_mutations_even_when_executor_enables_them(work, name):
    store, request, lab, runtime = work
    source, _ = prepare(work)
    arguments = {"source_id": source, "node": 5}
    if name == "browser_type":
        arguments["text"] = "Denied"
    client = ClientFixture(
        [response(name=name, arguments=arguments), response("Read-only background")]
    )
    runtime.client_factory = lambda _: client
    runtime.run(runtime.background.claim(), runtime.task_token(request["id"]), background=True)
    assert lab.adapter.interactive_previews
    browser_names = advertised_names(client.requests[0]) & {
        "browser_navigate",
        "browser_observe",
        "browser_source",
        "browser_click",
        "browser_type",
    }
    assert browser_names == {"browser_navigate", "browser_observe", "browser_source"}
    assert "error" in json.loads(
        next(m for m in reversed(client.requests[-1]["messages"]) if m["role"] == "tool")["content"]
    )
    assert not any(call[0] in {"click", "type"} for call in lab.executors[0].browser.calls)
    assert store.task("owner", request["id"])["status"] == "completed"


def test_background_allocates_lazily_if_foreground_handoff_did_not_use_browser(work):
    store, request, lab, runtime = work
    runtime.client_factory = lambda _: ClientFixture([handoff()])
    runtime.run(store.claim("owner"), runtime.task_token(request["id"]))
    assert lab.executors == []
    runtime.client_factory = lambda _: ClientFixture(
        [response(name="browser_navigate", arguments={"url": PUBLIC}), response("Read")]
    )
    runtime.run(runtime.background.claim(), runtime.task_token(request["id"]), background=True)
    assert len(lab.executors) == 1
    assert lab.executors[0].browser.data["strings"][0] == PUBLIC
    assert store.task("owner", request["id"])["status"] == "completed"


def test_scheduled_work_does_not_gain_browser_by_moving_into_background(work, monkeypatch):
    store, request, lab, runtime = work
    monkeypatch.setattr(store, "schedule_context", lambda *_: {"request": "Read the page"})
    runtime.client_factory = lambda _: ClientFixture([handoff()])
    runtime.run(store.claim("owner"), runtime.task_token(request["id"]))
    client = ClientFixture(
        [
            response(name="browser_navigate", arguments={"url": PUBLIC}),
            response("Browser unavailable for this scheduled turn"),
        ]
    )
    runtime.client_factory = lambda _: client
    runtime.run(runtime.background.claim(), runtime.task_token(request["id"]), background=True)
    assert not any(name.startswith("browser_") for name in advertised_names(client.requests[0]))
    assert "error" in json.loads(
        next(m for m in reversed(client.requests[1]["messages"]) if m["role"] == "tool")["content"]
    )
    assert lab.executors == []
    assert store.task("owner", request["id"])["status"] == "completed"


def test_correction_during_background_model_call_suppresses_stale_browser_action(work):
    store, request, lab, runtime = work
    source, _ = prepare(work)
    correction = followup(store, request, "Διόρθωση: use the already observed page. 日本語も残す。")
    current = store.claim("owner")

    def reply(step, kwargs):
        if step == 1:
            runtime.background.correct(current, request["id"])
            return response(name="browser_navigate", arguments={"url": PUBLIC})
        assert correction["message"] in json.dumps(kwargs["messages"], ensure_ascii=False)
        if step == 2:
            return response(name="browser_source", arguments={"source_id": source})
        return response("Read the corrected source")

    client = CallbackClient(reply)
    runtime.client_factory = lambda _: client
    runtime.run(runtime.background.claim(), runtime.task_token(request["id"]), background=True)
    assert len(client.requests) == 3
    assert not any(
        call[0] == "navigate" and call[3] == (PUBLIC,) for call in lab.executors[0].browser.calls
    )
    assert store.task("owner", request["id"])["status"] == "completed"


def test_correction_during_browser_read_is_applied_at_next_step_not_reported_as_immediate(
    work, monkeypatch
):
    store, request, lab, runtime = work
    prepare(work)
    correction = followup(store, request, "Keep the original page and revise the summary")
    current = store.claim("owner")
    browser = lab.executors[0].browser
    snapshot = browser.snapshot

    def read_with_correction(*args, **kwargs):
        result = snapshot(*args, **kwargs)
        runtime.background.correct(current, request["id"])
        return result

    monkeypatch.setattr(browser, "snapshot", read_with_correction)
    client = ClientFixture([response(name="browser_observe"), response("Revised summary")])
    runtime.client_factory = lambda _: client
    runtime.run(runtime.background.claim(), runtime.task_token(request["id"]), background=True)
    assert correction["message"] in json.dumps(client.requests[1]["messages"], ensure_ascii=False)
    assert any(
        message["role"] == "tool" and "source_id" in message["content"]
        for message in client.requests[1]["messages"]
    )
    assert store.task("owner", request["id"])["status"] == "completed"


@pytest.mark.parametrize("stop_runtime", [False, True], ids=["stop-task", "shutdown"])
def test_running_background_browser_cancellation_and_shutdown_close_owned_computer(
    work, stop_runtime
):
    store, request, lab, runtime = work
    entered, exited = threading.Event(), threading.Event()

    def block_second_snapshot(executor, cancel):
        snapshot = executor.browser.snapshot
        count = 0

        def read(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
                try:
                    deadline = time.monotonic() + 5
                    while not kwargs["cancel"]():
                        assert time.monotonic() < deadline
                        threading.Event().wait(0.01)
                    raise BrowserCancelledError("Synthetic cancellation")
                finally:
                    exited.set()
            return snapshot(*args, **kwargs)

        executor.browser.snapshot = read

    lab.factory_hook = block_second_snapshot
    client = ClientFixture(
        [
            response(name="browser_navigate", arguments={"url": PREVIEW}),
            handoff(),
            response(name="browser_observe"),
        ]
    )
    runtime.client_factory = lambda _: client
    runtime.start()
    try:
        assert entered.wait(5)
        if stop_runtime:
            runtime.stop()
        else:
            runtime.cancel(request["id"])
        assert exited.wait(5)
    finally:
        runtime.stop()
    assert store.task("owner", request["id"])["status"] == (
        "interrupted" if stop_runtime else "cancelled"
    )
    assert store.task("owner", request["id"])["output"] == ""
    assert lab.executors[0].closed
    assert lab.executors[0].view_at_close == ["not_running"]
    assert runtime._thread is runtime._background_thread is runtime._scheduler is None


def test_two_foreground_replies_finish_while_background_browser_holds_operation_lock(
    work, monkeypatch
):
    store, request, lab, runtime = work
    reading, finish_read, idle_reap = threading.Event(), threading.Event(), threading.Event()
    releases = []

    def delayed_browser(executor, cancel):
        snapshot = executor.browser.snapshot
        reads = 0

        def read(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                reading.set()
                assert finish_read.wait(10)
            return snapshot(*args, **kwargs)

        executor.browser.snapshot = read

    reap, release = lab.adapter.reap, lab.adapter.release

    def attempted_reap():
        if reading.is_set() and threading.current_thread().name == "alysis-bots-worker":
            idle_reap.set()
        reap()

    def observed_release(task_id, *, cancelled):
        releases.append((task_id, cancelled))
        release(task_id, cancelled=cancelled)

    monkeypatch.setattr(lab.adapter, "reap", attempted_reap)
    monkeypatch.setattr(lab.adapter, "release", observed_release)
    lab.factory_hook = delayed_browser
    original = ClientFixture(
        [
            response(name="browser_navigate", arguments={"url": PREVIEW}),
            handoff(),
            response(name="browser_observe"),
            response("Reading finished"),
        ]
    )
    replies = {}
    runtime.client_factory = lambda task_id: (
        original
        if task_id == request["id"]
        else replies.setdefault(task_id, ClientFixture([response("I am still reading")]))
    )

    def complete(task_id):
        deadline = time.monotonic() + 3
        while store.task("owner", task_id)["status"] != "completed":
            assert time.monotonic() < deadline, "Foreground reply waited for background browser I/O"
            threading.Event().wait(0.01)

    runtime.start()
    try:
        assert reading.wait(5)
        # Prove the idle worker attempted maintenance under the held browser
        # lock; a blocked reaper would prevent even the first reply being claimed.
        assert idle_reap.wait(5)
        for message in ("What are you reading?", "Thanks, keep reading"):
            follow = followup(store, request, message)
            runtime.wake()
            complete(follow["id"])
            assert len(replies[follow["id"]].requests) == 1
        # The second reply proves the first finally did not wait in release().
        assert not finish_read.is_set()
        assert store.task("owner", request["id"])["status"] == "waiting"
        assert releases == []
        assert len(lab.executors) == 1
        finish_read.set()
        complete(request["id"])
    finally:
        finish_read.set()
        runtime.stop()
    assert releases == [(request["id"], False)]
    assert lab.executors[0].closed


@pytest.mark.parametrize("claimed_before_restart", [False, True], ids=["queued", "running"])
def test_restart_never_restores_computer_or_replays_running_background_work(
    work, claimed_before_restart
):
    store, request, old_lab, old_runtime = work
    old_source, _ = prepare(work)
    if claimed_before_restart:
        assert old_runtime.background.claim()["id"] == request["id"]
    old_runtime.stop()
    assert old_lab.executors[0].closed
    restarted = Store(store.path)
    assert restarted.recover("owner") == int(claimed_before_restart)
    lab = Harness()
    client = ClientFixture(
        [
            response(name="browser_source", arguments={"source_id": old_source}),
            response(name="browser_navigate", arguments={"url": PREVIEW}),
            response("Read afresh"),
        ]
    )
    runtime = BotRuntime(restarted, "owner", lambda _: client, browser_computers=lab.adapter)
    try:
        if claimed_before_restart:
            assert runtime.background.claim() is None
            assert restarted.task("owner", request["id"])["status"] == "interrupted"
            assert lab.executors == [] and client.requests == []
        else:
            runtime.run(
                runtime.background.claim(), runtime.task_token(request["id"]), background=True
            )
            assert restarted.task("owner", request["id"])["status"] == "completed"
            assert "error" in json.loads(
                next(m for m in reversed(client.requests[1]["messages"]) if m["role"] == "tool")[
                    "content"
                ]
            )
            fresh = json.loads(
                next(m for m in reversed(client.requests[2]["messages"]) if m["role"] == "tool")[
                    "content"
                ]
            )
            assert fresh["source_id"] != old_source
            assert len(lab.executors) == 1
            assert (
                len([call for call in lab.executors[0].browser.calls if call[0] == "navigate"]) == 1
            )
    finally:
        runtime.stop()


def test_failed_handoff_releases_foreground_claim_normally(work, monkeypatch):
    store, request, lab, runtime = work

    def reject(*args):
        raise Conflict("Synthetic handoff rejection")

    monkeypatch.setattr(runtime.background, "submit", reject)
    client = ClientFixture(
        [response(name="browser_observe"), handoff(), response("Kept foreground")]
    )
    runtime.client_factory = lambda _: client
    runtime.run(store.claim("owner"), runtime.task_token(request["id"]))
    assert store.task("owner", request["id"])["status"] == "completed"
    computer = lab.adapter._computers[request["bot_id"]]
    assert computer.task_id is None and computer.cancelled is None
    assert runtime.background.claim() is None
    lab.adapter.reap()
    assert not lab.executors[0].closed


@pytest.mark.parametrize("background", [False, True], ids=["foreground", "inherited-background"])
def test_success_releases_claim_before_terminal_token_can_trigger_idle_reaper(
    work, monkeypatch, background
):
    store, request, lab, runtime = work
    if background:
        prepare(work)
        claimed = runtime.background.claim()
        client = ClientFixture([response("Summary from retained context")])
    else:
        claimed = store.claim("owner")
        client = ClientFixture([response(name="browser_observe"), response("Read the page")])
    runtime.client_factory = lambda _: client
    finish = store.finish
    closed_at_publication = []

    def finish_then_reap(owner, task_id, status, **kwargs):
        finish(owner, task_id, status, **kwargs)
        if task_id == request["id"] and status == "completed":
            assert runtime.task_token(task_id).is_cancelled
            # Simulate the idle foreground worker getting its turn immediately
            # after terminal publication, before run's finally can execute.
            lab.adapter.reap()
            closed_at_publication.append(lab.executors[0].closed)

    monkeypatch.setattr(store, "finish", finish_then_reap)
    runtime.run(claimed, runtime.task_token(request["id"]), background=background)
    assert store.task("owner", request["id"])["status"] == "completed"
    assert closed_at_publication == [False]
    computer = lab.adapter._computers[request["bot_id"]]
    assert computer.task_id is None and computer.cancelled is None
    assert lab.views.frame("owner", request["bot_id"])["state"] == "live"
