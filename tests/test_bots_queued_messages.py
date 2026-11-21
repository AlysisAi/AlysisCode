"""A message the person sent while their owl works waits its turn; until it starts they can
change its words (its files stay) or cancel it. A change never races the owl starting it.

Real store and runtime. The HTTP test at the end needs FastAPI.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError

from alysis_code.bots.clarifications import AskQuestion, ClarificationStore, QuestionDecision
from alysis_code.bots.models import (
    BotInput,
    ConversationPageQuery,
    FileInput,
    IntroInput,
    TaskInput,
)
from alysis_code.bots.queued_messages import QueuedMessageChange, QueuedMessages
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, NotFound, Store

GREEK = "Βρες μου εισιτήρια για Θεσσαλονίκη την Παρασκευή."
GREEK_EDIT = "Όχι Παρασκευή: Σάββατο πρωί, και μόνο απευθείας."
JAPANESE = "金曜日の会議の議事録を要約してください。"


@pytest.fixture
def owl(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("alice", BotInput(name="Κίρκη", purpose="Ταξίδια"))
    chat = store.create_chat("alice", bot["id"], "Chat")
    return store, bot, chat


def send(store, chat, message, file_ids=(), *, request_id=None):
    return store.enqueue(
        "alice",
        chat["id"],
        TaskInput(message=message, request_id=request_id or uuid4().hex, file_ids=list(file_ids)),
    )


def busy(store, chat):
    """The owl works on an earlier message, so the next ones wait."""
    task = send(store, chat, "Πρώτη δουλειά")
    assert store.claim("alice")["id"] == task["id"]
    return task


def edit(store, task, message, revision=0, owner="alice"):
    return QueuedMessages(store, owner).change(
        task["id"],
        QueuedMessageChange(action="edit", expected_revision=revision, message=message),
    )


def cancel(store, task, revision=0, owner="alice"):
    return QueuedMessages(store, owner).change(
        task["id"], QueuedMessageChange(action="cancel", expected_revision=revision)
    )


def shown(store, chat, task_id):
    conversation = store.conversation("alice", chat["id"])
    task = next(item for item in conversation["tasks"] if item["id"] == task_id)
    messages = [m for m in conversation["messages"] if m["task_id"] == task_id]
    return task, messages


def test_a_waiting_message_takes_new_words_and_keeps_its_files(owl):
    store, bot, chat = owl
    first = busy(store, chat)
    file = store.save_file("alice", bot["id"], FileInput(name="δρομολόγια.txt", content="09:00"))
    waiting = send(store, chat, GREEK, [file["id"]])
    task, [message] = shown(store, chat, waiting["id"])
    assert task["queued_message"] == {
        "revision": 0,
        "editable": True,
        "edited": False,
        "withdrawn": False,
    }
    assert task["started_at"] is None

    changed = edit(store, waiting, GREEK_EDIT)

    assert changed["outcome"] == "edited"
    assert changed["task"]["message"] == GREEK_EDIT
    assert changed["task"]["queued_message"]["revision"] == 1
    task, [message] = shown(store, chat, waiting["id"])
    assert task["message"] == message["content"] == GREEK_EDIT
    assert task["queued_message"] == {
        "revision": 1,
        "editable": True,
        "edited": True,
        "withdrawn": False,
    }
    # Its files stay, as sent and in their order.
    assert [f["id"] for f in message["files"]] == [file["id"]]
    assert [e["kind"] for e in store.task("alice", waiting["id"])["events"]] == [
        "queued",
        "message_edited",
    ]

    # The owl starts it with the new words, and the turn reads them.
    store.finish("alice", first["id"], "completed", content="Έγινε")
    started = store.claim("alice")
    assert started["id"] == waiting["id"] and started["message"] == GREEK_EDIT
    turn = BotRuntime(store, "alice", lambda _: None)._context(started)
    request = [m for m in turn if m["role"] == "user"][-1]["content"]
    assert request.startswith(GREEK_EDIT) and GREEK not in request and "δρομολόγια.txt" in request
    task, _ = shown(store, chat, waiting["id"])
    assert task["queued_message"]["editable"] is False and task["started_at"] is not None


def test_an_edit_keeps_words_or_files(owl):
    store, bot, chat = owl
    busy(store, chat)
    words_only = send(store, chat, JAPANESE)
    with pytest.raises(Conflict):
        edit(store, words_only, "   ")
    assert shown(store, chat, words_only["id"])[0]["message"] == JAPANESE
    file = store.save_file("alice", bot["id"], FileInput(name="議事録.txt", content="..."))
    with_file = send(store, chat, JAPANESE, [file["id"]])
    # A message may be only its files, as when it was sent.
    assert edit(store, with_file, "")["outcome"] == "edited"
    assert shown(store, chat, with_file["id"])[0]["message"] == ""


def test_cancel_removes_a_waiting_message_before_it_starts(owl):
    store, bot, chat = owl
    first = busy(store, chat)
    store.finish("alice", first["id"], "completed", content="Πρώτη απάντηση")
    before = store.list_bots("alice")[0]["latest_activity"]
    activity = store.conversation("alice", chat["id"])["activity_seq"]
    second = busy(store, chat)
    waiting = send(store, chat, GREEK, request_id="request_greek_waiting")
    later = send(store, chat, JAPANESE)

    cancelled = cancel(store, waiting)

    assert cancelled["outcome"] == "cancelled"
    assert cancelled["task"]["status"] == "cancelled"
    assert cancelled["task"]["queued_message"]["withdrawn"] is True
    task, messages = shown(store, chat, waiting["id"])
    assert messages == [] and task["message"] == ""
    assert all(
        GREEK not in m["content"] for m in store.conversation("alice", chat["id"])["messages"]
    )
    assert store.search_history("alice", bot["id"], _query("Θεσσαλονίκη"))["messages"] == []
    # The chat's activity is as it was before it was sent: nothing new to read.
    assert store.conversation("alice", chat["id"])["activity_seq"] == activity
    store.finish("alice", second["id"], "completed", content="Δεύτερη απάντηση")
    assert store.list_bots("alice")[0]["latest_activity"]["task_id"] == second["id"]
    assert before["task_id"] == first["id"]
    # The owl goes on to the next message and never reads the cancelled one.
    started = store.claim("alice")
    assert started["id"] == later["id"]
    turn = BotRuntime(store, "alice", lambda _: None)._context(started)
    assert all(GREEK not in str(m.get("content")) for m in turn)
    # Sending the same request again is still that one cancelled message.
    again = send(store, chat, GREEK, request_id="request_greek_waiting")
    assert again["id"] == waiting["id"] and again["status"] == "cancelled"
    assert shown(store, chat, waiting["id"])[1] == []


def _query(text):
    from alysis_code.bots.models import HistoryQuery

    return HistoryQuery(query=text)


def test_a_change_needs_the_revision_the_person_saw(owl):
    store, _, chat = owl
    busy(store, chat)
    waiting = send(store, chat, GREEK)
    assert edit(store, waiting, GREEK_EDIT, revision=0)["outcome"] == "edited"
    # Another window still showing the first words: nothing changes, and it says so.
    stale = edit(store, waiting, JAPANESE, revision=0)
    assert stale["outcome"] == "changed" and stale["task"]["message"] == GREEK_EDIT
    assert stale["task"]["queued_message"]["revision"] == 1
    assert cancel(store, waiting, revision=0)["outcome"] == "changed"
    assert shown(store, chat, waiting["id"])[0]["status"] == "queued"
    assert cancel(store, waiting, revision=1)["outcome"] == "cancelled"


def test_the_same_change_twice_gives_the_same_answer(owl):
    store, _, chat = owl
    busy(store, chat)
    waiting = send(store, chat, GREEK)
    assert edit(store, waiting, GREEK_EDIT, revision=0)["outcome"] == "edited"
    repeated = edit(store, waiting, GREEK_EDIT, revision=0)
    assert repeated["outcome"] == "edited" and repeated["task"]["queued_message"]["revision"] == 1
    assert len(store.task("alice", waiting["id"])["events"]) == 2
    assert cancel(store, waiting, revision=1)["outcome"] == "cancelled"
    assert cancel(store, waiting, revision=1)["outcome"] == "cancelled"
    # An edit that arrives after a cancel changes nothing.
    late = edit(store, waiting, JAPANESE, revision=1)
    assert late["outcome"] == "cancelled" and late["task"]["message"] == ""


def test_once_it_started_nothing_changes_and_it_says_so(owl):
    store, _, chat = owl
    waiting = send(store, chat, GREEK)
    assert store.claim("alice")["id"] == waiting["id"]
    for change in (lambda: edit(store, waiting, GREEK_EDIT), lambda: cancel(store, waiting)):
        result = change()
        assert result["outcome"] == "started"
        assert result["task"]["status"] == "running" and result["task"]["message"] == GREEK
    _, [message] = shown(store, chat, waiting["id"])
    assert message["content"] == GREEK
    store.finish("alice", waiting["id"], "completed", content="Έτοιμο")
    assert edit(store, waiting, GREEK_EDIT)["outcome"] == "started"


@pytest.mark.parametrize("action", ["edit", "cancel"])
def test_a_change_and_the_owl_starting_it_never_both_happen(tmp_path, action):
    outcomes = set()
    for index in range(16):
        store = Store(tmp_path / f"race-{action}-{index}.db")
        bot = store.save_bot("alice", BotInput(name="Ίρις", purpose=""))
        chat = store.create_chat("alice", bot["id"], "Chat")
        waiting = send(store, chat, GREEK)
        barrier = threading.Barrier(2)

        def start(store=store, barrier=barrier):
            barrier.wait()
            return store.claim("alice")

        def change(store=store, barrier=barrier, waiting=waiting):
            barrier.wait()
            if action == "edit":
                return edit(store, waiting, GREEK_EDIT)
            return cancel(store, waiting)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed, changed = pool.submit(start), pool.submit(change)
            claimed, changed = claimed.result(), changed.result()
        outcomes.add(changed["outcome"])
        final, messages = shown(store, chat, waiting["id"])
        if changed["outcome"] == "started":
            # The owl started first: it works on the words as sent, and nothing changed.
            assert claimed["id"] == waiting["id"] and claimed["message"] == GREEK
            assert final["message"] == GREEK and [m["content"] for m in messages] == [GREEK]
        elif action == "edit":
            # The change came first: the owl starts with the new words.
            assert changed["outcome"] == "edited"
            assert claimed["id"] == waiting["id"] and claimed["message"] == GREEK_EDIT
            assert [m["content"] for m in messages] == [GREEK_EDIT]
        else:
            assert changed["outcome"] == "cancelled"
            assert claimed is None and final["status"] == "cancelled" and messages == []
    assert outcomes <= {"started", "edited", "cancelled"}


def test_only_the_persons_own_chat_messages_and_only_by_their_owner(owl):
    store, bot, chat = owl
    first = busy(store, chat)
    waiting = send(store, chat, GREEK)
    with pytest.raises(NotFound):
        edit(store, waiting, GREEK_EDIT, owner="mallory")
    with pytest.raises(NotFound):
        cancel(store, waiting, owner="mallory")
    assert shown(store, chat, waiting["id"])[0]["message"] == GREEK

    # An answer to the owl's question, a routine's run and a new owl's first hello are not
    # messages the person sent in chat: they carry no state to change, and a change is refused.
    questions = ClarificationStore(store, "alice")
    asked = questions.ask(first, AskQuestion(question="Ποια μέρα;"))
    store.finish("alice", first["id"], "completed", content="Ποια μέρα;")
    questions.decide(
        bot["id"],
        asked["id"],
        QuestionDecision(action="answer", expected_revision=asked["revision"], answer="Σάββατο"),
    )
    # Runs of a routine wait while its owl has other work, so this one belongs to an idle owl.
    routines = store.save_bot("alice", BotInput(name="Ρουτίνες", purpose=""))
    store.save_schedule(
        "alice",
        routines["id"],
        ScheduleInput.model_validate(
            {
                "name": "Πρωινά",
                "prompt": "Σύνοψη",
                "timing": {"kind": "interval", "every_minutes": 5},
            }
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules("alice", max_calls=10, now=1400) == 1
    other = store.save_bot("alice", BotInput(name="Νέα", purpose=""))
    store.start_intro("alice", other["id"], IntroInput(request_id="intro_request_1", language="el"))
    others = [
        task
        for owl_id in (bot["id"], routines["id"], other["id"])
        for task in store.conversation("alice", store.list_chats("alice", owl_id)[0]["id"])["tasks"]
        if task["status"] == "queued" and task["id"] != waiting["id"]
    ]
    assert len(others) == 3
    for task in others:
        assert "queued_message" not in task
        with pytest.raises(Conflict):
            edit(store, task, GREEK_EDIT)
        with pytest.raises(Conflict):
            cancel(store, task)
        assert store.task("alice", task["id"])["status"] == "queued"


def test_a_message_that_waited_reads_after_the_reply_it_waited_for(owl):
    store, _, chat = owl
    first = busy(store, chat)
    waiting = send(store, chat, GREEK)
    store.finish("alice", first["id"], "completed", content="Πρώτη απάντηση")
    reply = next(
        m for m in store.conversation("alice", chat["id"])["messages"] if m["role"] == "assistant"
    )
    store.claim("alice")
    task, [message] = shown(store, chat, waiting["id"])
    # Sent while the first turn worked, started once its reply was out.
    assert message["created_at"] < reply["created_at"] <= task["started_at"]
    page = store.conversation_page("alice", chat["id"], ConversationPageQuery())
    paged = next(item for item in page["tasks"] if item["id"] == waiting["id"])
    assert paged["started_at"] == task["started_at"]
    assert paged["queued_message"] == task["queued_message"]


def test_pages_carry_a_change_and_a_cancel(owl):
    store, _, chat = owl
    busy(store, chat)
    waiting = send(store, chat, GREEK)
    later = send(store, chat, JAPANESE)
    first = store.conversation_page("alice", chat["id"], ConversationPageQuery())
    newest = first["messages"][-1]["id"]

    edit(store, waiting, GREEK_EDIT)
    changed = store.conversation_page(
        "alice", chat["id"], ConversationPageQuery(version=first["version"], after=newest)
    )
    assert changed["continues"] is True
    [task] = [item for item in changed["tasks"] if item["id"] == waiting["id"]]
    assert task["message"] == GREEK_EDIT and task["queued_message"]["revision"] == 1

    cancel(store, later)
    # The newest message the app had is gone: the next read replaces what it shows.
    after_cancel = store.conversation_page(
        "alice", chat["id"], ConversationPageQuery(version=changed["version"], after=newest)
    )
    assert after_cancel["continues"] is False
    assert all(m["task_id"] != later["id"] for m in after_cancel["messages"])
    [gone] = [item for item in after_cancel["tasks"] if item["id"] == later["id"]]
    assert gone["queued_message"]["withdrawn"] is True


def test_a_send_retried_after_a_change_is_still_that_message(owl):
    store, _, chat = owl
    busy(store, chat)
    waiting = send(store, chat, GREEK, request_id="request_greek_retry")
    edit(store, waiting, GREEK_EDIT)
    again = send(store, chat, GREEK, request_id="request_greek_retry")
    assert again["id"] == waiting["id"] and again["message"] == GREEK_EDIT
    with pytest.raises(Conflict):
        send(store, chat, JAPANESE, request_id="request_greek_retry")


@pytest.mark.parametrize(
    "body",
    [
        {"action": "edit", "expected_revision": 0},
        {"action": "cancel", "expected_revision": 0, "message": "x"},
        {"action": "edit", "expected_revision": -1, "message": "x"},
        {"action": "rename", "expected_revision": 0, "message": "x"},
        {"action": "edit", "expected_revision": 0, "message": "x" * 20001},
        {"action": "cancel"},
        {"action": "cancel", "expected_revision": 0, "owner": "other"},
    ],
)
def test_a_change_says_what_it_does(body):
    with pytest.raises(ValidationError):
        QueuedMessageChange.model_validate(body)


def test_deleting_the_owl_erases_its_waiting_messages(owl):
    store, bot, chat = owl
    first = busy(store, chat)
    waiting = send(store, chat, GREEK)
    edit(store, waiting, GREEK_EDIT)
    cancel(store, waiting, revision=1)
    store.finish("alice", first["id"], "completed", content="Τέλος")
    store.delete_bot("alice", bot["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM queued_messages").fetchone()[0] == 0


def test_queued_messages_over_http(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "queued.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    # No worker starts anything: the test starts tasks itself.
    app = create_app(settings, client_factory=lambda _: None, work_authorizer=lambda: False)
    store = app.state.store
    bot = store.save_bot(settings.owner, BotInput(name="Μίρα", purpose=""))
    chat = store.create_chat(settings.owner, bot["id"], "Chat")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "queued_message_edits" in client.get("/v1/status").json()["capabilities"]
        sent = [
            client.post(
                f"/v1/chats/{chat['id']}/tasks",
                json={"message": text, "request_id": uuid4().hex},
            ).json()
            for text in ("Πρώτο", GREEK, JAPANESE)
        ]
        assert store.claim(settings.owner)["id"] == sent[0]["id"]
        path = f"/v1/tasks/{sent[1]['id']}/queued"
        edited = client.patch(
            path, json={"action": "edit", "expected_revision": 0, "message": GREEK_EDIT}
        )
        assert edited.status_code == 200 and edited.json()["outcome"] == "edited"
        assert edited.json()["task"]["queued_message"]["revision"] == 1
        stale = client.patch(path, json={"action": "cancel", "expected_revision": 0})
        assert stale.status_code == 200 and stale.json()["outcome"] == "changed"
        assert (
            client.patch(path, json={"action": "edit", "expected_revision": 1}).status_code == 422
        )
        assert (
            client.patch(
                path, json={"action": "cancel", "expected_revision": 1, "reason": "x"}
            ).status_code
            == 422
        )
        cancelled = client.patch(
            f"/v1/tasks/{sent[2]['id']}/queued", json={"action": "cancel", "expected_revision": 0}
        )
        assert cancelled.json()["outcome"] == "cancelled"
        page = client.post(f"/v1/chats/{chat['id']}/page", json={}).json()
        assert JAPANESE not in [m["content"] for m in page["messages"]]
        started = client.patch(
            f"/v1/tasks/{sent[0]['id']}/queued",
            json={"action": "edit", "expected_revision": 0, "message": "Άλλο"},
        )
        assert started.status_code == 200 and started.json()["outcome"] == "started"
        missing = client.patch(
            f"/v1/tasks/{uuid4().hex}/queued", json={"action": "cancel", "expected_revision": 0}
        )
        assert missing.status_code == 404
