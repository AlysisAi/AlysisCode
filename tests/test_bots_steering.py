"""A waiting message the person sends now reaches the turn their owl is running, between its model
rounds, as their own words; a turn that ended first leaves it to run as the next turn.

Real store and runtime; the model is a scripted fixture. The HTTP test at the end needs FastAPI.
"""

from __future__ import annotations

import json
import threading
from uuid import uuid4

import pytest
from pydantic import ValidationError

from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.models import BotInput, FileInput, IntroInput, RegenerateInput, TaskInput
from alysis_code.bots.queued_messages import QueuedMessageChange, QueuedMessages
from alysis_code.bots.reply_review import ReplyReview
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.steering import MAX_PENDING
from alysis_code.bots.store import NotFound, Store, encode
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

REQUEST = "Φτιάξε μια λίστα για το ταξίδι στη Θεσσαλονίκη."
GREEK = "Πρόσθεσε και αντηλιακό· το Σάββατο έχει ζέστη."
CHINESE = "请把行李清单改成中文，并加上充电器。"
JAPANESE = "傘も入れてください。"


def reply(text="", calls=()):
    return LLMResponse(text, list(calls), {}, usage=LLMUsage(10, 5, 15))


def ready():
    return reply(json.dumps({"decision": "ready", "reason": "The reply matches the receipts."}))


class Model:
    model = "fixture"
    supports_tool_calling = True

    def __init__(self, script):
        self.script = iter(script)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        step = next(self.script)
        return step(kwargs) if callable(step) else step


@pytest.fixture
def owl(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Κίρκη", purpose="Ταξίδια", memory_enabled=False))
    chat = store.create_chat("owner", bot["id"], "Chat")
    return store, bot, chat


def send(store, chat, message, file_ids=()):
    return store.enqueue(
        "owner",
        chat["id"],
        TaskInput(message=message, request_id=uuid4().hex, file_ids=list(file_ids)),
    )


def steer(store, task, revision=0):
    return QueuedMessages(store, "owner").change(
        task["id"], QueuedMessageChange(action="steer", expected_revision=revision)
    )


def working(store, chat, message=REQUEST):
    """The owl runs the person's request, so their next messages wait."""
    task = send(store, chat, message)
    assert store.claim("owner")["id"] == task["id"]
    return task


def runtime_for(store, model, *, review=False):
    runtime = BotRuntime(store, "owner", lambda _: model, limits=RuntimeLimits(calls_per_task=12))
    if review:
        runtime.reply_review = ReplyReview(runtime)
    return runtime


def run(runtime, task_id):
    runtime.run(runtime.store.task("owner", task_id), EventCancellationToken(threading.Event()))


def steering_during_tools(runtime, store, waiting, *, at_call=1):
    """The person presses Send now while the owl runs the ``at_call``-th tool of the turn."""
    original, seen, answers = runtime._tool, [], []

    def tool(task, name, arguments, *args, **kwargs):
        seen.append(name)
        if len(seen) == at_call:
            answers.append(steer(store, waiting))
        return original(task, name, arguments, *args, **kwargs)

    runtime._tool = tool
    return answers


def users(messages):
    return [m["content"] for m in messages if m["role"] == "user"]


def kinds(store, task_id):
    return [e["kind"] for e in store.task("owner", task_id)["events"]]


def replies(store, chat, task_id):
    return [
        m["content"]
        for m in store.conversation("owner", chat["id"])["messages"]
        if m["task_id"] == task_id and m["role"] == "assistant"
    ]


def decorated(store, chat, task_id):
    return next(
        task for task in store.conversation("owner", chat["id"])["tasks"] if task["id"] == task_id
    )


def test_a_steer_reaches_the_turn_between_rounds_as_the_persons_own_words(owl):
    store, bot, chat = owl
    first = working(store, chat)
    waiting = send(store, chat, GREEK)

    def second_round(kwargs):
        messages = kwargs["messages"]
        # After the tool's result, the person's words exactly as sent, as their own message.
        assert messages[-2]["role"] == "tool"
        assert messages[-1] == {"role": "user", "content": GREEK}
        assert not any(GREEK in str(m.get("content")) for m in messages if m["role"] == "system")
        return reply("Η λίστα έχει και αντηλιακό.")

    model = Model(
        [
            reply(calls=[ToolCall("list", "save_file", {"name": "λίστα.txt", "content": "Νερό"})]),
            second_round,
        ]
    )
    runtime = runtime_for(store, model)
    answers = steering_during_tools(runtime, store, waiting)
    run(runtime, first["id"])

    assert answers[0]["outcome"] == "steering"
    assert answers[0]["task"]["queued_message"]["steer"] == {
        "state": "pending",
        "task_id": first["id"],
    }
    assert len(model.requests) == 2
    assert store.task("owner", first["id"])["status"] == "completed"
    assert replies(store, chat, first["id"]) == ["Η λίστα έχει και αντηλιακό."]
    # The message is part of the turn it steered: it never runs on its own, and says so.
    message_task = decorated(store, chat, waiting["id"])
    assert message_task["status"] == "completed" and message_task["calls"] == 0
    assert message_task["queued_message"]["steer"] == {"state": "applied", "task_id": first["id"]}
    assert message_task["queued_message"]["editable"] is False
    assert message_task["started_at"] is not None
    assert kinds(store, waiting["id"]) == ["queued", "steer_received", "steer_applied"]
    assert {"steer_received", "steer_applied"} <= set(kinds(store, first["id"]))
    assert store.claim("owner") is None
    assert steer(store, waiting)["outcome"] == "steered"

    # Later turns read it where it was said: in the turn it steered, before that turn's reply.
    later = send(store, chat, JAPANESE)
    history = runtime._context(store.claim("owner"))
    said = [(m["role"], m["content"]) for m in history if m["role"] in ("user", "assistant")]
    assert said[-4:] == [
        ("user", REQUEST),
        ("user", GREEK),
        ("assistant", "Η λίστα έχει και αντηλιακό."),
        ("user", JAPANESE),
    ]
    store.finish("owner", later["id"], "completed", content="Εντάξει.")
    # The reply it steered is still the turn a new answer can be asked from, after the next one.
    store.regenerate("owner", later["id"], RegenerateInput(request_id=uuid4().hex))


def test_a_reply_about_to_be_published_is_held_and_the_turn_goes_on(owl):
    store, bot, chat = owl
    first = working(store, chat)
    waiting = send(store, chat, CHINESE)

    def drafting(kwargs):
        # The person sends their message now, while the owl writes what would be its reply.
        assert steer(store, waiting)["outcome"] == "steering"
        return reply("Here is the packing list.")

    def after_steer(kwargs):
        messages = [m for m in kwargs["messages"] if m["role"] != "system"]
        assert messages[-2:] == [
            {"role": "user", "content": REQUEST},
            {"role": "user", "content": CHINESE},
        ]
        # The held draft was never part of the conversation.
        assert "Here is the packing list." not in json.dumps(messages, ensure_ascii=False)
        return reply("行李清单：充电器、护照。")

    model = Model([drafting, after_steer])
    run(runtime_for(store, model), first["id"])

    assert replies(store, chat, first["id"]) == ["行李清单：充电器、护照。"]
    assert decorated(store, chat, waiting["id"])["queued_message"]["steer"]["state"] == "applied"


def test_the_reply_checker_holds_a_reviewed_reply_and_sees_the_steer_as_the_request(owl):
    store, bot, chat = owl
    first = working(store, chat)
    waiting = send(store, chat, GREEK)
    reviews = []

    def reviewing(kwargs):
        payload = json.loads(kwargs["messages"][1]["content"])
        reviews.append(payload)
        if len(reviews) == 1:
            # The person sends their message now, while the first draft is being checked.
            assert "messages_sent_during_task" not in payload
            assert steer(store, waiting)["outcome"] == "steering"
        else:
            assert payload["current_request"] == REQUEST
            assert payload["messages_sent_during_task"] == [GREEK]
        return ready()

    model = Model(
        [
            reply("Η λίστα: νερό."),
            reviewing,
            lambda kwargs: reply("Η λίστα: νερό, αντηλιακό."),
            reviewing,
        ]
    )
    run(runtime_for(store, model, review=True), first["id"])

    assert len(reviews) == 2
    assert replies(store, chat, first["id"]) == ["Η λίστα: νερό, αντηλιακό."]


def test_a_batch_of_tool_calls_is_never_interrupted(owl):
    store, bot, chat = owl
    first = working(store, chat)
    waiting = send(store, chat, GREEK)

    def second_round(kwargs):
        messages = kwargs["messages"]
        # Both calls ran and answered; the person's words come after the whole batch.
        assert [m["role"] for m in messages[-4:]] == ["assistant", "tool", "tool", "user"]
        assert messages[-1]["content"] == GREEK
        return reply("Δύο αρχεία έτοιμα.")

    model = Model(
        [
            reply(
                calls=[
                    ToolCall("a", "save_file", {"name": "α.txt", "content": "1"}),
                    ToolCall("b", "save_file", {"name": "β.txt", "content": "2"}),
                ]
            ),
            second_round,
        ]
    )
    runtime = runtime_for(store, model)
    answers = steering_during_tools(runtime, store, waiting, at_call=1)
    run(runtime, first["id"])

    assert answers[0]["outcome"] == "steering"
    assert sorted(f["name"] for f in store.list_files("owner", bot["id"])) == ["α.txt", "β.txt"]
    assert replies(store, chat, first["id"]) == ["Δύο αρχεία έτοιμα."]


def test_a_turn_that_ended_first_leaves_the_message_to_run_next(owl):
    store, bot, chat = owl
    first = working(store, chat)
    waiting = send(store, chat, GREEK)
    assert steer(store, waiting)["outcome"] == "steering"
    # The reply was already going out: the turn ends without reading the message.
    store.finish("owner", first["id"], "completed", content="Έτοιμη η λίστα.")

    started = store.claim("owner")
    assert started["id"] == waiting["id"] and started["message"] == GREEK
    assert kinds(store, waiting["id"]) == ["queued", "steer_received", "steer_queued", "running"]
    shown = decorated(store, chat, waiting["id"])
    assert shown["queued_message"]["steer"] == {"state": "queued", "task_id": first["id"]}
    # It is a turn of its own, after the reply it waited for.
    history = BotRuntime(store, "owner", lambda _: None)._context(started)
    assert users(history)[-1] == GREEK
    assert "Έτοιμη η λίστα." in [m["content"] for m in history if m["role"] == "assistant"]

    # Nothing runs now, so a message sent now simply waits its turn, and the answer says so.
    store.finish("owner", waiting["id"], "completed", content="Πρόσθεσα αντηλιακό.")
    later = send(store, chat, CHINESE)
    answer = steer(store, later)
    assert answer["outcome"] == "queued" and answer["reason"] == "not_working"
    assert "steer" not in answer["task"]["queued_message"]
    assert kinds(store, later["id"]) == ["queued"]


def test_a_changed_or_cancelled_steer_reads_as_it_is_then(owl):
    store, bot, chat = owl
    first = working(store, chat)
    changed = send(store, chat, GREEK)
    cancelled = send(store, chat, JAPANESE)
    assert steer(store, changed)["outcome"] == "steering"
    assert steer(store, changed)["outcome"] == "steering"  # The same press again.
    assert steer(store, cancelled)["outcome"] == "steering"
    edited = QueuedMessages(store, "owner").change(
        changed["id"], QueuedMessageChange(action="edit", expected_revision=0, message=CHINESE)
    )
    assert edited["outcome"] == "edited"
    assert edited["task"]["queued_message"]["steer"]["state"] == "pending"
    withdrawn = QueuedMessages(store, "owner").change(
        cancelled["id"], QueuedMessageChange(action="cancel", expected_revision=0)
    )
    assert "steer" not in withdrawn["task"]["queued_message"]
    # A stale window's Send now changes nothing.
    stale = send(store, chat, "Και καπέλο.")
    QueuedMessages(store, "owner").change(
        stale["id"], QueuedMessageChange(action="edit", expected_revision=0, message="Καπέλο.")
    )
    assert steer(store, stale, revision=0)["outcome"] == "changed"

    model = Model([lambda kwargs: reply("OK")])
    runtime = runtime_for(store, model)
    assert runtime.steering.take(store.task("owner", first["id"])) == [
        {"role": "user", "content": CHINESE}
    ]
    assert runtime.steering.take(store.task("owner", first["id"])) == []
    assert store.task("owner", cancelled["id"])["status"] == "cancelled"


def test_steers_are_bounded_and_only_reach_the_persons_own_running_turn(owl):
    store, bot, chat = owl
    with pytest.raises(ValidationError):
        QueuedMessageChange(action="steer", expected_revision=0, message="words")
    first = working(store, chat)
    waiting = [send(store, chat, f"Μήνυμα {n}") for n in range(MAX_PENDING + 1)]
    for task in waiting[:MAX_PENDING]:
        assert steer(store, task)["outcome"] == "steering"
    full = steer(store, waiting[-1])
    assert full["outcome"] == "queued" and full["reason"] == "full"
    assert "steer" not in full["task"]["queued_message"]
    # Each read leaves room for one more.
    runtime = runtime_for(store, Model([]))
    assert len(runtime.steering.take(store.task("owner", first["id"]))) == MAX_PENDING
    assert steer(store, waiting[-1])["outcome"] == "steering"
    store.finish("owner", first["id"], "completed", content="Τέλος.")
    store.claim("owner")

    # A first hello is not the person's request: a message sent during it waits its turn.
    other = store.save_bot("owner", BotInput(name="Νέα", purpose=""))
    store.start_intro("owner", other["id"], IntroInput(request_id="intro_request_1", language="el"))
    other_chat = store.list_chats("owner", other["id"])[0]
    hello = store.claim("owner")
    assert hello["config"].get("intro")
    during_hello = send(store, other_chat, GREEK)
    answer = steer(store, during_hello)
    assert answer["outcome"] == "queued" and answer["reason"] == "not_steerable"

    # Only the person who sent it.
    with pytest.raises(NotFound):
        QueuedMessages(store, "mallory").change(
            during_hello["id"], QueuedMessageChange(action="steer", expected_revision=0)
        )


def test_a_turn_the_owl_started_by_checking_back_is_not_the_persons_to_steer(owl):
    from alysis_code.bots.check_backs import CheckBackRequest, CheckBacks

    store, _, chat = owl
    now = [1_800_000_000.0]
    keep = CheckBacks(
        store, "owner", clock=lambda: now[0], read_page=lambda _: ("", True), run_check=run_now
    )
    source = working(store, chat)
    keep.set(source, CheckBackRequest(note="Δες ξανά τις πτήσεις.", after_seconds=600))
    store.finish("owner", source["id"], "completed", content="Θα ξανακοιτάξω σε δέκα λεπτά.")
    now[0] += 600
    assert keep.tick() == 1
    checking = store.claim("owner")
    assert checking["config"].get("check_back")
    # The person's message during it waits its turn, as one during a routine's run does.
    during = send(store, chat, JAPANESE)
    answer = steer(store, during)
    assert answer["outcome"] == "queued" and answer["reason"] == "not_steerable"
    assert "steer" not in answer["task"]["queued_message"]
    assert runtime_for(store, Model([])).steering.take(checking) == []


def run_now(work):
    work()


def test_a_steer_with_files_names_them_like_any_message(owl):
    store, bot, chat = owl
    first = working(store, chat)
    file = store.save_file("owner", bot["id"], FileInput(name="εισιτήρια.txt", content="ΑΘΗ-ΘΕΣ"))
    waiting = send(store, chat, GREEK, [file["id"]])
    assert steer(store, waiting)["outcome"] == "steering"
    [arrived] = runtime_for(store, Model([])).steering.take(store.task("owner", first["id"]))
    assert arrived["role"] == "user"
    assert arrived["content"].startswith(GREEK + "\n\n")
    assert file["id"] in arrived["content"] and "εισιτήρια.txt" in arrived["content"]


def test_background_corrections_reach_their_turn_as_before(owl):
    store, bot, chat = owl
    original = working(store, chat)
    background = BackgroundStore(store, "owner")
    background.submit(original, "Λίστα", [{"role": "user", "content": REQUEST}])
    assert background.claim()["id"] == original["id"]
    correction = send(store, chat, CHINESE)
    source = store.claim("owner")
    assert source["id"] == correction["id"]
    background.correct(source, original["id"])
    # A waiting message can't steer work that goes on in the background: only a running turn.
    waiting = send(store, chat, GREEK)
    store.finish("owner", correction["id"], "completed", content="Θα το προσθέσω.")
    assert steer(store, waiting)["reason"] == "not_working"

    def first_round(kwargs):
        last = [m for m in kwargs["messages"] if m["role"] != "system"][-1]
        assert last["role"] == "user"
        assert last["content"].startswith("Current user corrections, in order:\n")
        updates = json.loads(last["content"].split("\n", 1)[1])
        assert [u["content"] for u in updates] == [CHINESE]
        assert last["content"] == "Current user corrections, in order:\n" + encode(updates)
        return reply("清单已更新。")

    model = Model([first_round])
    runtime = runtime_for(store, model)
    runtime.run(
        store.task("owner", original["id"]),
        EventCancellationToken(threading.Event()),
        background=True,
    )
    assert store.task("owner", original["id"])["status"] == "completed"
    assert replies(store, chat, original["id"]) == ["清单已更新。"]
    assert store.task("owner", waiting["id"])["status"] == "queued"


def test_an_existing_database_gains_the_steer_columns(tmp_path):
    import sqlite3

    from alysis_code.bots.schema import SCHEMA_VERSION

    path = tmp_path / "earlier.db"
    store = Store(path)
    bot = store.save_bot("owner", BotInput(name="Ήρα", purpose="Ταξίδια"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    waiting = send(store, chat, GREEK)
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX queued_messages_steers")
        db.execute("ALTER TABLE queued_messages DROP COLUMN steer_state")
        db.execute("ALTER TABLE queued_messages DROP COLUMN steer_target")
        db.execute("PRAGMA user_version=46")
    migrated = Store(path)
    with migrated.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(queued_messages)")}
        assert {"steer_target", "steer_state"} <= columns
        indexes = {row["name"] for row in db.execute("PRAGMA index_list(queued_messages)")}
        assert "queued_messages_steers" in indexes
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 49
    # The message sent before still waits as it did, and can be sent now.
    [task] = [
        t for t in migrated.conversation("owner", chat["id"])["tasks"] if t["id"] == waiting["id"]
    ]
    assert "steer" not in task["queued_message"]
    assert migrated.claim("owner")["id"] == waiting["id"]


def test_steering_over_http(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "steer.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    # No worker starts anything: the test starts tasks itself.
    app = create_app(settings, client_factory=lambda _: None, work_authorizer=lambda: False)
    store = app.state.store
    bot = store.save_bot(settings.owner, BotInput(name="Μίρα", purpose=""))
    chat = store.create_chat(settings.owner, bot["id"], "Chat")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        capabilities = client.get("/v1/status").json()["capabilities"]
        assert {"queued_message_edits", "queued_message_steer"} <= set(capabilities)
        sent = [
            client.post(
                f"/v1/chats/{chat['id']}/tasks",
                json={"message": text, "request_id": uuid4().hex},
            ).json()
            for text in (REQUEST, GREEK)
        ]
        assert store.claim(settings.owner)["id"] == sent[0]["id"]
        before = client.post(f"/v1/chats/{chat['id']}/page", json={}).json()
        [waiting] = [task for task in before["tasks"] if task["id"] == sent[1]["id"]]
        # A message that just waits looks as it did before steering existed.
        assert set(waiting["queued_message"]) == {"revision", "editable", "edited", "withdrawn"}
        path = f"/v1/tasks/{sent[1]['id']}/queued"
        answer = client.patch(path, json={"action": "steer", "expected_revision": 0})
        assert answer.status_code == 200 and answer.json()["outcome"] == "steering"
        assert answer.json()["task"]["queued_message"]["steer"]["task_id"] == sent[0]["id"]
        words = client.patch(path, json={"action": "steer", "expected_revision": 0, "message": "x"})
        assert words.status_code == 422
        page = client.post(f"/v1/chats/{chat['id']}/page", json={}).json()
        [paged] = [task for task in page["tasks"] if task["id"] == sent[1]["id"]]
        assert paged["queued_message"]["steer"]["state"] == "pending"
