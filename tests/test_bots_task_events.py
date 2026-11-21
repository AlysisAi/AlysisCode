"""A task's progress as it happens (POST /v1/tasks/{id}/events, task_events.py): a waiting
request answers as soon as an event is written, an empty answer comes after the bounded wait,
``after`` is honoured, the owl's draft reply never appears, and another owner's task is refused.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Store
from alysis_code.bots.task_events import TaskEventsQuery, TaskSignals, follow
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage

OWNER = "owner"


def settings_for(tmp_path, owner=OWNER):
    return Settings(
        tmp_path / "owls.db",
        "t" * 40,
        "fixture",
        "https://example.invalid",
        "fixture",
        owner=owner,
    )


def running_task(store, owner=OWNER, message="Βρες τιμές για 東京"):
    bot = store.save_bot(owner, BotInput(name="Ίρις"))
    chat = store.create_chat(owner, bot["id"], "Συζήτηση")
    store.enqueue(owner, chat["id"], TaskInput(message=message, request_id="req_" + uuid4().hex))
    return store.claim(owner)


@contextmanager
def service(tmp_path, owner=OWNER):
    """The owls API over the test's store, without starting its worker (no task is claimed or
    recovered under the test)."""
    settings = settings_for(tmp_path, owner)
    store = Store(settings.database)
    app = create_app(settings, task_store=store)
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + settings.token
    yield store, client


def last_seq(store, task_id):
    return store.task(OWNER, task_id)["events"][-1]["seq"]


def until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_status_lists_the_live_task_events_capability(tmp_path):
    with service(tmp_path) as (_store, client):
        assert "live_task_events" in client.get("/v1/status").json()["capabilities"]


def test_a_waiting_request_answers_as_soon_as_an_event_is_written(tmp_path):
    with service(tmp_path) as (store, client):
        task = running_task(store)
        after = last_seq(store, task["id"])
        answers = []

        def ask():
            started = time.monotonic()
            response = client.post(
                f"/v1/tasks/{task['id']}/events", json={"after": after, "wait": 8}
            )
            answers.append((time.monotonic() - started, response))

        asker = threading.Thread(target=ask)
        asker.start()
        # The request is waiting for this task before anything new is written.
        until(lambda: task["id"] in store.task_signals._waiters)
        store.event(
            OWNER, task["id"], "tool_started", {"name": "web_search", "topic": "ξενοδοχεία"}
        )
        asker.join(10)
        elapsed, response = answers[0]
        assert response.status_code == 200
        body = response.json()
        assert elapsed < 4, "the answer came only when the wait ran out"
        assert [event["kind"] for event in body["events"]] == ["tool_started"]
        assert body["events"][0]["data"] == {"name": "web_search", "topic": "ξενοδοχεία"}
        assert body["after"] == body["events"][0]["seq"] > after
        assert body["status"] == "running" and body["more"] is False
        # Nobody is left waiting once the answer is out.
        assert task["id"] not in store.task_signals._waiters


def test_an_event_written_by_the_turn_ending_wakes_the_request_too(tmp_path):
    with service(tmp_path) as (store, client):
        task = running_task(store)
        after = last_seq(store, task["id"])
        answers = []
        asker = threading.Thread(
            target=lambda: answers.append(
                client.post(f"/v1/tasks/{task['id']}/events", json={"after": after, "wait": 8})
            )
        )
        asker.start()
        until(lambda: task["id"] in store.task_signals._waiters)
        finished = time.monotonic()
        store.finish(OWNER, task["id"], "completed", content="Τελική απάντηση 完成")
        asker.join(10)
        assert time.monotonic() - finished < 4, "the answer came only when the wait ran out"
        body = answers[0].json()
        assert body["status"] == "completed"
        assert [event["kind"] for event in body["events"]] == ["completed"]
        # The reply is read from the conversation, never from the feed.
        assert "Τελική" not in json.dumps(body, ensure_ascii=False)


def test_with_nothing_new_it_answers_empty_once_the_wait_is_over(tmp_path):
    with service(tmp_path) as (store, client):
        task = running_task(store)
        after = last_seq(store, task["id"])
        started = time.monotonic()
        body = client.post(
            f"/v1/tasks/{task['id']}/events", json={"after": after, "wait": 0.4}
        ).json()
        elapsed = time.monotonic() - started
        assert 0.35 <= elapsed < 3
        assert body["events"] == [] and body["after"] == after and body["status"] == "running"
        assert task["id"] not in store.task_signals._waiters


def test_a_finished_task_answers_at_once(tmp_path):
    with service(tmp_path) as (store, client):
        task = running_task(store)
        store.finish(OWNER, task["id"], "failed", error="Stopped")
        after = last_seq(store, task["id"])
        started = time.monotonic()
        body = client.post(
            f"/v1/tasks/{task['id']}/events", json={"after": after, "wait": 8}
        ).json()
        assert time.monotonic() - started < 3
        assert body["events"] == [] and body["status"] == "failed"


def test_after_gives_only_later_events_in_order_like_the_task_lists_them(tmp_path):
    with service(tmp_path) as (store, client):
        task = running_task(store)
        store.event(OWNER, task["id"], "progress_note", {"text": "Ψάχνω τρία καταστήματα."})
        store.event(OWNER, task["id"], "tool_started", {"name": "web_fetch", "host": "例え.jp"})
        store.event(OWNER, task["id"], "tool_completed", {"name": "web_fetch"})
        listed = store.task(OWNER, task["id"])["events"]
        first_note = next(event for event in listed if event["kind"] == "progress_note")
        path = f"/v1/tasks/{task['id']}/events"

        body = client.post(path, json={"after": first_note["seq"]}).json()
        later = [event for event in listed if event["seq"] > first_note["seq"]]
        # Same events, same shape and order as GET /v1/tasks/{id}.
        assert body["events"] == later
        assert body["after"] == later[-1]["seq"] and body["more"] is False

        everything = client.post(path, json={"after": 0}).json()["events"]
        assert everything == listed

        page = client.post(path, json={"after": 0, "limit": 2}).json()
        assert page["events"] == listed[:2] and page["more"] is True
        rest = client.post(path, json={"after": page["after"], "limit": 200}).json()
        assert page["events"] + rest["events"] == listed


def test_requests_outside_the_bounds_are_refused(tmp_path):
    with service(tmp_path) as (store, client):
        task = running_task(store)
        path = f"/v1/tasks/{task['id']}/events"
        for bad in (
            {"wait": 10.5},
            {"wait": -1},
            {"after": -1},
            {"limit": 0},
            {"limit": 201},
            {"after": 0, "since": 3},
        ):
            assert client.post(path, json=bad).status_code == 422, bad


def test_another_owners_task_is_refused(tmp_path):
    with service(tmp_path, owner="someone-else") as (store, client):
        task = running_task(store, owner=OWNER)
        path = f"/v1/tasks/{task['id']}/events"
        refused = client.post(path, json={"wait": 1})
        assert refused.status_code == 404
        # Exactly like reading the task itself.
        assert client.get(f"/v1/tasks/{task['id']}").status_code == 404
        del client.headers["Authorization"]
        assert client.post(path, json={}).status_code == 401
        # Nothing was left waiting for it.
        assert not store.task_signals._waiters


def test_a_draft_reply_never_reaches_the_feed(tmp_path):
    """A first draft the reply checker withholds, then a reply it passes: neither draft shows in
    the feed while the turn works, and the withheld one never does."""
    withheld = "Το αρχείο 東京.txt αποθηκεύτηκε."
    reply = "Δεν αποθήκευσα ακόμα το αρχείο."
    suggestion = "Αποθήκευσέ το τώρα"
    settings = settings_for(tmp_path)
    store = Store(settings.database)
    task = running_task(store, message="Αποθήκευσε μια σημείωση 東京.txt")
    client = TestClient(create_app(settings, task_store=store))
    client.headers["Authorization"] = "Bearer " + settings.token
    path = f"/v1/tasks/{task['id']}/events"
    seen = []

    def feed():
        body = client.post(path, json={"after": 0}).json()
        seen.append(body)
        return json.dumps(body, ensure_ascii=False)

    def first_review(_request):
        assert withheld not in feed()
        return LLMResponse(
            json.dumps(
                {
                    "decision": "repair",
                    "reason": f'The draft says "{withheld}" but no save receipt exists.',
                    "issue": "contradiction",
                    "false_claim": withheld,
                },
                ensure_ascii=False,
            ),
            [],
            {},
            usage=LLMUsage(3, 2, 5),
        )

    def second_review(_request):
        text = feed()
        assert withheld not in text and reply not in text and suggestion not in text
        return LLMResponse(
            '{"decision":"ready","reason":"Honest status."}', [], {}, usage=LLMUsage(3, 2, 5)
        )

    answers = iter(
        [
            LLMResponse(withheld, [], {}, usage=LLMUsage(10, 5, 15)),
            first_review,
            LLMResponse(
                f"{reply}\n<follow_ups>\n- {suggestion}\n</follow_ups>",
                [],
                {},
                usage=LLMUsage(10, 5, 15),
            ),
            second_review,
        ]
    )

    class Model:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            item = next(answers)
            if callable(item):
                assert kwargs["messages"][0]["content"] == REVIEW_INSTRUCTIONS
                return item(kwargs)
            return item

    runtime = BotRuntime(store, OWNER, lambda _: Model(), limits=RuntimeLimits(calls_per_task=8))
    runtime.reply_review = ReplyReview(runtime)
    runtime.run(task, EventCancellationToken(threading.Event()))

    final = store.task(OWNER, task["id"])
    assert final["status"] == "completed" and final["output"] == reply
    # The task itself keeps the checker's verdicts; the feed has them, with their data withheld.
    recorded = json.dumps(final["events"], ensure_ascii=False)
    assert withheld in recorded and suggestion in recorded
    body = client.post(path, json={"after": 0}).json()
    text = json.dumps(body, ensure_ascii=False)
    assert withheld not in text and reply not in text and suggestion not in text
    held = [event for event in body["events"] if event.get("withheld")]
    assert {event["kind"] for event in held} == {"reply_review_finished", "reply_suggestions"}
    assert all(event["data"] == {} for event in held)
    assert len(seen) == 2


def test_waiting_requests_are_bounded_per_owner():
    signals = TaskSignals(max_waiters=1)
    with signals.watching("a", "t1", lambda: None) as first:
        with signals.watching("a", "t2", lambda: None) as second:
            # Another owner still gets its own allowance.
            with signals.watching("b", "t3", lambda: None) as other:
                assert (first, second, other) == (True, False, True)
    assert not signals._waiters and not signals._owners


def test_a_request_over_the_bound_answers_at_once_without_waiting(tmp_path):
    store = Store(tmp_path / "owls.db")
    task = running_task(store)
    store.task_signals = TaskSignals(max_waiters=0)
    query = TaskEventsQuery(after=last_seq(store, task["id"]), wait=8)
    started = time.monotonic()
    answer = asyncio.run(follow(store, OWNER, task["id"], query))
    assert time.monotonic() - started < 3 and answer["events"] == []


def test_the_shared_database_store_wakes_followers_after_its_commit(monkeypatch):
    """PostgreSQL stores wake the same way, after the transaction commits (no database: the
    connection pool is a stand-in)."""
    pytest.importorskip("psycopg")
    from alysis_code.bots.postgres import PostgresStore

    order = []

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, kind, *_):
            order.append("rollback" if kind else "commit")

    class Raw:
        def transaction(self):
            return Transaction()

        def execute(self, *_args):
            return None

    class Pool:
        def getconn(self):
            return Raw()

        def putconn(self, _raw):
            pass

    store = object.__new__(PostgresStore)
    store._pool, store._fence, store.task_signals = Pool(), None, TaskSignals()
    woken = []
    with store.task_signals.watching(OWNER, "t1", lambda: woken.append(list(order))):
        with store.connect(write=True) as db:
            Store._event(db, "t1", "tool_started", {"name": "web_search"})
            assert woken == []
        assert woken == [["commit"]]
        with pytest.raises(RuntimeError):
            with store.connect(write=True) as db:
                Store._event(db, "t1", "tool_started", {"name": "web_search"})
                raise RuntimeError("rolled back")
        # A rolled-back transaction wrote nothing to wake anyone for.
        assert woken == [["commit"]]
