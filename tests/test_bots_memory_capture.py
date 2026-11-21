"""Memory after the chat (memory_capture.py).

Once an owl's own chat has been quiet a while after a finished, checked turn, the service reads
the person's words in the turns since the last read with one bounded model call, without tools,
and keeps a durable fact only when it quotes the person's own words and isn't saved already.
Updates go through the replace path, everything is kept with Undo, the read is charged like the
owl's other calls, and it never runs under a turn. Real store and runtime; scripted models;
Greek and Chinese; two models.
"""

from __future__ import annotations

import json
import os
import threading
import time
from uuid import uuid4

import pytest

from alysis_code.bots import memory_capture
from alysis_code.bots import runtime as runtime_module
from alysis_code.bots.memory_capture import (
    KEEP_FOR_PERSON,
    MAX_FACTS,
    MAX_TURNS,
    MESSAGE_CHARACTERS,
    QUIET_SECONDS,
    MemoryCapture,
    quoted_in,
    same_fact,
)
from alysis_code.bots.models import BotInput, BotOrganizeInput, FileInput, MemoryInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Store
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

OWNER = "alice"  # as in test_bots_owl_move, whose move helpers a test here shares

GREEK_MESSAGE = "Παρεμπιπτόντως, είμαι χορτοφάγος, οπότε μη μου προτείνεις κρέας. Τι να μαγειρέψω;"
GREEK_FACT = {
    "title": "Διατροφή",
    "content": "Ο χρήστης είναι χορτοφάγος.",
    "quote": "είμαι χορτοφάγος",
}
CHINESE_MESSAGE = "顺便说一下，我每周五在家办公，所以周五的会议请安排在线上。帮我看看下周的安排。"
CHINESE_FACT = {
    "title": "工作安排",
    "content": "用户每周五在家办公，周五的会议安排在线上。",
    "quote": "我每周五在家办公",
}
CASES = {"greek": (GREEK_MESSAGE, GREEK_FACT), "chinese": (CHINESE_MESSAGE, CHINESE_FACT)}


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("ALYSIS_BOTS_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set disposable PostgreSQL 18 DSN for shared memory capture checks")
        shared = PostgresStore(
            dsn,
            objects=LocalObjectStorage(tmp_path / "objects"),
            state_directory=tmp_path / "state",
        )
        yield shared
        shared.close()
    else:
        yield Store(tmp_path / "bots.db")


class Reader:
    """The model that reads the quiet chat. Two models: one answers with plain JSON, the other
    (a small model) wraps it in a code fence. Neither offers tool calling: the read needs none."""

    supports_tool_calling = False

    def __init__(self, facts=(), *, model="reader-large", fenced=False, during=None, error=None):
        self.facts, self.model, self.fenced = list(facts), model, fenced
        self.during, self.error = during, error
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        token = kwargs["cancellation_token"]
        if self.during is not None:
            self.during()
            # A real client watches the token while it waits for the provider.
            if token.wait(timeout=5):
                raise CooperativeCancellationError()
        if self.error is not None:
            raise self.error
        content = json.dumps({"facts": self.facts}, ensure_ascii=False)
        if self.fenced:
            content = "```json\n" + content + "\n```"
        return LLMResponse(content, [], {}, usage=LLMUsage(120, 30, 150))

    def payload(self, index=-1):
        return json.loads(self.requests[index]["messages"][1]["content"])

    def text(self, index=-1):
        return json.dumps(self.requests[index]["messages"], ensure_ascii=False)


@pytest.fixture(params=["reader-large", "reader-small"])
def model(request):
    return {"model": request.param, "fenced": request.param == "reader-small"}


def owl(store, **setup):
    bot = store.save_bot(OWNER, BotInput(name="Γλαύκα", **setup))
    return bot, store.create_chat(OWNER, bot["id"], "Συνομιλία")


def turn(store, chat, text, reply="Εντάξει.", *, checked=True, request_id=None):
    """A turn of the person's: their message, and the owl's reply that passed its check."""
    task = store.enqueue(
        OWNER, chat["id"], TaskInput(message=text, request_id=request_id or uuid4().hex)
    )
    assert store.claim(OWNER)["id"] == task["id"]
    store.finish(OWNER, task["id"], "completed", content=reply, checked=checked)
    return store.task(OWNER, task["id"])


def runtime(store, reader, limits=None):
    return BotRuntime(store, OWNER, lambda _: reader, limits=limits)


def quiet():
    """A moment after the chat has been quiet long enough."""
    return time.time() + QUIET_SECONDS + 1


def memories(store, bot):
    return store.list_memories(OWNER, bot["id"], for_person=True)


def changes(store, bot):
    return [(e["action"], e["memory_id"]) for e in store.memory_changes(OWNER, bot["id"])["events"]]


def provider_calls(store, task_id):
    with store.connect() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT model,status FROM provider_calls WHERE task_id=?", (task_id,)
            )
        ]


def events(store, task_id, kind):
    return [e["data"] for e in store.task(OWNER, task_id)["events"] if e["kind"] == kind]


def state(store, bot):
    with store.connect() as db:
        row = db.execute(
            "SELECT * FROM memory_capture_state WHERE bot_id=?", (bot["id"],)
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------------------------
# What the person says in passing is kept, with its quote, in any language


@pytest.mark.parametrize("language", ["greek", "chinese"])
def test_a_preference_stated_in_passing_is_kept_after_the_chat_goes_quiet(store, model, language):
    message, fact = CASES[language]
    bot, chat = owl(store)
    task = turn(store, chat, message, reply="Ορίστε μερικές ιδέες.")
    reader = Reader([{**fact, "message": "1"}], **model)
    rt = runtime(store, reader)
    # Not while the chat may still go on.
    assert rt.capture_memories(now=time.time()) == [] and reader.requests == []
    [done] = rt.capture_memories(now=quiet())
    assert done["kept"] and done["dropped"]["not_quoted"] == 0
    [saved] = memories(store, bot)
    assert (saved["title"], saved["content"]) == (fact["title"], fact["content"])
    # Kept like an owl's own save: at once, from the turn it quotes, so it shows under that
    # reply with Undo, and marked as kept after the chat.
    assert saved["status"] == "active" and saved["source_task_id"] == task["id"]
    assert saved["can_undo"] is True and saved["captured"] is True
    assert ("captured", saved["id"]) in changes(store, bot)
    # One bounded call on the owl's model, without tools, of the person's words only.
    [request] = reader.requests
    assert request["tools"] is None and request["max_tokens"] <= 4096
    assert reader.payload()["person_messages"] == [{"id": "1", "text": message}]
    assert "Ορίστε μερικές ιδέες" not in reader.text()
    assert provider_calls(store, task["id"]) == [{"model": model["model"], "status": "completed"}]
    assert store.task(OWNER, task["id"])["calls"] == 1
    assert events(store, task["id"], "memory_captured")[0]["kept"] == [saved["id"]]
    # Undo forgets it, as for any memory an owl kept by itself.
    store.undo_memory(OWNER, bot["id"], saved["id"])
    assert memories(store, bot) == []
    # The turns were read: a later quiet spell reads nothing again.
    assert rt.capture_memories(now=quiet() + 600) == [] and len(reader.requests) == 1


def test_a_quote_matches_across_case_spacing_and_accents_but_never_another_text():
    message = "Παρεμπιπτόντως,  ΕΙΜΑΙ   χορτοφάγος από το 2019."
    assert quoted_in("είμαι χορτοφάγος", [message]) == 0
    # A model that drops the accents still quotes the person's words.
    assert quoted_in("ειμαι χορτοφαγος", ["άλλο", message]) == 1
    assert quoted_in("我每周五在家办公", [CHINESE_MESSAGE]) == 0
    assert quoted_in("我每周四在家办公", [CHINESE_MESSAGE]) is None
    # Too short to say anything on its own, in any script.
    assert quoted_in("ναι", ["ναι, σωστά"]) is None and quoted_in("好的", ["好的"]) is None
    assert same_fact("Ο χρήστης είναι  χορτοφάγος.", "ο χρήστης ΕΙΝΑΙ χορτοφάγος.")
    assert not same_fact("Ο χρήστης είναι χορτοφάγος.", "Ο χρήστης τρώει κρέας.")


def test_a_fact_whose_quote_is_not_the_persons_own_words_is_dropped(store, model):
    bot, chat = owl(store)
    task = turn(store, chat, GREEK_MESSAGE, reply="Θα σου στείλω συνταγές με ψάρι αύριο.")
    reader = Reader(
        [
            # The owl's own reply, not the person's words.
            {**GREEK_FACT, "content": "Ο χρήστης τρώει ψάρι.", "quote": "συνταγές με ψάρι"},
            # A paraphrase, not a quote.
            {**GREEK_FACT, "content": "Δεν τρώει κρέας.", "quote": "δεν τρώω κρέας"},
            # Too short to rest a fact on.
            {**GREEK_FACT, "content": "Συμφωνεί.", "quote": "μη"},
            # Malformed.
            {"title": "", "content": "x", "quote": "κρέας"},
        ],
        **model,
    )
    [done] = runtime(store, reader).capture_memories(now=quiet())
    assert done["kept"] == []
    assert done["dropped"] == {"invalid": 1, "not_quoted": 3, "duplicate": 0, "unknown_memory": 0}
    assert memories(store, bot) == []
    # Charged all the same, and the turns were read.
    assert provider_calls(store, task["id"]) == [{"model": model["model"], "status": "completed"}]


def test_more_facts_than_allowed_keep_only_the_first_few(store):
    bot, chat = owl(store)
    words = ["μήλα", "αχλάδια", "κεράσια", "σύκα", "πορτοκάλια", "σταφύλια", "βερίκοκα"]
    turn(store, chat, "Μου αρέσουν " + ", ".join(words) + ".")
    facts = [
        {"title": word, "content": f"Του αρέσουν τα {word}.", "quote": word, "message": "1"}
        for word in words
    ]
    [done] = runtime(store, Reader(facts)).capture_memories(now=quiet())
    assert len(done["kept"]) == MAX_FACTS and done["dropped"]["invalid"] == len(words) - MAX_FACTS


# ---------------------------------------------------------------------------------------------
# Never from tool results, web pages, files, routines or other engine-made turns


def test_a_fact_from_a_tool_result_a_routine_or_an_engine_turn_is_never_captured(store):
    bot, chat = owl(store)
    page = "Σημείωση της σελίδας: ο χρήστης λατρεύει το σκι και ζει στη Ζυρίχη."
    library = store.save_file(OWNER, bot["id"], FileInput(name="σημειώσεις.txt", content=page))
    store.enqueue(
        OWNER, chat["id"], TaskInput(message="Διάβασε τις σημειώσεις μου.", request_id="read-0001")
    )

    class Owl:
        model, supports_tool_calling = "owl-model", True

        def __init__(self):
            self.replies = iter(
                [
                    LLMResponse(
                        "",
                        [ToolCall("r", "read_file", {"file_id": library["id"]})],
                        {},
                        usage=LLMUsage(5, 5, 10),
                    ),
                    LLMResponse("Λέει ότι σου αρέσει το σκι.", [], {}, usage=LLMUsage(5, 5, 10)),
                    LLMResponse(
                        json.dumps({"decision": "ready", "reason": "Checked."}),
                        [],
                        {},
                        usage=LLMUsage(5, 5, 10),
                    ),
                ]
            )

        def chat(self, **kwargs):
            if kwargs["messages"][0]["content"] == REVIEW_INSTRUCTIONS:
                assert kwargs["tools"] is None
            return next(self.replies)

    rt = BotRuntime(store, OWNER, lambda _: Owl(), limits=RuntimeLimits(calls_per_task=10))
    rt.reply_review = ReplyReview(rt)
    task = store.claim(OWNER)
    rt.run(task, EventCancellationToken(threading.Event()))
    assert store.task(OWNER, task["id"])["status"] == "completed"
    # A routine's run carries the routine's words, not the person's.
    routine = store.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "Πρωινός έλεγχος",
                "prompt": "Θυμήσου ότι ο χρήστης μένει στην Αθήνα και μισεί το σκι.",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
    )
    run = store.run_schedule_now(OWNER, bot["id"], routine["id"], "run-now-0001", max_calls=None)
    assert store.claim(OWNER)["id"] == run["task_id"]
    store.finish(OWNER, run["task_id"], "completed", content="Έγινε.")
    reader = Reader(
        [
            {"title": "Σκι", "content": "Λατρεύει το σκι.", "quote": "λατρεύει το σκι"},
            {"title": "Πόλη", "content": "Μένει στην Αθήνα.", "quote": "μένει στην Αθήνα"},
        ]
    )
    [done] = runtime(store, reader).capture_memories(now=quiet())
    # Only the person's own message was read: not the file, the reply or the routine.
    assert reader.payload()["person_messages"] == [
        {"id": "1", "text": "Διάβασε τις σημειώσεις μου."}
    ]
    assert "Ζυρίχη" not in reader.text() and "Αθήνα" not in reader.text()
    assert done["dropped"]["not_quoted"] == 2 and memories(store, bot) == []


def test_a_turn_whose_reply_was_not_checked_does_not_start_a_read(store):
    bot, chat = owl(store)
    reader = Reader([])
    # An owl's first unchecked turn doesn't start one, nor make a cursor.
    turn(store, chat, "Γεια σου.", checked=False)
    assert runtime(store, reader).capture_memories(now=quiet()) == []
    assert reader.requests == [] and state(store, bot) is None
    turn(store, chat, "Καλημέρα.")
    runtime(store, reader).capture_memories(now=quiet())
    # Its cursor starts at its first checked turn: earlier history is never read.
    assert [m["text"] for m in reader.payload()["person_messages"]] == ["Καλημέρα."]
    turn(store, chat, GREEK_MESSAGE, checked=False)
    assert runtime(store, reader).capture_memories(now=quiet()) == [] and len(reader.requests) == 1
    # The next checked turn reads both: the person's words were theirs either way.
    turn(store, chat, CHINESE_MESSAGE)
    runtime(store, reader).capture_memories(now=quiet())
    assert [m["text"] for m in reader.payload()["person_messages"]] == [
        GREEK_MESSAGE,
        CHINESE_MESSAGE,
    ]


# ---------------------------------------------------------------------------------------------
# The memory switch, a running turn and the bounds


def test_with_memory_off_nothing_is_read(store):
    bot, chat = owl(store, memory_enabled=False)
    turn(store, chat, GREEK_MESSAGE)
    reader = Reader([GREEK_FACT])
    assert runtime(store, reader).capture_memories(now=quiet()) == []
    assert reader.requests == [] and state(store, bot) is None
    # Switched off after the turn: the read that was due doesn't happen either.
    other, other_chat = owl(store)
    turn(store, other_chat, CHINESE_MESSAGE)
    store.save_bot(OWNER, BotInput(name="Γλαύκα", memory_enabled=False), other["id"])
    assert runtime(store, reader).capture_memories(now=quiet()) == []
    assert reader.requests == [] and memories(store, other) == []


def test_a_running_turn_makes_the_read_wait_and_the_next_read_covers_both(store):
    bot, chat = owl(store)
    first = turn(store, chat, GREEK_MESSAGE)
    second = store.enqueue(
        OWNER, chat["id"], TaskInput(message=CHINESE_MESSAGE, request_id="second-0001")
    )
    assert store.claim(OWNER)["id"] == second["id"]
    reader = Reader([{**GREEK_FACT, "message": "1"}, {**CHINESE_FACT, "message": "2"}])
    rt = runtime(store, reader)
    assert rt.capture_memories(now=quiet()) == [] and reader.requests == []
    store.finish(OWNER, second["id"], "completed", content="好的。", checked=True)
    # Quiet is counted from the turn that just finished.
    assert rt.capture_memories(now=time.time() + 5) == []
    [done] = rt.capture_memories(now=quiet())
    assert len(done["kept"]) == 2
    assert [m["text"] for m in reader.payload()["person_messages"]] == [
        GREEK_MESSAGE,
        CHINESE_MESSAGE,
    ]
    by_content = {m["content"]: m["source_task_id"] for m in memories(store, bot)}
    assert by_content == {GREEK_FACT["content"]: first["id"], CHINESE_FACT["content"]: second["id"]}
    # Charged once, to the newest turn read.
    assert provider_calls(store, first["id"]) == []
    assert len(provider_calls(store, second["id"])) == 1


def test_a_message_sent_to_a_running_turn_is_read_as_the_persons_words_of_that_turn(store):
    from alysis_code.bots.queued_messages import QueuedMessageChange, QueuedMessages
    from alysis_code.bots.steering import Steering

    bot, chat = owl(store)
    running = store.enqueue(
        OWNER, chat["id"], TaskInput(message="Βρες μου μια συνταγή.", request_id="steered-0001")
    )
    assert store.claim(OWNER)["id"] == running["id"]
    sent = store.enqueue(
        OWNER, chat["id"], TaskInput(message=GREEK_MESSAGE, request_id="steer-0001")
    )
    answer = QueuedMessages(store, OWNER).change(
        sent["id"], QueuedMessageChange(action="steer", expected_revision=0)
    )
    assert answer["outcome"] == "steering"
    assert Steering(store, OWNER).take(store.task(OWNER, running["id"]))
    store.finish(OWNER, running["id"], "completed", content="Ορίστε.", checked=True)
    reader = Reader([{**GREEK_FACT, "message": "2"}])
    [done] = runtime(store, reader).capture_memories(now=quiet())
    # One turn: its own message, then the one the person sent to it, in order.
    assert [m["text"] for m in reader.payload()["person_messages"]] == [
        "Βρες μου μια συνταγή.",
        GREEK_MESSAGE,
    ]
    assert done["turns"] == 1
    [saved] = memories(store, bot)
    assert saved["source_task_id"] == running["id"] and saved["captured"] is True


def test_check_backs_quiet_routine_runs_and_restarts_never_read_anything_twice_or_unasked(store):
    from alysis_code.bots.check_backs import CheckBackRequest, CheckBacks

    bot, chat = owl(store)
    reader = Reader([])
    rt = runtime(store, reader)
    # A turn a restart cut off starts again once; its words are read once, when it ends.
    cut = store.enqueue(OWNER, chat["id"], TaskInput(message=GREEK_MESSAGE, request_id="cut-0001"))
    assert store.claim(OWNER)["id"] == cut["id"]
    assert store.restart_cut(OWNER, cut["id"])
    assert state(store, bot) is None
    assert store.claim(OWNER)["id"] == cut["id"]
    now = [time.time()]
    keep = CheckBacks(
        store, OWNER, clock=lambda: now[0], read_page=lambda _: ("", True), run_check=lambda w: w()
    )
    keep.set(store.task(OWNER, cut["id"]), CheckBackRequest(note="Ξανά.", after_seconds=600))
    store.finish(OWNER, cut["id"], "completed", content="Εντάξει.", checked=True)
    rt.capture_memories(now=quiet())
    assert [m["text"] for m in reader.payload()["person_messages"]] == [GREEK_MESSAGE]
    # The turn the owl started by checking back, and a routine's run that found nothing new,
    # carry no words of the person's: neither starts a read.
    now[0] += 600
    assert keep.tick() == 1
    checking = store.claim(OWNER)
    assert checking["config"].get("check_back")
    store.finish(OWNER, checking["id"], "completed", content="Τίποτα νέο.", checked=True)
    routine = store.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate(
            {"name": "Έλεγχος", "prompt": "Δες τα νέα.", "timing": {"kind": "interval", "every_minutes": 60}}
        ),
    )  # fmt: skip
    run = store.run_schedule_now(OWNER, bot["id"], routine["id"], "run-now-0002", max_calls=None)
    assert store.claim(OWNER)["id"] == run["task_id"]
    store.finish(OWNER, run["task_id"], "completed", nothing_new="Τίποτα καινούργιο.")
    assert state(store, bot)["due_at"] is None
    assert rt.capture_memories(now=quiet() + 600) == [] and len(reader.requests) == 1


def test_a_read_is_bounded_to_the_newest_turns_and_words(store):
    bot, chat = owl(store)
    long = "Μήνυμα " + "λ" * (MESSAGE_CHARACTERS + 500)
    for number in range(MAX_TURNS + 1):
        turn(store, chat, f"Μήνυμα {number}")
    turn(store, chat, long)
    reader = Reader([])
    [done] = runtime(store, reader).capture_memories(now=quiet())
    sent = [m["text"] for m in reader.payload()["person_messages"]]
    assert len(sent) == MAX_TURNS and sent[0] == "Μήνυμα 2"
    assert sent[-1] == long[:MESSAGE_CHARACTERS]
    assert done["turns_left_out"] == 2


# ---------------------------------------------------------------------------------------------
# Never saved twice; an update goes through the replace path


def test_a_fact_already_saved_is_not_saved_again(store, model):
    bot, chat = owl(store)
    store.save_memory(
        OWNER, bot["id"], MemoryInput(title="Φαγητό", content="ο χρήστης  ΕΙΝΑΙ χορτοφάγος")
    )
    turn(store, chat, GREEK_MESSAGE)
    reader = Reader([{**GREEK_FACT, "message": "1"}, {**GREEK_FACT, "message": "1"}], **model)
    [done] = runtime(store, reader).capture_memories(now=quiet())
    assert done["kept"] == [] and done["dropped"]["duplicate"] == 2
    assert [m["content"] for m in memories(store, bot)] == ["ο χρήστης  ΕΙΝΑΙ χορτοφάγος"]
    # The read showed what is saved, so the model can leave it out.
    assert reader.payload()["saved_memories"][0]["content"] == "ο χρήστης  ΕΙΝΑΙ χορτοφάγος"


def test_an_update_replaces_the_memory_it_names_and_undo_brings_it_back(store, model):
    bot, chat = owl(store)
    old = store.save_memory(
        OWNER, bot["id"], MemoryInput(title="Γραφείο", content="Δουλεύει από το σπίτι τη Δευτέρα.")
    )
    task = turn(store, chat, "Από τώρα δουλεύω από το σπίτι την Παρασκευή, όχι τη Δευτέρα.")
    reader = Reader(
        [
            {
                "title": "Γραφείο",
                "content": "Δουλεύει από το σπίτι την Παρασκευή.",
                "quote": "δουλεύω από το σπίτι την Παρασκευή",
                "updates_memory_id": old["id"],
            },
            {
                "title": "Άγνωστο",
                "content": "Κάτι άλλο.",
                "quote": "όχι τη Δευτέρα",
                "updates_memory_id": "no-such-memory",
            },
        ],
        **model,
    )
    [done] = runtime(store, reader).capture_memories(now=quiet())
    assert done["replaced"] == [old["id"]] and done["dropped"]["unknown_memory"] == 1
    [current] = memories(store, bot)
    assert current["content"] == "Δουλεύει από το σπίτι την Παρασκευή."
    assert current["replaces_memory_id"] == old["id"] and current["source_task_id"] == task["id"]
    assert ("superseded", old["id"]) in changes(store, bot)
    assert store.undo_memory(OWNER, bot["id"], current["id"])["restored_memory_id"] == old["id"]
    assert [m["content"] for m in memories(store, bot)] == ["Δουλεύει από το σπίτι τη Δευτέρα."]


# ---------------------------------------------------------------------------------------------
# A new message, archiving, deleting or switching memory off cancels a read


@pytest.mark.parametrize("change", ["message", "archive", "delete", "memory_off"])
def test_a_read_is_cancelled_when_the_owl_changes_under_it(store, change):
    bot, chat = owl(store)
    turn(store, chat, GREEK_MESSAGE)
    before = state(store, bot)["through_seq"]

    def meanwhile():
        if change == "message":
            store.enqueue(
                OWNER, chat["id"], TaskInput(message="Και κάτι ακόμα.", request_id="message-0002")
            )
        elif change == "archive":
            store.organize_bot(OWNER, bot["id"], BotOrganizeInput(archived=True))
        elif change == "delete":
            store.delete_bot(OWNER, bot["id"])
        else:
            store.save_bot(OWNER, BotInput(name="Γλαύκα", memory_enabled=False), bot["id"])

    reader = Reader([{**GREEK_FACT, "message": "1"}], during=meanwhile)
    [done] = runtime(store, reader).capture_memories(now=quiet())
    assert done == {"reason": "cancelled"}
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
        [call] = db.execute("SELECT status FROM provider_calls").fetchall()
    # Usage of a stopped request is unknown, never free.
    assert call["status"] == "unknown"
    if change == "delete":
        return
    # Nothing moved on: the same turn is read again once the owl is back to normal.
    assert state(store, bot)["through_seq"] == before
    if change == "message":
        follow = store.claim(OWNER)
        store.finish(OWNER, follow["id"], "completed", content="Εντάξει.", checked=True)
    elif change == "archive":
        store.organize_bot(OWNER, bot["id"], BotOrganizeInput(archived=False))
    else:
        store.save_bot(OWNER, BotInput(name="Γλαύκα", memory_enabled=True), bot["id"])
    again = Reader([{**GREEK_FACT, "message": "1"}])
    [done] = runtime(store, again).capture_memories(now=quiet() + QUIET_SECONDS)
    assert GREEK_MESSAGE in [m["text"] for m in again.payload()["person_messages"]]
    assert [m["content"] for m in memories(store, bot)] == [GREEK_FACT["content"]]


def test_stopping_the_service_cancels_a_read_and_it_is_read_again_later(store):
    bot, chat = owl(store)
    turn(store, chat, GREEK_MESSAGE)
    rt = runtime(store, None)
    reader = Reader([GREEK_FACT], during=rt._stop.set)
    rt.client_factory = lambda _: reader
    assert rt.capture_memories(now=quiet()) == [{"reason": "cancelled"}]
    assert memories(store, bot) == [] and state(store, bot)["due_at"] is not None


# ---------------------------------------------------------------------------------------------
# Charged like the owl's other calls


def test_a_short_allowance_skips_the_read_and_keeps_the_turns_for_later(store):
    bot, chat = owl(store)
    task = turn(store, chat, GREEK_MESSAGE)
    used = store.usage(OWNER)["calls"]
    reader = Reader([{**GREEK_FACT, "message": "1"}])
    short = RuntimeLimits(max_calls=used + KEEP_FOR_PERSON)
    assert runtime(store, reader, short).capture_memories(now=quiet()) == [{"reason": "allowance"}]
    assert reader.requests == [] and provider_calls(store, task["id"]) == []
    assert events(store, task["id"], "memory_capture_ended") == [{"reason": "allowance"}]
    # A turn's own ceiling, when one is set, is respected too.
    thanks = turn(store, chat, "Ευχαριστώ.")
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET calls=3 WHERE id=?", (thanks["id"],))
    ceiling = RuntimeLimits(calls_per_task=3)
    assert runtime(store, reader, ceiling).capture_memories(now=quiet()) == [
        {"reason": "allowance"}
    ]
    assert reader.requests == []
    # With room, the next quiet spell reads the turns that waited.
    turn(store, chat, "Καληνύχτα.")
    roomy = RuntimeLimits(max_calls=used + KEEP_FOR_PERSON + 5)
    [done] = runtime(store, reader, roomy).capture_memories(now=quiet())
    assert done["kept"] and [m["text"] for m in reader.payload()["person_messages"]] == [
        GREEK_MESSAGE,
        "Ευχαριστώ.",
        "Καληνύχτα.",
    ]


def test_a_temporary_provider_failure_is_tried_once_more_later(store):
    bot, chat = owl(store)
    task = turn(store, chat, GREEK_MESSAGE)
    failing = Reader(error=TimeoutError("timed out"))
    assert runtime(store, failing).capture_memories(now=quiet()) == [{"reason": "provider_error"}]
    retry_at = state(store, bot)["due_at"]
    assert retry_at > time.time() + QUIET_SECONDS
    assert runtime(store, failing).capture_memories(now=retry_at + 1) == [
        {"reason": "provider_error"}
    ]
    # Not a third time until the person's next turn.
    assert state(store, bot)["due_at"] is None
    assert [c["status"] for c in provider_calls(store, task["id"])] == ["unknown", "unknown"]


# ---------------------------------------------------------------------------------------------
# A moved owl, a restart and the scheduler loop


def test_a_moved_owl_never_reads_its_old_turns_again(tmp_path):
    from test_bots_owl_move import copy_files, send_pages, staged

    from alysis_code.bots.owl_move import require_unchanged

    source = Store(tmp_path / "here" / "bots.sqlite3")
    target = Store(tmp_path / "there" / "bots.sqlite3")
    bot, chat = owl(source)
    turn(source, chat, GREEK_MESSAGE)
    # Read in its first place, then another turn there that was never read.
    runtime(source, Reader([GREEK_FACT])).capture_memories(now=quiet())
    turn(source, chat, "Και ξυπνάω στις 6 κάθε πρωί.")
    moves, move_id, pages = staged(source, target, bot["id"])
    send_pages(moves, move_id, pages)
    assert copy_files(source, target, bot["id"], moves.receipt(move_id), pages)
    done = moves.complete(move_id)
    source.delete_bot(
        OWNER, bot["id"], check=lambda db: require_unchanged(db, bot["id"], pages[0]["fingerprint"])
    )
    copy = done["owl"]
    moved_chat = target.list_chats(OWNER, copy["id"])[0]
    # The other place's clock ran an hour ahead: what it brought still counts as read.
    with target.connect(write=True) as db:
        db.execute("UPDATE tasks SET created_at=created_at+3600 WHERE bot_id=?", (copy["id"],))
    reader = Reader([CHINESE_FACT])
    assert runtime(target, reader).capture_memories(now=quiet()) == [] and not reader.requests
    turn(target, moved_chat, CHINESE_MESSAGE)
    [done] = runtime(target, reader).capture_memories(now=quiet())
    assert [m["text"] for m in reader.payload()["person_messages"]] == [CHINESE_MESSAGE]
    assert {m["content"] for m in memories(target, copy)} == {
        GREEK_FACT["content"],
        CHINESE_FACT["content"],
    }


def test_a_read_cut_off_by_a_restart_is_read_again_after_a_quiet_spell(store):
    bot, chat = owl(store)
    turn(store, chat, GREEK_MESSAGE)
    capture = MemoryCapture(store, OWNER)
    assert capture.claim(quiet()) is not None and state(store, bot)["due_at"] is None
    capture.recover()
    assert state(store, bot)["attempt"] is None
    [done] = runtime(store, Reader([GREEK_FACT])).capture_memories(now=quiet() + QUIET_SECONDS)
    assert done["kept"]


def test_an_existing_database_gains_the_capture_table_and_its_owls_start_fresh(tmp_path):
    import sqlite3

    from alysis_code.bots.schema import SCHEMA_VERSION

    path = tmp_path / "earlier.db"
    earlier = Store(path)
    bot, chat = owl(earlier)
    with earlier.connect(write=True) as db:
        db.execute("DROP TABLE memory_capture_state")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=46")
    store = Store(path)
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 50
    # Its earlier turns are history: only turns from now on are read.
    with store.connect(write=True) as db:
        db.execute("DELETE FROM memory_capture_state")
    old = store.enqueue(OWNER, chat["id"], TaskInput(message=GREEK_MESSAGE, request_id="old-0001"))
    store.claim(OWNER)
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='completed' WHERE id=?", (old["id"],))
        Store._event(db, old["id"], "completed", {})
    turn(store, chat, CHINESE_MESSAGE)
    reader = Reader([])
    runtime(store, reader).capture_memories(now=quiet())
    assert [m["text"] for m in reader.payload()["person_messages"]] == [CHINESE_MESSAGE]


def test_the_scheduler_loop_runs_the_read_in_the_memory_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_capture, "QUIET_SECONDS", 0.2)
    monkeypatch.setattr(runtime_module, "CAPTURE_CHECK_SECONDS", 0.1)
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    turn(store, chat, CHINESE_MESSAGE)
    reader = Reader([CHINESE_FACT], model="reader-small", fenced=True)
    rt = runtime(store, reader)
    rt.start()
    try:
        deadline = time.monotonic() + 20
        while not memories(store, bot) and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        rt.stop()
    [saved] = memories(store, bot)
    assert saved["content"] == CHINESE_FACT["content"] and saved["captured"] is True


# ---------------------------------------------------------------------------------------------
# Tidy-up: exact duplicates merge through the replace path, never deleted


def test_tidy_merges_exact_duplicates_and_undo_brings_the_copy_back(store):
    bot, chat = owl(store)
    task = turn(store, chat, GREEK_MESSAGE)
    mine = store.save_memory(OWNER, bot["id"], MemoryInput(title="Α", content="Πίνει τσάι."))
    # Two copies the owl kept from its turns (as during a turn, with Undo).
    with store.connect(write=True) as db:
        owls = [
            Store._keep_owl_memory(db, bot["id"], "Β", text, task["id"], None, time.time())
            for text in ("ΠΊΝΕΙ  τσάι.", "Πίνει τσάι.")
        ]
        Store._keep_owl_memory(db, bot["id"], "Γ", "Πίνει καφέ.", task["id"], None, time.time())
    capture = MemoryCapture(store, OWNER)
    # Never while a turn of the owl works.
    store.enqueue(OWNER, chat["id"], TaskInput(message="Γεια", request_id="busy-0001"))
    assert capture.tidy_due(time.time()) == 0
    store.finish(OWNER, store.claim(OWNER)["id"], "completed", content="Γεια.", checked=True)
    assert capture.tidy_due(time.time()) == 2
    left = memories(store, bot)
    # The person's own copy stays; the owl's copies are superseded in a chain, not deleted.
    assert sorted(m["content"] for m in left) == ["Πίνει καφέ.", "Πίνει τσάι."]
    assert next(m for m in left if m["content"] == "Πίνει τσάι.")["id"] == mine["id"]
    assert [a for a, i in changes(store, bot) if i in {o["id"] for o in owls}].count("merged") == 2
    # Once a day.
    assert capture.tidy_due(time.time() + 60) == 0
    assert capture.waiting(time.time() + 60) is False
    # Deleting the one kept retires the copies with it: Undo can no longer bring them back.
    store.delete_item(OWNER, bot["id"], mine["id"], kind="memories")
    assert [m["content"] for m in memories(store, bot)] == ["Πίνει καφέ."]
    with store.connect() as db:
        statuses = {
            row[0]: (row[1], row[2])
            for row in db.execute(
                "SELECT id,status,content FROM memories WHERE bot_id=?", (bot["id"],)
            )
        }
    assert all(statuses[o["id"]] == ("rejected", "") for o in owls)


def test_undo_of_a_merged_owl_memory_brings_its_copy_back(store):
    bot, chat = owl(store)
    task = turn(store, chat, GREEK_MESSAGE)
    with store.connect(write=True) as db:
        first, second = (
            Store._keep_owl_memory(db, bot["id"], "Τσάι", "Πίνει τσάι.", task["id"], None, now)
            for now in (time.time(), time.time() + 1)
        )
    assert MemoryCapture(store, OWNER).tidy(bot["id"]) == 1
    [kept] = memories(store, bot)
    assert kept["id"] == first["id"] and kept["can_undo"]
    # Undo of the one kept forgets it and brings the copy back: nothing is lost.
    store.undo_memory(OWNER, bot["id"], first["id"])
    assert [m["id"] for m in memories(store, bot)] == [second["id"]]
