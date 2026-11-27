"""An owl's earlier chat made ready while it is idle (context_summary.WarmUp).

A short while after a turn ends, the memory lane writes the summary of the owl's earlier turns
and indexes the memories saved since, from exactly what the next turn would read, on the model
that turn thinks with. The person's next turn then starts its own work at once. A turn that
starts first wins and summarises itself; nothing is kept for a chat that changed while it was
read. Real store and runtime; scripted models; Greek and Chinese chats; two models with
different context windows.
"""

from __future__ import annotations

import json
import math
import threading
import time
from uuid import uuid4

import pytest

from alysis_code.bots import context_summary
from alysis_code.bots.context_summary import (
    KEEP_FOR_PERSON,
    SUMMARY_INSTRUCTIONS,
    WARM_SECONDS,
    WARM_TRIES,
)
from alysis_code.bots.memory_applicability import INSTRUCTIONS as INDEX_INSTRUCTIONS
from alysis_code.bots.memory_capture import INSTRUCTIONS as CAPTURE_INSTRUCTIONS
from alysis_code.bots.memory_capture import MAX_FACTS, QUIET_SECONDS
from alysis_code.bots.models import BotInput, MemoryInput, NoteInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Store
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage

OWNER = "alice"

# Each earlier turn is long enough that a dozen of them outgrow a small model's history allowance.
CHATS = {
    "english": (
        "Project decision {i}: keep budget.xlsx unchanged and deliver by Friday. " * 14,
        "Budget decisions: budget.xlsx stays unchanged; delivery is due Friday.",
    ),
    "greek": (
        "Απόφαση έργου {i}: το αρχείο προϋπολογισμός.xlsx μένει ως έχει, παράδοση την Παρασκευή. "
        * 10,
        "Αποφάσεις έργου: το προϋπολογισμός.xlsx μένει ως έχει· παράδοση την Παρασκευή.",
    ),
    "chinese": (
        "项目决定 {i}：预算.xlsx 保持不变，周五前交付，所有改动先发给我确认。" * 14,
        "项目决定：预算.xlsx 保持不变；周五前交付。",
    ),
}
# Two models' allowances: a small context window keeps a short history before summarising, a
# large one holds the whole chat.
SMALL_WINDOW = RuntimeLimits(history_tokens=1500)
LARGE_WINDOW = RuntimeLimits(context_tokens=400_000, history_tokens=200_000)


class Model:
    """A scripted model. It summarises in the words it was given, citing the first message it
    was shown; sorts every memory record as general; and answers a turn.

    ``during`` runs inside the first call of kind ``during_kind`` ("summary" or "index"), as if
    the provider were still working. A real client watches the token while it waits: when
    ``during`` returns True this one does too, so a cancelled call stops there."""

    supports_tool_calling = True

    def __init__(
        self,
        summary="Summary.",
        *,
        model="model-small-window",
        during=None,
        during_kind="summary",
        facts=(),
    ):
        self.summary, self.model, self.facts = summary, model, list(facts)
        self.during, self.during_kind = during, during_kind
        self.requests: list[list[dict]] = []
        self.kinds: list[str] = []

    def _provider_works(self, kind, token):
        if self.during is None or kind != self.during_kind:
            return
        during, self.during = self.during, None
        if during() and token.wait(timeout=5):
            raise CooperativeCancellationError()

    def chat(self, **kwargs):
        messages = json.loads(json.dumps(kwargs["messages"]))
        self.requests.append(messages)
        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        if system == SUMMARY_INSTRUCTIONS:
            self.kinds.append("summary")
            self._provider_works("summary", kwargs["cancellation_token"])
            payload = json.loads(messages[1]["content"])
            first = payload["settled_tasks"][0]["messages"][0]["id"]
            items = [
                {"kind": "project_context", "text": self.summary, "source_message_ids": [first]}
            ]
            return reply(json.dumps({"items": items}, ensure_ascii=False))
        if system == INDEX_INSTRUCTIONS:
            self.kinds.append("index")
            self._provider_works("index", kwargs["cancellation_token"])
            records = json.loads(messages[-1]["content"])
            items = [{"id": record["id"], "scope": "general"} for record in records]
            return reply(json.dumps({"items": items}))
        if system == CAPTURE_INSTRUCTIONS.format(max_facts=MAX_FACTS):
            self.kinds.append("capture")
            return reply(json.dumps({"facts": self.facts}, ensure_ascii=False))
        self.kinds.append("turn")
        return reply("Έτοιμο.")


def reply(content):
    return LLMResponse(content, [], {}, usage=LLMUsage(10, 5, 15))


def owl(store, language="english", turns=12):
    bot = store.save_bot(OWNER, BotInput(name="Γλαύκα"))
    chat = store.create_chat(OWNER, bot["id"], "Συνομιλία")
    text = CHATS[language][0]
    for i in range(turns):
        task = store.enqueue(
            OWNER, chat["id"], TaskInput(message=text.format(i=i), request_id=uuid4().hex)
        )
        assert store.claim(OWNER)["id"] == task["id"]
        store.finish(OWNER, task["id"], "completed", content="Καταγράφηκε.")
    return bot, chat


def runtime(store, model, limits=SMALL_WINDOW):
    return BotRuntime(store, OWNER, lambda _: model, limits=limits)


def later():
    """Once the owl has been quiet long enough after its last turn."""
    return time.time() + WARM_SECONDS + 1


def next_turn(store, chat, message="Τι αποφασίσαμε;"):
    task = store.enqueue(OWNER, chat["id"], TaskInput(message=message, request_id=uuid4().hex))
    assert store.claim(OWNER)["id"] == task["id"]
    return task


def run_turn(engine, store, task):
    engine.run(task, EventCancellationToken(threading.Event()))
    return store.task(OWNER, task["id"])


def purposes(task):
    return [e["data"]["purpose"] for e in task["events"] if e["kind"] == "model_started"]


def summary_state(store, bot):
    return context_summary.BotContextSummary(store, OWNER).read(bot["id"])


def warm(engine, bot):
    """The owl's turn ended a while ago: one pass of the memory lane."""
    engine.warm_up.ended(bot["id"], now=time.time() - WARM_SECONDS - 1)
    return engine.warm_up_pass(now=later())


# ---------------------------------------------------------------------------------------------
# The next turn starts at once


@pytest.mark.parametrize("model_name", ["model-small-window", "model-other-small-window"])
def test_the_next_turn_of_a_long_chat_makes_no_summary_call_once_the_idle_summary_is_written(
    tmp_path, model_name
):
    # Without the idle summary, a long chat's next turn first waits for one.
    store = Store(tmp_path / "before.db")
    bot, chat = owl(store)
    model = Model(model=model_name)
    before = run_turn(runtime(store, model), store, next_turn(store, chat))
    assert purposes(before)[0] == "history_summary"

    store = Store(tmp_path / "after.db")
    bot, chat = owl(store)
    model = Model(model=model_name)
    engine = runtime(store, model)
    done = warm(engine, bot)
    assert done and done[0]["history_summary"][-1] == "not_needed"
    written = done[0]["history_summary"][:-1]
    assert written and set(written) == {"written"}
    assert model.kinds == ["summary"] * len(written)
    state = summary_state(store, bot)
    assert state["revision"] == len(written) and state["through_id"]

    # Recorded like the memory lane's read: calls of the newest turn the person asked, labelled.
    newest = store.conversation(OWNER, chat["id"])["tasks"][-1]
    newest = store.task(OWNER, newest["id"])
    assert purposes(newest) == ["history_summary"] * len(written)
    assert [call["status"] for call in newest["usage"]][-len(written) :] == ["completed"] * len(
        written
    )
    assert store.usage(OWNER)["calls"] == len(written)

    turn = run_turn(engine, store, next_turn(store, chat))
    assert turn["status"] == "completed"
    assert purposes(turn) == ["turn"]
    assert model.kinds[len(written) :] == ["turn"]
    # The turn read the summary the idle owl wrote.
    assert any(
        m["role"] == "user" and "Historical conversation summary" in str(m["content"])
        for m in model.requests[-1]
    )
    assert "Summary." in json.dumps(model.requests[-1], ensure_ascii=False)


def test_a_turn_arms_the_idle_work_and_a_starting_turn_takes_it_back(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store, turns=1)
    model = Model()
    engine = runtime(store, model)
    assert not engine.warm_up.due(later())
    run_turn(engine, store, next_turn(store, chat))
    assert not engine.warm_up.due(time.time())
    assert engine.warm_up.due(later())
    # A short chat needs no summary and few memories need no index: nothing is called.
    assert engine.warm_up_pass(now=later()) == [{"history_summary": ["not_needed"]}]
    assert not engine.warm_up.due(later())
    assert model.kinds == ["turn"]
    engine.warm_up.ended(bot["id"])
    engine.warm_up.started(bot["id"])
    assert not engine.warm_up.due(later())


# ---------------------------------------------------------------------------------------------
# A turn that starts first wins


@pytest.mark.parametrize("language", ["greek", "english"])
def test_a_turn_that_starts_during_the_idle_summary_wins_and_summarises_itself(tmp_path, language):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store, language)
    started = {}

    def person_writes():
        started["task"] = next_turn(store, chat)
        return True

    model = Model(CHATS[language][1], during=person_writes)
    engine = runtime(store, model)
    assert warm(engine, bot) == [{"history_summary": ["cancelled"]}]
    state = summary_state(store, bot)
    assert state["revision"] == 0 and not state["through_id"]
    assert store.task(OWNER, started["task"]["id"])["usage"] == []
    # The call was made: it stays on the ledger of the turn it was charged to, usage unknown.
    anchor = store.conversation(OWNER, chat["id"])["tasks"][-2]
    anchor = store.task(OWNER, anchor["id"])
    assert purposes(anchor) == ["history_summary"]
    assert [call["status"] for call in anchor["usage"]] == ["unknown"]
    assert "context_compaction_discarded" in [e["kind"] for e in anchor["events"]]

    # The turn that started is not held back by the dropped attempt: it summarises as before.
    turn = run_turn(engine, store, started["task"])
    assert turn["status"] == "completed"
    assert purposes(turn) == ["history_summary", "turn"]
    assert summary_state(store, bot)["revision"] == 1


def test_a_busy_owl_is_tried_again_a_bounded_number_of_times(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    model = Model()
    engine = runtime(store, model)
    queued = store.enqueue(
        OWNER, chat["id"], TaskInput(message="Και μετά;", request_id=uuid4().hex)
    )
    engine.warm_up.ended(bot["id"], now=time.time() - WARM_SECONDS - 1)
    now = later()
    for _ in range(WARM_TRIES + 1):
        assert engine.warm_up.due(now)
        assert engine.warm_up_pass(now=now) == []
        now += context_summary.WARM_RETRY_SECONDS + 1
    assert not engine.warm_up.due(now + 3600)
    assert model.kinds == []
    # The queued turn ends; that arms it again.
    assert store.claim(OWNER)["id"] == queued["id"]
    store.finish(OWNER, queued["id"], "completed", content="Εντάξει.")
    done = warm(engine, bot)
    assert "written" in done[0]["history_summary"]


def test_the_idle_summary_keeps_calls_for_the_persons_next_turn(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    model = Model()
    limits = RuntimeLimits(context_tokens=12000, history_tokens=1500, max_calls=KEEP_FOR_PERSON)
    engine = runtime(store, model, limits)
    assert warm(engine, bot) == [{"history_summary": ["allowance"]}]
    assert model.kinds == [] and store.usage(OWNER)["calls"] == 0


# ---------------------------------------------------------------------------------------------
# Languages and models


@pytest.mark.parametrize("language", ["greek", "chinese"])
@pytest.mark.parametrize("model_name", ["model-small-window", "model-other-small-window"])
def test_greek_and_chinese_chats_keep_their_language(tmp_path, language, model_name):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store, language)
    text, summary = CHATS[language]
    model = Model(summary, model=model_name)
    engine = runtime(store, model)
    done = warm(engine, bot)
    assert "written" in done[0]["history_summary"]
    request = model.requests[0]
    # The model is asked to keep the chat's language and reads its words as written.
    assert "in the language of the messages it cites" in request[0]["content"]
    assert text.format(i=0)[:40] in request[1]["content"]
    assert json.loads(summary_state(store, bot)["content"])["items"][0]["text"] == summary
    turn = run_turn(engine, store, next_turn(store, chat))
    assert purposes(turn) == ["turn"]
    assert summary in json.dumps(model.requests[-1], ensure_ascii=False)


def test_two_models_with_different_context_windows_qualify_differently(tmp_path):
    small, large = Store(tmp_path / "small.db"), Store(tmp_path / "large.db")
    small_bot, _ = owl(small, "chinese")
    large_bot, large_chat = owl(large, "chinese")
    small_model = Model(CHATS["chinese"][1], model="model-small-window")
    large_model = Model(CHATS["chinese"][1], model="model-large-window")
    small_done = warm(runtime(small, small_model, SMALL_WINDOW), small_bot)
    large_engine = runtime(large, large_model, LARGE_WINDOW)
    large_done = warm(large_engine, large_bot)
    assert "written" in small_done[0]["history_summary"]
    assert large_done == [{"history_summary": ["not_needed"]}]
    assert small_model.kinds[0] == "summary" and large_model.kinds == []
    assert small.usage(OWNER)["calls"] >= 1 and large.usage(OWNER)["calls"] == 0
    # Its whole chat fits the larger window: its next turn needs no summary either.
    turn = run_turn(large_engine, large, next_turn(large, large_chat))
    assert purposes(turn) == ["turn"]


# ---------------------------------------------------------------------------------------------
# No stale summary


def test_a_summary_of_a_chat_that_changed_while_it_was_written_is_not_kept(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store, "chinese")

    def quick_turn():
        # A turn starts and ends while the provider works (another place, or a very short one).
        task = next_turn(store, chat, "顺便说一下，预算改成十万。")
        store.finish(OWNER, task["id"], "completed", content="好的。")
        return False

    model = Model(CHATS["chinese"][1], during=quick_turn)
    engine = runtime(store, model)
    engine.warm_up.ended(bot["id"], now=time.time() - WARM_SECONDS - 1)
    job = engine.warm_up.claim(later())
    client, limits = engine.turn_model(job.anchor_task_id, job.bot_id)
    # A token that never fires: only the check before writing stands between the two.
    token = EventCancellationToken(threading.Event())
    assert engine.warm_up.run(job, client, limits, token) == {"history_summary": ["discarded"]}
    assert summary_state(store, bot)["revision"] == 0
    # The next turn summarises the chat as it is now.
    turn = run_turn(engine, store, next_turn(store, chat))
    assert purposes(turn) == ["history_summary", "turn"]
    payload = json.loads(model.requests[-2][1]["content"])
    assert summary_state(store, bot)["revision"] == 1
    assert payload["settled_tasks"]


@pytest.mark.parametrize("change", ["memory", "note"])
def test_a_summary_written_before_memory_or_notes_changed_is_not_used(tmp_path, change):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store, "greek")
    model = Model(CHATS["greek"][1])
    engine = runtime(store, model)
    assert "written" in warm(engine, bot)[0]["history_summary"]
    if change == "memory":
        store.save_memory(
            OWNER, bot["id"], MemoryInput(title="Ύφος", content="Απαντάς σύντομα, στα ελληνικά.")
        )
    else:
        store.save_note(
            OWNER, bot["id"], NoteInput(title="Έργο", content="Νέα προθεσμία: Δευτέρα.")
        )
    task = next_turn(store, chat)
    context = engine._context(task, mark_history=True)
    assert not any("_bot_summary" in m for m in context)
    # Its next turn writes the summary again, for the memory and notes it reads.
    turn = run_turn(engine, store, task)
    assert purposes(turn)[0] == "history_summary"


def test_turns_after_the_idle_summary_are_read_word_for_word(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    model = Model()
    engine = runtime(store, model)
    assert "written" in warm(engine, bot)[0]["history_summary"]
    boundary = summary_state(store, bot)["through_id"]
    newer = next_turn(store, chat, "Also: the client renamed the file to budget-final.xlsx.")
    store.finish(OWNER, newer["id"], "completed", content="Noted.")
    task = next_turn(store, chat)
    context = engine._context(task, mark_history=True)
    assert any("_bot_summary" in m for m in context)
    assert any(m.get("content") == newer["message"] for m in context)
    assert boundary != newer["id"]


# ---------------------------------------------------------------------------------------------
# Memories indexed when saved


def test_memories_saved_in_a_turn_are_indexed_before_the_next_turn(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store, "greek", turns=2)
    task = next_turn(store, chat, "Κράτα αυτά για αργότερα.")
    for index in range(40):
        store.save_memory(
            OWNER,
            bot["id"],
            MemoryInput(
                title=f"Γεγονός {index}", content=f"Λεπτομέρεια για το θέμα {index}. " * 12
            ),
            source_task_id=task["id"],
            saved_by_owl=True,
        )
    store.finish(OWNER, task["id"], "completed", content="Τα κράτησα.")
    model = Model()
    engine = runtime(store, model)
    done = warm(engine, bot)
    assert done == [{"history_summary": ["not_needed"]}]
    assert set(model.kinds) == {"index"}
    assert all(m["context_scope"] == "general" for m in store.list_memories(OWNER, bot["id"]))
    indexed = store.task(OWNER, task["id"])
    assert set(purposes(indexed)) == {"memory_index"}
    turn = run_turn(engine, store, next_turn(store, chat))
    assert purposes(turn) == ["turn"]


def test_a_turn_that_starts_while_memories_are_indexed_indexes_them_itself(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store, "chinese", turns=2)
    for index in range(40):
        store.save_memory(
            OWNER,
            bot["id"],
            MemoryInput(title=f"事实 {index}", content=f"关于主题 {index} 的细节。" * 30),
        )
    started = {}

    def person_writes():
        started["task"] = next_turn(store, chat)
        return True

    model = Model(during=person_writes, during_kind="index")
    engine = runtime(store, model)
    assert warm(engine, bot) == [{"cancelled": True}]
    assert model.kinds == ["index"]
    assert not any(m["context_scope"] for m in store.list_memories(OWNER, bot["id"]))
    turn = run_turn(engine, store, started["task"])
    assert turn["status"] == "completed"
    assert purposes(turn)[0] == "memory_index" and purposes(turn)[-1] == "turn"
    assert all(m["context_scope"] == "general" for m in store.list_memories(OWNER, bot["id"]))


def test_memories_kept_after_the_chat_went_quiet_are_made_ready_too(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store, "chinese", turns=1)
    said = "顺便说一下，我每周五在家办公，所以周五的会议请安排在线上。"
    task = next_turn(store, chat, said)
    store.finish(OWNER, task["id"], "completed", content="好的。", checked=True)
    fact = {"title": "工作安排", "content": "用户每周五在家办公。", "quote": "我每周五在家办公"}
    model = Model(facts=[fact], model="model-other-small-window")
    engine = runtime(store, model)
    quiet = time.time() + QUIET_SECONDS + 1
    engine.capture_memories(now=quiet)
    assert [m["content"] for m in store.list_memories(OWNER, bot["id"])] == [fact["content"]]
    assert engine.warm_up.due(quiet + WARM_SECONDS + 1)


# ---------------------------------------------------------------------------------------------
# The memory lane does it between the person's turns


def caught_up(engine, bot, chat, model):
    following = {"id": "", "created_at": math.inf, "bot_id": bot["id"], "chat_id": chat["id"]}
    return engine.context_summary._plan(following, model, SMALL_WINDOW) is None


def test_the_memory_lane_makes_the_chat_ready_between_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(context_summary, "WARM_SECONDS", 0.2)
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store, "greek")
    model = Model(CHATS["greek"][1], model="model-other-small-window")
    engine = runtime(store, model)

    def wait_until(condition):
        deadline = time.monotonic() + 20
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert condition()

    engine.start()
    try:
        first = store.enqueue(
            OWNER, chat["id"], TaskInput(message="Πού είμαστε;", request_id=uuid4().hex)
        )
        engine.wake()
        wait_until(lambda: store.task(OWNER, first["id"])["status"] == "completed")
        # Nothing was ready yet: that turn summarised first. Then the lane caught up.
        assert purposes(store.task(OWNER, first["id"]))[:2] == ["history_summary", "turn"]
        wait_until(lambda: caught_up(engine, bot, chat, model))
        assert purposes(store.task(OWNER, first["id"])).count("history_summary") >= 2
        second = store.enqueue(
            OWNER, chat["id"], TaskInput(message="Και τώρα;", request_id=uuid4().hex)
        )
        engine.wake()
        wait_until(lambda: store.task(OWNER, second["id"])["status"] == "completed")
    finally:
        engine.stop()
    assert purposes(store.task(OWNER, second["id"])) == ["turn"]


# ---------------------------------------------------------------------------------------------
# Memories the person changes in the app


def app_for(tmp_path, model):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture", owner=OWNER
    )
    app = create_app(settings, client_factory=lambda _: model)
    app.state.runtime.limits = SMALL_WINDOW
    # Without its lifespan the runtime's lanes do not run: the test takes each pass itself.
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + settings.token
    return app.state.store, app.state.runtime, client


@pytest.mark.parametrize("language", ["greek", "chinese"])
def test_memories_added_in_the_app_are_indexed_before_the_next_turn(tmp_path, language):
    model = Model()
    store, engine, client = app_for(tmp_path, model)
    bot, chat = owl(store, language, turns=2)
    for index in range(40):
        added = client.post(
            f"/v1/bots/{bot['id']}/memories",
            json={"title": f"{CHATS[language][1][:20]} {index}", "content": CHATS[language][0]},
        )
        assert added.status_code == 201
    assert engine.warm_up.due(later())
    assert engine.warm_up_pass(now=later()) == [{"history_summary": ["not_needed"]}]
    assert set(model.kinds) == {"index"}
    assert all(m["context_scope"] == "general" for m in store.list_memories(OWNER, bot["id"]))
    turn = run_turn(engine, store, next_turn(store, chat))
    # The turn starts its own work at once (the app also checks the reply after it).
    assert purposes(turn)[0] == "turn" and "memory_index" not in purposes(turn)


@pytest.mark.parametrize("change", ["edit", "approve", "undo", "delete"])
def test_every_memory_change_in_the_app_is_made_ready_while_idle(tmp_path, change):
    store, engine, client = app_for(tmp_path, Model())
    bot, chat = owl(store, "greek", turns=1)
    task = next_turn(store, chat, "Κράτα το.")
    saved = store.save_memory(
        OWNER,
        bot["id"],
        MemoryInput(title="Παράδοση", content="Η παράδοση είναι την Παρασκευή."),
        source_task_id=task["id"],
        saved_by_owl=True,
    )
    # One the owl only proposed, for the person to approve.
    proposed = store.save_memory(
        OWNER,
        bot["id"],
        MemoryInput(title="Ώρα", content="Στις 10:00."),
        source_task_id=task["id"],
    )
    store.finish(OWNER, task["id"], "completed", content="Το κράτησα.")
    engine.warm_up.started(bot["id"])  # nothing is due before the change
    assert not engine.warm_up.due(later())
    path = f"/v1/bots/{bot['id']}/memories/{saved['id']}"
    if change == "edit":
        response = client.put(path, json={"title": "Παράδοση", "content": "Την Πέμπτη."})
    elif change == "approve":
        response = client.patch(
            f"/v1/bots/{bot['id']}/memories/{proposed['id']}", json={"status": "active"}
        )
    elif change == "undo":
        response = client.post(path + "/undo")
    else:
        response = client.delete(path)
    assert response.status_code < 300, response.text
    assert engine.warm_up.due(later())
