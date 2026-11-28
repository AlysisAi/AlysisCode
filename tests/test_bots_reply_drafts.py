"""The owl's draft reply while the reply checker checks it (reply_review.py,
task_events.REPLY_DRAFT): the owner's live view of the turn gets it, as it would be published,
before the check starts; it becomes the reply when the check passes, gives way to the next draft
when it does not, never outlives its check (Stop, a failed check, a restart), and nothing but that
live view of the turn (not the conversation, search, Needs you, a move, a routine's output or
another owl) ever carries it. Scripted models, not live evidence."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from uuid import uuid4

import pytest
from test_bots_desktop_runtime import run_task
from test_bots_desktop_runtime import study as execution_study

from alysis_code.bots.cooperation import PeerRunner, PeerStore
from alysis_code.bots.models import (
    BotInput,
    ConversationPageQuery,
    HistoryQuery,
    MemoryInput,
    TaskInput,
)
from alysis_code.bots.owl_move import MoveExport, export_page
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Store, encode
from alysis_code.bots.task_events import DRAFT_SETTLED_BY
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

OWNER = "owner"
study = execution_study

# Two scripted models that declare different capabilities: what the person sees of a draft
# depends on neither.
MODELS = [
    pytest.param(("fixture-tools", True), id="tool-calling"),
    pytest.param(("fixture-text", False), id="text-only"),
]


def response(text="", calls=()):
    return LLMResponse(text, list(calls), {}, usage=LLMUsage(10, 5, 15))


def verdict(decision, reason="The reply matches the receipts.", issue=None):
    body = {"decision": decision, "reason": reason} | ({"issue": issue} if issue else {})
    return response(json.dumps(body))


class Owl:
    def __init__(self, answers, model=("fixture-tools", True)):
        self.model, self.supports_tool_calling = model
        self.answers = iter(answers)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        item = next(self.answers)
        return item(kwargs) if callable(item) else item


def reviewing(request) -> bool:
    return request["messages"][0]["content"] == REVIEW_INSTRUCTIONS


def turn(tmp_path, message, answers, *, model=("fixture-tools", True), review=True):
    store = Store(tmp_path / "owls.db")
    bot = store.save_bot(OWNER, BotInput(name="Ίρις", purpose="Help"))
    chat = store.create_chat(OWNER, bot["id"], "Συζήτηση")
    store.enqueue(OWNER, chat["id"], TaskInput(message=message, request_id=uuid4().hex))
    task = store.claim(OWNER)
    if not review:
        task["config"]["reply_review_enabled"] = False
        with store.connect(write=True) as db:
            db.execute("UPDATE tasks SET config=? WHERE id=?", (encode(task["config"]), task["id"]))
    owl = Owl(answers, model)
    runtime = BotRuntime(store, OWNER, lambda _: owl, limits=RuntimeLimits(calls_per_task=12))
    runtime.reply_review = ReplyReview(runtime)
    return store, task, owl, runtime


def run(runtime, task):
    runtime.run(task, EventCancellationToken(threading.Event()))
    return runtime.store.task(OWNER, task["id"])


def drafts(task_view):
    """The draft events of a task view, as (kind, data)."""
    return [
        (event["kind"], event["data"])
        for event in task_view["events"]
        if event["kind"].startswith("reply_draft")
    ]


def shown(task_view):
    """The draft the person's live view shows now, by the rule the App follows: the latest draft
    with its text, while no later event settles it."""
    events = task_view["events"]
    for index in range(len(events) - 1, -1, -1):
        if events[index]["kind"] == "reply_draft":
            later = {event["kind"] for event in events[index + 1 :]}
            text = events[index]["data"].get("text")
            return text if text and not later & DRAFT_SETTLED_BY else None
    return None


def assistant_messages(store, task):
    return [
        m["content"]
        for m in store.conversation(OWNER, task["chat_id"])["messages"]
        if m["role"] == "assistant"
    ]


@pytest.mark.parametrize("model", MODELS)
def test_the_draft_is_shown_before_its_check_starts_without_changing_what_the_reviewer_reads(
    tmp_path, model
):
    draft = "Βρήκα δύο πτήσεις για 東京· δεν έκλεισα καμία."
    suggestion = "Κλείσε την πρώτη"

    def review(request):
        events = store.task(OWNER, task["id"])["events"]
        kinds = [event["kind"] for event in events]
        # Recorded before the check made its call, as the person would get it: without the
        # suggested next messages, which travel beside a reply.
        assert kinds.index("reply_draft") < kinds.index("reply_review_started")
        assert drafts({"events": events}) == [("reply_draft", {"draft": 1, "text": draft})]
        assert shown({"events": events}) == draft
        # The reviewer reads the draft as before, and nothing of the draft's record.
        payload = json.loads(request["messages"][1]["content"])
        assert payload["draft_reply"] == draft
        assert "reply_draft" not in json.dumps(request["messages"], ensure_ascii=False)
        return verdict("ready")

    store, task, owl, runtime = turn(
        tmp_path,
        "Βρες μου πτήσεις για 東京",
        [response(f"{draft}\n<follow_ups>\n- {suggestion}\n</follow_ups>"), review],
        model=model,
    )
    final = run(runtime, task)
    assert final["status"] == "completed" and final["output"] == draft
    assert sum(reviewing(request) for request in owl.requests) == 1


@pytest.mark.parametrize("model", MODELS)
def test_an_accepted_draft_becomes_the_reply_with_no_draft_left(tmp_path, model):
    draft = "Το άθροισμα είναι 437."
    store, task, owl, runtime = turn(
        tmp_path, "Πόσο κάνει 19×23;", [response(draft), verdict("ready")], model=model
    )
    final = run(runtime, task)
    assert final["status"] == "completed" and final["output"] == draft
    # One reply, once: the conversation has it, and the draft carries no text any more.
    assert assistant_messages(store, task) == [draft]
    assert drafts(final) == [("reply_draft", {"draft": 1})]
    assert shown(final) is None
    kinds = [event["kind"] for event in final["events"]]
    assert (
        kinds.index("reply_draft") < kinds.index("reply_review_finished") < kinds.index("completed")
    )


@pytest.mark.parametrize("model", MODELS)
def test_a_repaired_draft_gives_way_to_the_next_one(tmp_path, model):
    withheld = "Αποθήκευσα το 東京.txt."
    reply = "Αποθήκευσα το 東京.txt στη Βιβλιοθήκη."

    def first_check(_request):
        assert shown(store.task(OWNER, task["id"])) == withheld
        return verdict("repair", "No save receipt exists yet.", issue="omission")

    def save(_request):
        # The withheld draft is gone while the owl works again: nothing shows in its place yet.
        view = store.task(OWNER, task["id"])
        assert shown(view) is None
        assert drafts(view) == [
            ("reply_draft", {"draft": 1}),
            ("reply_draft_withdrawn", {"draft": 1}),
        ]
        return response(
            calls=[ToolCall("save", "save_file", {"name": "東京.txt", "content": "Γεια"})]
        )

    def second_check(_request):
        assert shown(store.task(OWNER, task["id"])) == reply
        return verdict("ready")

    store, task, owl, runtime = turn(
        tmp_path,
        "Αποθήκευσε μια σημείωση 東京.txt",
        [response(withheld), first_check, save, response(reply), second_check],
        model=model,
    )
    final = run(runtime, task)
    assert final["status"] == "completed" and final["output"] == reply
    assert assistant_messages(store, task) == [reply]
    kinds = [event["kind"] for event in final["events"]]
    assert kinds.index("reply_review_finished") < kinds.index("reply_draft_withdrawn")
    assert drafts(final) == [
        ("reply_draft", {"draft": 1}),
        ("reply_draft_withdrawn", {"draft": 1}),
        ("reply_draft", {"draft": 2}),
    ]
    assert withheld not in json.dumps(drafts(final), ensure_ascii=False)


def test_a_draft_set_aside_for_a_change_during_its_check_goes_at_once(tmp_path):
    """Saved memory changes while the draft is checked: the turn writes again from the current
    state, and the set-aside draft is gone while it does."""
    stale = "Η προτίμησή σου: απαντήσεις στα αγγλικά."
    reply = "Η προτίμησή σου: απαντήσεις στα ελληνικά."

    def change_memory(_request):
        assert shown(store.task(OWNER, task["id"])) == stale
        store.save_memory(
            OWNER,
            task["bot_id"],
            MemoryInput(title="Γλώσσα", content="Ελληνικά"),
            memory_id=memory["id"],
        )
        return verdict("ready")

    def write_again(_request):
        view = store.task(OWNER, task["id"])
        assert shown(view) is None
        assert ("reply_draft_withdrawn", {"draft": 1}) in drafts(view)
        return response(reply)

    store, task, owl, runtime = turn(
        tmp_path,
        "Ποια προτίμηση έχω αποθηκεύσει;",
        [response(stale), change_memory, write_again, verdict("ready")],
    )
    memory = store.save_memory(
        OWNER, task["bot_id"], MemoryInput(title="Γλώσσα", content="Αγγλικά")
    )
    final = run(runtime, task)
    assert final["status"] == "completed" and final["output"] == reply
    assert assistant_messages(store, task) == [reply]
    assert stale not in json.dumps(final["events"], ensure_ascii=False)


def test_stop_during_the_check_takes_the_draft_away(tmp_path):
    draft = "Η απάντηση είναι 437."

    def stop(_request):
        assert shown(store.task(OWNER, task["id"])) == draft
        store.cancel(OWNER, task["id"])
        # Stop alone settles it, before the check even returns.
        assert shown(store.task(OWNER, task["id"])) is None
        token._event.set()
        return verdict("ready")

    store, task, owl, runtime = turn(tmp_path, "Πόσο κάνει 19×23;", [response(draft), stop])
    token = EventCancellationToken(threading.Event())
    runtime.run(task, token)
    final = store.task(OWNER, task["id"])
    assert final["status"] == "cancelled"
    assert assistant_messages(store, task) == []
    assert draft not in json.dumps(final["events"], ensure_ascii=False)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda _request: response("not a verdict"), id="invalid-verdict"),
        pytest.param(lambda _request: response(calls=[ToolCall("x", "save_file", {})]), id="acts"),
    ],
)
def test_a_failed_check_takes_the_draft_away(tmp_path, failure):
    draft = "Έστειλα το email στη Μαρία."
    store, task, owl, runtime = turn(tmp_path, "Στείλε email στη Μαρία", [response(draft), failure])
    final = run(runtime, task)
    assert final["status"] == "failed"
    assert assistant_messages(store, task) == []
    assert shown(final) is None
    assert draft not in json.dumps(final["events"], ensure_ascii=False)
    assert ("reply_draft_withdrawn", {"draft": 1}) in drafts(final)


@pytest.mark.parametrize("how", ["recover", "restart_cut"])
def test_a_restart_during_the_check_takes_the_draft_away(tmp_path, how):
    draft = "Βρήκα τρία ξενοδοχεία στο Ναύπλιο."
    store, task, _owl, _runtime = turn(tmp_path, "Βρες ξενοδοχεία στο Ναύπλιο", [])
    assert store.show_reply_draft(OWNER, task["id"], draft) == 1
    assert shown(store.task(OWNER, task["id"])) == draft
    # The service stops under the check (a crash, an update or a quit): the turn starts again.
    if how == "recover":
        store.recover(OWNER)
    else:
        assert store.restart_cut(OWNER, task["id"])
    view = store.task(OWNER, task["id"])
    assert view["status"] == "queued"
    assert shown(view) is None
    assert draft not in json.dumps(view["events"], ensure_ascii=False)
    again = store.claim(OWNER)
    assert again["id"] == task["id"] and shown(store.task(OWNER, task["id"])) is None


def test_the_turn_pausing_for_the_persons_control_of_its_computer_takes_the_draft_away(tmp_path):
    draft = "Συμπλήρωσα τη φόρμα κράτησης."
    store, task, _owl, _runtime = turn(tmp_path, "Συμπλήρωσε τη φόρμα", [])
    store.show_reply_draft(OWNER, task["id"], draft)
    # What the computer's hand-over records as the turn waits (computer_control.py).
    store.event(OWNER, task["id"], "computer_control_taken", {"message": "Paused."})
    view = store.task(OWNER, task["id"])
    assert shown(view) is None and draft not in json.dumps(view["events"], ensure_ascii=False)


def test_a_draft_is_only_for_a_working_turn_and_only_with_words(tmp_path):
    store, task, _owl, _runtime = turn(tmp_path, "Γεια", [])
    assert store.show_reply_draft(OWNER, task["id"], "  \n ") is None
    store.cancel(OWNER, task["id"])
    assert store.show_reply_draft(OWNER, task["id"], "Γεια σου!") is None
    assert drafts(store.task(OWNER, task["id"])) == []
    # Nothing to withdraw is no change.
    store.withdraw_reply_draft(OWNER, task["id"])
    assert drafts(store.task(OWNER, task["id"])) == []


def test_with_reply_checking_off_nothing_changes(tmp_path):
    reply = "Το άθροισμα είναι 437."
    store, task, owl, runtime = turn(tmp_path, "Πόσο κάνει 19×23;", [response(reply)], review=False)
    final = run(runtime, task)
    assert final["status"] == "completed" and final["output"] == reply
    assert drafts(final) == []
    assert not any(reviewing(request) for request in owl.requests)


def test_a_draft_never_leaves_the_live_view_of_its_turn(tmp_path):
    withheld = "Κλείσα το δωμάτιο 東京-17 για την Παρασκευή."
    reply = "Δεν έκλεισα τίποτα· το δωμάτιο 東京-17 είναι ακόμη διαθέσιμο."

    def nowhere_else(text):
        bot_id = task["bot_id"]
        surfaces = {
            "conversation": store.conversation(OWNER, task["chat_id"]),
            "page": store.conversation_page(OWNER, task["chat_id"], ConversationPageQuery()),
            "search": store.search_history(
                OWNER, bot_id, HistoryQuery(query="東京-17"), excerpts=True
            ),
            "needs_you": store.needs_you(OWNER),
            "owls": store.list_bots(OWNER),
        }
        for name, value in surfaces.items():
            assert text not in json.dumps(value, ensure_ascii=False), name

    def first_check(_request):
        # The live view of the turn has the draft; nothing else does.
        assert shown(store.task(OWNER, task["id"])) == withheld
        nowhere_else(withheld)
        return verdict("repair", "No booking receipt exists.", issue="contradiction")

    def second_check(_request):
        assert shown(store.task(OWNER, task["id"])) == reply
        nowhere_else(reply)
        nowhere_else(withheld)
        return verdict("ready")

    store, task, owl, runtime = turn(
        tmp_path,
        "Κλείσε το δωμάτιο 東京-17",
        [response(withheld), first_check, response(reply), second_check],
    )
    final = run(runtime, task)
    assert final["status"] == "completed" and final["output"] == reply
    nowhere_else(withheld)
    # Moving the owl to another of the person's places carries its turns, never a draft.
    moved = json.dumps(export_page(store, OWNER, task["bot_id"], MoveExport()), ensure_ascii=False)
    assert withheld not in moved
    assert '"reply_draft"' in moved and reply in moved  # The turn, and its published reply.


def test_a_routine_run_and_an_exchange_between_owls_never_carry_a_draft(tmp_path):
    """Neither a routine's run nor a turn that reports another owl's answer is checked here, so
    their outputs, and what one owl sends another, are never a draft."""
    store = Store(tmp_path / "owls.db")
    owl = store.save_bot(OWNER, BotInput(name="Ίρις", purpose="Help"))
    peer = store.save_bot(OWNER, BotInput(name="Νεφέλη", purpose="Review"))
    routine = store.save_schedule(
        OWNER,
        owl["id"],
        ScheduleInput.model_validate(
            {
                "name": "Πρωινός έλεγχος",
                "prompt": "Έλεγξε τις τιμές για 東京.",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
    )
    run_now = store.run_schedule_now(OWNER, owl["id"], routine["id"], uuid4().hex, max_calls=None)
    routine_reply = "Οι τιμές για 東京 δεν άλλαξαν."
    model = Owl([response(routine_reply)])
    runtime = BotRuntime(store, OWNER, lambda _: model, limits=RuntimeLimits(calls_per_task=6))
    runtime.reply_review = ReplyReview(runtime)
    claimed = store.claim(OWNER)
    assert claimed["id"] == run_now["task_id"]
    final = run(runtime, claimed)
    assert final["status"] == "completed" and final["output"] == routine_reply
    assert drafts(final) == []

    chat = store.create_chat(OWNER, owl["id"], "Συζήτηση")
    store.enqueue(OWNER, chat["id"], TaskInput(message="Ρώτα τη Νεφέλη", request_id=uuid4().hex))
    asking = store.claim(OWNER)
    answer = "Η χωρητικότητα είναι 7· το 東京 μένει ίδιο."

    def respond(request):
        if request["messages"][0]["content"].startswith("You are the receiving peer"):
            return response(answer)
        if reviewing(request):
            return verdict("ready")
        if request["tools"] and not any(m["role"] == "tool" for m in request["messages"]):
            return response(
                calls=[
                    ToolCall(
                        "peer-call",
                        "request_peer",
                        {"recipient_bot_id": peer["id"], "brief": "Έλεγξε τη χωρητικότητα."},
                    )
                ]
            )
        return response("Η Νεφέλη λέει: χωρητικότητα 7.")

    model = Owl([respond] * 8)
    runtime = BotRuntime(store, OWNER, lambda _: model, limits=RuntimeLimits(calls_per_task=6))
    runtime.reply_review = ReplyReview(runtime)
    run(runtime, asking)
    peers = PeerStore(store, OWNER)
    PeerRunner(peers, runtime.client_factory, runtime.limits, threading.Event()).run(peers.claim())
    resumed = store.claim(OWNER)
    final = run(runtime, resumed)
    assert final["status"] == "completed"
    assert drafts(final) == []
    exchange = peers.for_task(asking["id"])
    assert exchange["result"] and "reply_draft" not in json.dumps(exchange, ensure_ascii=False)


def test_background_work_shows_its_report_while_checked_and_withdraws_it_when_set_aside(study):
    store, task, manager, _ = study
    stale = "Ο φάκελος έχει 3 αρχεία."
    report = "Ο φάκελος έχει 4 αρχεία."

    def check_then_change_memory(_request):
        assert shown(store.task(OWNER, task["id"])) == stale
        # Saved memory changes while the report is checked: the work sets this report aside.
        store.save_memory(
            OWNER, task["bot_id"], MemoryInput(title="Μονάδα", content="Μέτρα αρχεία")
        )
        return verdict("ready")

    def write_again(_request):
        view = store.task(OWNER, task["id"])
        assert shown(view) is None
        assert ("reply_draft_withdrawn", {"draft": 1}) in drafts(view)
        return response(report)

    def check(_request):
        assert shown(store.task(OWNER, task["id"])) == report
        return verdict("ready")

    answers = iter([response(stale), check_then_change_memory, write_again, check])

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            item = next(answers)
            return item(kwargs) if callable(item) else item

    runtime = BotRuntime(store, OWNER, lambda _: Client(), desktop_computers=manager)
    runtime.reply_review = ReplyReview(runtime)
    runtime.limits = replace(runtime.limits, compaction_enabled=False, calls_per_task=6)
    run_task(runtime, task)
    final = store.task(OWNER, task["id"])
    assert final["status"] == "completed", final["error"]
    assert final["output"] == report
    assert shown(final) is None and stale not in json.dumps(final["events"], ensure_ascii=False)
