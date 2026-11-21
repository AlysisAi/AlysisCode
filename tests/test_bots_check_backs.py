"""An owl coming back to its chat later by itself (check_backs.py).

Instead of waiting inside a turn, an owl calls check_back and ends the turn; the service starts
one new turn in the same chat when it is due, or sooner when a condition it checks without a
model holds (a page shows or no longer shows a text, a Library file arrives, the owl's other
work ends). The turn's request is a note from the app, never the person's words. It fires once,
never twice after a restart, once late after the app was asleep; limits, who may set one, moves,
deletion, routes and the schema are pinned here. Real store and runtime; scripted model; a fake
clock and fake pages; Greek and Chinese texts.
"""

from __future__ import annotations

import json
import threading
from uuid import uuid4

import pytest
from pydantic import ValidationError

import alysis_code.bots.web_lookup as web_lookup
from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.check_backs import (
    MAX_PENDING,
    CheckBackRequest,
    CheckBacks,
    name_matches,
    page_shows,
)
from alysis_code.bots.models import BotInput, BotOrganizeInput, TaskInput
from alysis_code.bots.owl_move import MAX_COUNTS, require_unchanged
from alysis_code.bots.owl_move import fingerprint as owl_fingerprint
from alysis_code.bots.owl_prompt import guidance
from alysis_code.bots.owl_tools import ROOM_TURN_LEFT_OUT, ROUTINE_TOOLS, tool_schema
from alysis_code.bots.owl_toolset import EVERYDAY_TOOLS, Turn, tool_names
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

OWNER = "alice"  # as in test_bots_owl_move, whose move helpers these tests share
NOW = 1_800_000_000.0
HOUR = 3600.0
GREEK_NOTE = "Δες αν το προϊόν είναι ξανά διαθέσιμο και πες το στον χρήστη."
CHINESE_NOTE = "看看页面是否还写着售罄，然后告诉用户。"


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class Pages:
    """A watched page as the service reads it: each read takes the next answer (the last stays)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.reads: list[str] = []

    def __call__(self, url):
        self.reads.append(url)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def inline(work):
    work()


@pytest.fixture(autouse=True)
def web_lookup_on(monkeypatch):
    # The test machine's search package doesn't decide whether pages can be watched here.
    monkeypatch.setattr(web_lookup, "web_tools_available", lambda: True)


def owl(store, **setup):
    bot = store.save_bot(OWNER, BotInput(name="Γλαύκα", purpose="Βοηθός αγορών", **setup))
    return bot, store.create_chat(OWNER, bot["id"], "Συνομιλία")


def ask(store, chat, text="Πες μου όταν το προϊόν είναι ξανά διαθέσιμο."):
    task = store.enqueue(OWNER, chat["id"], TaskInput(message=text, request_id=uuid4().hex))
    assert store.claim(OWNER)["id"] == task["id"]
    return store.task(OWNER, task["id"])


def keeper(store, clock, pages=None):
    return CheckBacks(
        store, OWNER, clock=clock, read_page=pages or Pages(("", True)), run_check=inline
    )


def request(**fields):
    return CheckBackRequest.model_validate({"note": GREEK_NOTE, **fields})


def started(store, check_back_id):
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE owner=? AND request_id=?",
            (OWNER, "check_back_" + check_back_id),
        ).fetchall()
    return [store.task(OWNER, row["id"]) for row in rows]


def row_of(store, check_back_id):
    with store.connect() as db:
        return dict(db.execute("SELECT * FROM check_backs WHERE id=?", (check_back_id,)).fetchone())


def response(text="", calls=()):
    return LLMResponse(text, list(calls), {}, usage=LLMUsage(10, 5, 15))


class Model:
    model = "fixture"
    supports_tool_calling = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.responses)

    def tools(self, index=0):
        return {t["function"]["name"] for t in self.requests[index].get("tools") or []}


def runtime(store, model, clock, pages=None, **limits):
    rt = BotRuntime(store, OWNER, lambda _: model, limits=RuntimeLimits(**limits))
    rt.check_backs = keeper(store, clock, pages)
    return rt


def run_next(rt, store):
    task = store.claim(OWNER)
    rt.run(task, EventCancellationToken(threading.Event()))
    return store.task(OWNER, task["id"])


def tool_result(model, index=1):
    return json.loads(
        next(m for m in model.requests[index]["messages"] if m["role"] == "tool")["content"]
    )


# ---------------------------------------------------------------------------------------------
# Set from the person's own turn, listed in the chat, cancelled by the person


def test_a_persons_turn_sets_one_the_chat_shows_it_and_the_person_cancels_it(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    clock = Clock()
    store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(message="Ξανακοίτα τη σελίδα σε μία ώρα.", request_id="message-0001"),
    )
    model = Model(
        [
            response(
                calls=[
                    ToolCall("c", "check_back", {"after_seconds": 3600, "note": GREEK_NOTE}),
                ]
            ),
            response("Θα ξανακοιτάξω σε μία ώρα."),
        ]
    )
    done = run_next(runtime(store, model, clock), store)
    assert done["status"] == "completed"
    assert "check_back" in model.tools()
    result = tool_result(model)
    assert result["state"] == "pending" and result["due_at"] == NOW + HOUR
    assert "End your turn now" in result["message"]
    # Its step is recorded on the turn that set it.
    assert any(e["kind"] == "check_back_set" for e in done["events"])
    keep = keeper(store, clock)
    [listed] = keep.list(chat["id"])["check_backs"]
    assert (listed["note"], listed["source_task_id"]) == (GREEK_NOTE, done["id"])
    # The chat shows it under the reply that set it.
    [turn] = store.conversation(OWNER, chat["id"])["tasks"]
    assert [c["state"] for c in turn["check_backs"]] == ["pending"]
    page = store.conversation_page(OWNER, chat["id"], _page_query())
    assert page["tasks"][0]["check_backs"][0]["id"] == listed["id"]
    # Cancel: it never starts, and a second Cancel answers the same.
    assert keep.cancel(chat["id"], listed["id"])["state"] == "cancelled"
    assert keep.cancel(chat["id"], listed["id"])["state"] == "cancelled"
    assert keep.list(chat["id"]) == {"check_backs": []}
    clock.now += 2 * HOUR
    assert keep.tick() == 0 and started(store, listed["id"]) == []
    assert store.conversation(OWNER, chat["id"])["tasks"][0]["check_backs"][0]["state"] == (
        "cancelled"
    )
    with pytest.raises(NotFound):
        keep.cancel(chat["id"], uuid4().hex)
    other_bot, other_chat = owl(store)
    with pytest.raises(NotFound):
        keep.cancel(other_chat["id"], listed["id"])
    with pytest.raises(NotFound):
        keep.list(uuid4().hex)
    assert other_bot["id"] != bot["id"]


def _page_query():
    from alysis_code.bots.models import ConversationPageQuery

    return ConversationPageQuery()


# ---------------------------------------------------------------------------------------------
# It fires once, at its time, as a note from the app


def test_it_fires_once_at_its_time_as_a_note_from_the_app_not_the_persons_words(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    clock = Clock()
    source = ask(store, chat)
    keep = keeper(store, clock)
    item = keep.set(source, request(after_seconds=600))
    store.finish(OWNER, source["id"], "completed", content="Θα ξανακοιτάξω σε δέκα λεπτά.")
    clock.now += 599
    assert keep.tick() == 0
    clock.now += 1
    assert keep.tick() == 1
    assert keep.tick() == 0 and keep.tick(NOW + 5 * HOUR) == 0
    [turn] = started(store, item["id"])
    assert turn["message"] == "" and turn["status"] == "queued"
    assert turn["config"]["check_back"]["outcome"] == "time"
    assert "late_by_seconds" not in turn["config"]["check_back"]
    assert [e["kind"] for e in turn["events"]][:2] == ["queued", "checked_back"]
    assert row_of(store, item["id"])["state"] == "starting"
    assert keep.list(chat["id"]) == {"check_backs": []}
    # The person said nothing: the chat has no message of theirs for it.
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM messages WHERE task_id=?", (turn["id"],)).fetchone()
    # The turn reads the app's note after the person's own earlier request.
    model = Model([response("Είναι ξανά διαθέσιμο.")])
    rt = runtime(store, model, clock)
    rt.check_backs = keep
    done = run_next(rt, store)
    assert done["status"] == "completed"
    asked = [m["content"] for m in model.requests[0]["messages"] if m["role"] == "user"]
    note = next(text for text in asked if "You asked to check back:" in text)
    assert note.startswith("This message comes from the Alysis app, not from the person.")
    assert json.dumps(GREEK_NOTE, ensure_ascii=False) in note
    assert any("Πες μου όταν το προϊόν" in text for text in asked)
    # It carries the person's request on: it can wait again, but sets up nothing for them.
    tools = model.tools()
    assert "check_back" in tools
    assert (
        not (ROUTINE_TOOLS | {"propose_schedule", "propose_setup", "answer_clarification"}) & tools
    )
    # Its turn ended, so it is done; the chat shows "Checked back" above its reply.
    keep.tick()
    assert row_of(store, item["id"])["state"] == "done"
    shown = next(t for t in store.conversation(OWNER, chat["id"])["tasks"] if t["id"] == done["id"])
    assert shown["checked_back"] == {"id": item["id"], "note": GREEK_NOTE, "outcome": "time"}
    assert bot["id"] == done["bot_id"]


def test_a_restart_never_starts_it_twice(tmp_path):
    path = tmp_path / "bots.db"
    store = Store(path)
    _bot, chat = owl(store)
    clock = Clock()
    source = ask(store, chat)
    item = keeper(store, clock).set(source, request(after_seconds=60))
    store.finish(OWNER, source["id"], "completed", content="Εντάξει.")
    clock.now += 61
    assert keeper(store, clock).tick() == 1
    # The service stops and starts again: the start was saved with its turn.
    again = Store(path)
    assert keeper(again, clock).tick() == 0
    # It stopped again while that turn worked: that same turn starts again once (it had changed
    # nothing, step_effects.py), and the check-back never starts another.
    assert again.claim(OWNER)["request_id"] == "check_back_" + item["id"]
    assert Store(path).recover(OWNER) == 1
    later = Store(path)
    clock.now += HOUR
    assert keeper(later, clock).tick() == 0
    [turn] = started(later, item["id"])
    assert turn["status"] == "queued" and turn["restarts"] == 1
    assert row_of(later, item["id"])["state"] == "starting"
    # Cut off a second time, it is interrupted, never replayed, and the check-back is done.
    assert later.claim(OWNER)["id"] == turn["id"]
    assert Store(path).recover(OWNER) == 1
    last = Store(path)
    clock.now += HOUR
    assert keeper(last, clock).tick() == 0
    [turn] = started(last, item["id"])
    assert turn["status"] == "interrupted"
    assert turn["failure"]["not_rerun"] == "already_rerun"
    assert row_of(last, item["id"])["state"] == "done"


def test_after_the_app_slept_through_its_time_it_fires_once_and_says_how_late(tmp_path):
    store = Store(tmp_path / "bots.db")
    _bot, chat = owl(store)
    clock = Clock()
    source = ask(store, chat)
    keep = keeper(store, clock)
    item = keep.set(source, request(after_seconds=3600))
    store.finish(OWNER, source["id"], "completed", content="Εντάξει.")
    # Nothing ran for three days.
    clock.now = NOW + 3600 + 3 * 86400
    assert keep.tick() == 1 and keep.tick() == 0
    [turn] = started(store, item["id"])
    assert turn["config"]["check_back"]["late_by_seconds"] == 3 * 86400
    model = Model([response("Συγγνώμη για την καθυστέρηση.")])
    rt = runtime(store, model, clock)
    run_next(rt, store)
    asked = "\n".join(str(m["content"]) for m in model.requests[0]["messages"])
    assert "3 days later than you planned" in asked


# ---------------------------------------------------------------------------------------------
# Conditions, checked without a model


@pytest.mark.parametrize(
    ("page", "text", "shown"),
    [
        ("Κατάσταση: ΔΙΑΘΕΣΙΜΟ ΣΕ ΑΠΟΘΕΜΑ", "διαθέσιμο", True),  # case and accents
        ("Κατάσταση: Διαθέσιμο", "ΔΙΑΘΕΣΙΜΟ", True),
        ("Διαθέσιμο   σε\n απόθεμα", "διαθέσιμο σε απόθεμα", True),  # spacing
        ("Εξαντλήθηκε", "διαθέσιμο", False),
        ("状态：已售罄，请稍后再来", "售罄", True),
        ("状态：有货", "售罄", False),
        ("ｓｏｌｄ ｏｕｔ", "Sold out", True),  # full-width forms, only when nothing else matches
    ],
)
def test_page_text_is_matched_the_same_way_in_every_script(page, text, shown):
    assert page_shows(page, text) is shown


def test_a_greek_page_text_brings_the_turn_early_without_a_model_and_with_backoff(tmp_path):
    store = Store(tmp_path / "bots.db")
    _bot, chat = owl(store)
    clock = Clock()
    pages = Pages(
        ("Το προϊόν: Εξαντλήθηκε", True),
        web_lookup.Conflict("The page could not be read (the site is busy)."),
        ("Το προϊόν: ΔΙΑΘΕΣΙΜΟ", True),
    )
    keep = keeper(store, clock, pages)
    source = ask(store, chat)
    url = "https://shop.example.invalid/item"
    item = keep.set(
        source,
        request(
            after_seconds=7 * 86400,
            condition={"kind": "page_contains", "url": url, "text": "Διαθέσιμο"},
        ),
    )
    store.finish(OWNER, source["id"], "completed", content="Θα σου πω.")
    # No look before the first check is due.
    assert keep.tick() == 0 and pages.reads == []
    clock.now += 60
    assert keep.tick() == 0 and pages.reads == [url]
    # The next look waits longer: the wait doubles.
    clock.now += 60
    assert keep.tick() == 0 and len(pages.reads) == 1
    clock.now += 60
    assert keep.tick() == 0 and len(pages.reads) == 2
    assert "busy" in row_of(store, item["id"])["check_error"]
    clock.now += 239
    assert keep.tick() == 0 and len(pages.reads) == 2
    clock.now += 1
    assert keep.tick() == 1 and len(pages.reads) == 3
    [turn] = started(store, item["id"])
    assert turn["config"]["check_back"]["outcome"] == "met"
    model = Model([response("Είναι διαθέσιμο!")])
    run_next(runtime(store, model, clock), store)
    asked = "\n".join(str(m["content"]) for m in model.requests[0]["messages"])
    assert "What you were waiting for happened: the page " + url in asked


def test_a_chinese_text_that_disappears_brings_the_turn_and_a_partial_page_never_does(tmp_path):
    store = Store(tmp_path / "bots.db")
    _bot, chat = owl(store)
    clock = Clock()
    pages = Pages(
        ("状态：已售罄", True),
        ("状态：" + "很长的页面 " * 10, False),  # read only in part: the text may be further down
        ("状态：有货", True),
    )
    keep = keeper(store, clock, pages)
    source = ask(store, chat, "页面不再写售罄时告诉我。")
    item = keep.set(
        source,
        CheckBackRequest(
            note=CHINESE_NOTE,
            after_seconds=86400,
            condition={"kind": "page_lacks", "url": "https://example.invalid/货品", "text": "售罄"},
        ),
    )
    for wait in (60, 120, 240):
        clock.now += wait
        fired = keep.tick()
    assert len(pages.reads) == 3 and fired == 1
    with store.connect() as db:
        assert db.execute("SELECT checks FROM check_backs").fetchone()[0] == 3
    [turn] = started(store, item["id"])
    assert turn["config"]["check_back"]["outcome"] == "met"
    assert turn["config"]["check_back"]["note"] == CHINESE_NOTE


@pytest.mark.parametrize(
    ("name", "pattern", "matches"),
    [
        ("Αναφορά Νοεμβρίου.pdf", "αναφορα*.PDF", True),
        ("报告 2026.xlsx", "报告*", True),
        ("[τελικό] σχέδιο.docx", "[τελικό]*", True),  # brackets are literal
        ("σχέδιο.docx", "σχέδιο.pdf", False),
        ("σχέδιο-1.docx", "σχέδιο-?.docx", True),
    ],
)
def test_file_names_match_their_pattern_in_every_script(name, pattern, matches):
    assert name_matches(name, pattern) is matches


def test_a_library_file_added_after_it_was_set_brings_the_turn(tmp_path):
    from alysis_code.bots.models import FileInput

    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    clock = Clock(NOW)
    store.save_file(OWNER, bot["id"], FileInput(name="Αναφορά Οκτωβρίου.md", content="παλιά"))
    source = ask(store, chat)
    keep = keeper(store, clock)
    # Files already there don't count: only one added from now on.
    clock.now = store.list_files(OWNER, bot["id"])[0]["created_at"]
    item = keep.set(
        source,
        request(after_seconds=86400, condition={"kind": "file_appears", "name": "αναφορα*"}),
    )
    store.finish(OWNER, source["id"], "completed", content="Θα περιμένω.")
    clock.now += 5
    assert keep.tick() == 0
    store.save_file(OWNER, bot["id"], FileInput(name="ΑΝΑΦΟΡΆ Νοεμβρίου.md", content="νέα"))
    clock.now += 10
    assert keep.tick() == 1
    [turn] = started(store, item["id"])
    assert turn["config"]["check_back"]["outcome"] == "met"


def test_the_end_of_the_owls_background_work_brings_the_turn(tmp_path):
    store = Store(tmp_path / "bots.db")
    _bot, chat = owl(store)
    clock = Clock()
    work = ask(store, chat, "Ετοίμασε την αναφορά στο παρασκήνιο.")
    BackgroundStore(store, OWNER).submit(work, "Η αναφορά", [])
    source = ask(store, chat, "Πες μου όταν τελειώσει.")
    keep = keeper(store, clock)
    item = keep.set(
        source,
        request(after_seconds=86400, condition={"kind": "work_ends", "work_id": work["id"]}),
    )
    store.finish(OWNER, source["id"], "completed", content="Θα σου πω.")
    clock.now += 5
    assert keep.tick() == 0
    store.finish(OWNER, work["id"], "failed", error="Σταμάτησε.")
    clock.now += 10
    assert keep.tick() == 1
    assert started(store, item["id"])[0]["config"]["check_back"]["outcome"] == "met"
    # Waiting for work that already ended, or isn't this owl's, is refused.
    other = ask(store, chat, "Και πάλι;")
    with pytest.raises(Conflict, match="already ended"):
        keep.set(
            other,
            request(after_seconds=600, condition={"kind": "work_ends", "work_id": work["id"]}),
        )
    with pytest.raises(NotFound):
        keep.set(
            other,
            request(after_seconds=600, condition={"kind": "work_ends", "work_id": uuid4().hex}),
        )
    with pytest.raises(Conflict, match="itself"):
        keep.set(
            other,
            request(after_seconds=600, condition={"kind": "work_ends", "work_id": other["id"]}),
        )


def test_a_condition_that_never_holds_fires_at_the_deadline_and_says_so(tmp_path):
    store = Store(tmp_path / "bots.db")
    _bot, chat = owl(store)
    clock = Clock()
    pages = Pages(
        web_lookup.Conflict("The page could not be read (the site refused automated reads).")
    )
    keep = keeper(store, clock, pages)
    source = ask(store, chat)
    item = keep.set(
        source,
        request(
            after_seconds=3600,
            condition={
                "kind": "page_contains",
                "url": "https://example.invalid/",
                "text": "Διαθέσιμο",
            },
        ),
    )
    store.finish(OWNER, source["id"], "completed", content="Θα δω.")
    while clock.now < NOW + 3600:
        clock.now += 30
        keep.tick()
    # Backoff: a handful of looks in an hour, not one every pass.
    assert 3 <= len(pages.reads) <= 7
    [turn] = started(store, item["id"])
    assert turn["config"]["check_back"]["outcome"] == "deadline"
    model = Model([response("Δεν άλλαξε τίποτα.")])
    run_next(runtime(store, model, clock), store)
    asked = "\n".join(str(m["content"]) for m in model.requests[0]["messages"])
    assert "Your time ran out before what you were waiting for happened" in asked
    assert "refused automated reads" in asked


# ---------------------------------------------------------------------------------------------
# Limits and who may set one


def test_at_most_ten_wait_in_a_chat_and_times_are_bounded(tmp_path):
    store = Store(tmp_path / "bots.db")
    _bot, chat = owl(store)
    clock = Clock()
    keep = keeper(store, clock)
    source = ask(store, chat)
    for index in range(MAX_PENDING):
        keep.set(source, request(after_seconds=60 + index))
    with pytest.raises(LimitReached):
        keep.set(source, request(after_seconds=600))
    # Cancelling one makes room.
    first = keep.list(chat["id"])["check_backs"][0]
    keep.cancel(chat["id"], first["id"])
    keep.set(source, request(after_seconds=600))
    assert MAX_PENDING == 10 and MAX_COUNTS["check_back"] == MAX_PENDING
    for wrong in (
        {"after_seconds": 59},
        {"after_seconds": 30 * 86400 + 1},
        {"after_seconds": 600, "at": "2027-01-01T09:00:00+02:00"},
        {},
        {"at": "2027-01-01T09:00:00"},  # no offset
        {"after_seconds": 600, "condition": {"kind": "page_contains", "url": "https://a.invalid/"}},
        {"after_seconds": 600, "condition": {"kind": "file_appears", "name": "α", "text": "β"}},
        {"after_seconds": 600, "note": ""},
    ):
        with pytest.raises(ValidationError):
            CheckBackRequest.model_validate({"note": GREEK_NOTE, **wrong})
    other_bot, other_chat = owl(store)
    other = ask(store, other_chat)
    from datetime import UTC, datetime

    for at in (NOW + 30, NOW - HOUR, NOW + 31 * 86400):
        with pytest.raises(Conflict, match="1 minute to 30 days"):
            keep.set(other, request(at=datetime.fromtimestamp(at, UTC).isoformat()))
    assert (
        keep.set(other, request(at=datetime.fromtimestamp(NOW + 86400, UTC).isoformat()))["due_at"]
        == NOW + 86400
    )
    for url in (
        "http://127.0.0.1/",
        "http://localhost:8080/",
        "file:///etc/hosts",
        "http://10.0.0.5/",
    ):
        with pytest.raises(Conflict):
            keep.set(
                other,
                request(
                    after_seconds=600, condition={"kind": "page_lacks", "url": url, "text": "x"}
                ),
            )
    assert other_bot["id"]


def test_files_off_or_web_lookup_off_refuse_what_they_would_watch(tmp_path, monkeypatch):
    store = Store(tmp_path / "bots.db")
    _bot, chat = owl(store, files_enabled=False)
    keep = keeper(store, Clock())
    source = ask(store, chat)
    with pytest.raises(Conflict, match="Files are off"):
        keep.set(
            source, request(after_seconds=600, condition={"kind": "file_appears", "name": "*"})
        )
    monkeypatch.setattr(web_lookup, "web_tools_available", lambda: False)
    with pytest.raises(Conflict, match="web pages is off"):
        keep.set(
            source,
            request(
                after_seconds=600,
                condition={"kind": "page_contains", "url": "https://example.invalid/", "text": "x"},
            ),
        )


def test_only_the_persons_own_turn_or_a_check_back_turn_is_offered_it_and_may_set_it(tmp_path):
    # Offered: the person's own message, and a turn a check-back started.
    assert "check_back" in tool_names(Turn(answers_person=True, own_message=True))
    chained = tool_names(
        Turn(answers_person=True, checking_back=True, about_me=True, other_owls=True)
    )
    assert "check_back" in chained
    assert not (ROUTINE_TOOLS | {"propose_setup", "suggest_about_me", "list_peer_bots"}) & set(
        chained
    )
    # Not offered: a first hello or an app's own carrying on, a routine's run, a file event,
    # background work, a room.
    for turn in (
        Turn(answers_person=True),
        Turn(answers_person=True, first_hello=True),
        Turn(answers_person=False, scheduled=True, own_message=True),
        Turn(answers_person=False, activation=True, own_message=True),
        Turn(answers_person=False, background=True, own_message=True),
        Turn(answers_person=True, room=True, own_message=True),
    ):
        assert "check_back" not in tool_names(turn), turn
    assert "check_back" in ROOM_TURN_LEFT_OUT and "check_back" in EVERYDAY_TOOLS
    # The schema has no top-level anyOf, oneOf or allOf, which model APIs refuse.
    parameters = tool_schema("check_back")["function"]["parameters"]
    assert parameters["type"] == "object" and not {"anyOf", "oneOf", "allOf"} & set(parameters)
    # Setting one checks again, whatever a model was offered.
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    clock = Clock()
    keep = keeper(store, clock)
    routine = store.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "Έλεγχος",
                "prompt": "Κοίτα τη σελίδα.",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
    )
    store.run_schedule_now(OWNER, bot["id"], routine["id"], uuid4().hex, max_calls=None)
    run = store.task(OWNER, store.claim(OWNER)["id"])
    with pytest.raises(Conflict, match="person's own message"):
        keep.set(run, request(after_seconds=600))
    store.finish(OWNER, run["id"], "completed", content="Έγινε.")
    # A turn that has ended can't set one either.
    with pytest.raises(Conflict):
        keep.set(store.task(OWNER, run["id"]), request(after_seconds=600))
    # A turn a check-back started may set the next one, and is never the person's own request.
    source = ask(store, chat)
    item = keep.set(source, request(after_seconds=60))
    store.finish(OWNER, source["id"], "completed", content="Εντάξει.")
    clock.now += 60
    keep.tick()
    chained_turn = store.claim(OWNER)
    assert chained_turn["request_id"] == "check_back_" + item["id"]
    assert (
        keep.set(chained_turn, request(after_seconds=600))["source_task_id"] == chained_turn["id"]
    )
    with store.connect() as db:
        with pytest.raises(Conflict):
            store.direct_request(db, OWNER, bot["id"], chained_turn["id"])


def test_a_routine_run_is_never_offered_it(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, _chat = owl(store)
    routine = store.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "每日检查",
                "prompt": "检查页面。",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
    )
    store.run_schedule_now(OWNER, bot["id"], routine["id"], uuid4().hex, max_calls=None)
    model = Model([response("完成。")])
    run_next(runtime(store, model, Clock()), store)
    assert "check_back" not in model.tools()


def test_the_turn_it_starts_keeps_the_owls_limits(tmp_path):
    store = Store(tmp_path / "bots.db")
    _bot, chat = owl(store)
    clock = Clock()
    source = ask(store, chat)
    keeper(store, clock).set(source, request(after_seconds=60))
    store.finish(OWNER, source["id"], "completed", content="Εντάξει.")
    clock.now += 60
    keeper(store, clock).tick()
    # The workspace's model-call allowance is used up: the turn stops before any model call.
    model = Model([response("δεν πρέπει να φτάσει εδώ")])
    done = run_next(runtime(store, model, clock, max_calls=0), store)
    assert done["status"] == "failed" and model.requests == []


def test_an_archived_owl_rests_and_comes_back_to_it_once(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    clock = Clock()
    keep = keeper(store, clock)
    source = ask(store, chat)
    item = keep.set(source, request(after_seconds=60))
    store.finish(OWNER, source["id"], "completed", content="Εντάξει.")
    store.organize_bot(OWNER, bot["id"], BotOrganizeInput(archived=True))
    clock.now += 2 * HOUR
    assert keep.tick() == 0
    store.organize_bot(OWNER, bot["id"], BotOrganizeInput(archived=False))
    assert keep.tick() == 1 and keep.tick() == 0
    assert started(store, item["id"])[0]["config"]["check_back"]["late_by_seconds"] >= HOUR


def test_the_services_scheduler_starts_them(tmp_path):
    store = Store(tmp_path / "bots.db")
    _bot, chat = owl(store)
    clock = Clock()
    source = ask(store, chat)
    keeper(store, clock).set(source, request(after_seconds=60))
    store.finish(OWNER, source["id"], "completed", content="Εντάξει.")
    clock.now += 60
    rt = runtime(store, Model([]), clock)
    ticks = []

    def tick():
        ticks.append(rt.check_backs._fire(clock.now))
        rt._stop.set()
        return ticks[-1]

    rt.check_backs.tick = tick
    rt._schedule_loop()
    assert ticks == [1]


def test_the_rulebook_says_it_in_one_short_line():
    text = guidance(
        bot={
            "name": "Γλαύκα",
            "purpose": "",
            "instructions": "",
            "memory_enabled": True,
            "files_enabled": True,
        },
        plugins=False,
        browser=False,
        web_tools=False,
        desktop=False,
        coding_workspace=False,
        about_me="",
    )
    lines = [line for line in text.splitlines() if "check_back" in line]
    assert lines == ["- Never wait in a turn: to come back later, call check_back and end it."]


# ---------------------------------------------------------------------------------------------
# A moved owl keeps them; a deleted one takes them with it


def test_waiting_check_backs_travel_with_a_moved_owl(tmp_path):
    from test_bots_owl_move import copy_files, records_of, send_pages, staged

    source = Store(tmp_path / "here" / "bots.sqlite3")
    target = Store(tmp_path / "there" / "bots.sqlite3")
    bot, chat = owl(source)
    clock = Clock()
    keep = keeper(source, clock)
    # One that already started a turn, which ended: its turn moves and still says why it came.
    first = ask(source, chat)
    fired = keep.set(first, request(after_seconds=60))
    source.finish(OWNER, first["id"], "completed", content="Εντάξει.")
    clock.now += 60
    keep.tick()
    chained = source.claim(OWNER)
    source.finish(OWNER, chained["id"], "completed", content="Κοίταξα.")
    keep.tick()
    # Background work that ended before the move.
    work = ask(source, chat, "Στο παρασκήνιο.")
    BackgroundStore(source, OWNER).submit(work, "Δουλειά", [])
    waiting_turn = ask(source, chat, "Και τα υπόλοιπα;")
    with source.connect() as db:
        before = owl_fingerprint(db, bot["id"])
    items = [
        keep.set(waiting_turn, request(after_seconds=86400)),
        keep.set(
            waiting_turn,
            CheckBackRequest(
                note=CHINESE_NOTE,
                after_seconds=2 * 86400,
                condition={"kind": "page_lacks", "url": "https://example.invalid/", "text": "售罄"},
            ),
        ),
        keep.set(
            waiting_turn,
            request(after_seconds=3 * 86400, condition={"kind": "file_appears", "name": "*.pdf"}),
        ),
        keep.set(
            waiting_turn,
            request(
                after_seconds=4 * 86400, condition={"kind": "work_ends", "work_id": work["id"]}
            ),
        ),
    ]
    cancelled = keep.set(waiting_turn, request(after_seconds=5 * 86400))
    keep.cancel(chat["id"], cancelled["id"])
    source.finish(OWNER, waiting_turn["id"], "completed", content="Θα ξανακοιτάξω.")
    source.finish(OWNER, work["id"], "failed", error="Σταμάτησε.")
    with source.connect() as db:
        # Setting them is a change a move notices; a look at a condition isn't.
        changed = owl_fingerprint(db, bot["id"])
        assert changed != before
    clock.now += 100
    keep._check(clock.now)
    with source.connect() as db:
        assert owl_fingerprint(db, bot["id"]) == changed
    moves, move_id, pages = staged(source, target, bot["id"])
    moved_records = [r for r in records_of(pages) if r["kind"] == "check_back"]
    assert sorted(r["note"] for r in moved_records) == sorted(i["note"] for i in items)
    send_pages(moves, move_id, pages)
    assert copy_files(source, target, bot["id"], moves.receipt(move_id), pages) is not None
    # Nothing of it starts while the copy is still arriving.
    assert keeper(target, Clock(NOW + 10 * 86400)).tick() == 0
    done = moves.complete(move_id)
    source.delete_bot(
        OWNER, bot["id"], check=lambda db: require_unchanged(db, bot["id"], pages[0]["fingerprint"])
    )
    copy_id = done["owl"]["id"]
    moved_chat = target.list_chats(OWNER, copy_id)[0]
    # The page still shows the text there, so only the ended work brings a turn now.
    there = keeper(target, Clock(clock.now + HOUR), Pages(("状态：已售罄", True)))
    listed = there.list(moved_chat["id"])["check_backs"]
    assert sorted((c["due_at"], c["note"]) for c in listed) == sorted(
        (i["due_at"], i["note"]) for i in items
    )
    # Each still shows under the reply that set it.
    turns = target.conversation(OWNER, moved_chat["id"])["tasks"]
    under = [c for t in turns for c in t.get("check_backs", [])]
    assert len(under) == 4 and all(c["state"] == "pending" for c in under)
    # The turn a check-back started still says so.
    assert [t["checked_back"]["id"] for t in turns if t.get("checked_back")] == [fired["id"]]
    # The work it waited for ended before the move: that one comes back at once.
    assert there.tick() == 1
    # The original went with the move.
    with source.connect() as db:
        assert db.execute("SELECT count(*) FROM check_backs").fetchone()[0] == 0


def test_deleting_the_owl_erases_its_check_backs(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot, chat = owl(store)
    keep = keeper(store, Clock())
    source = ask(store, chat)
    keep.set(source, request(after_seconds=600))
    store.finish(OWNER, source["id"], "completed", content="Εντάξει.")
    store.delete_bot(OWNER, bot["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM check_backs").fetchone()[0] == 0
    with pytest.raises(NotFound):
        keep.list(chat["id"])


# ---------------------------------------------------------------------------------------------
# The app's routes, and a database from before check-backs


def test_routes_and_capability(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "check-backs.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    store, owner = app.state.store, settings.owner
    bot = store.save_bot(owner, BotInput(name="フクロウ"))
    chat = store.create_chat(owner, bot["id"], "Chat")
    store.enqueue(owner, chat["id"], TaskInput(message="一小时后再看看。", request_id=uuid4().hex))
    task = store.claim(owner)
    item = CheckBacks(store, owner).set(
        task, CheckBackRequest(note=CHINESE_NOTE, after_seconds=3600)
    )
    store.finish(owner, task["id"], "completed", content="好的。")
    base = f"/v1/chats/{chat['id']}/check-backs"
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "check_back" in client.get("/v1/status").json()["capabilities"]
        [listed] = client.get(base).json()["check_backs"]
        assert (listed["id"], listed["note"]) == (item["id"], CHINESE_NOTE)
        page = client.post(f"/v1/chats/{chat['id']}/page", json={}).json()
        assert page["tasks"][0]["check_backs"][0]["state"] == "pending"
        cancelled = client.post(f"{base}/{item['id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
        assert client.get(base).json() == {"check_backs": []}
        assert client.post(f"{base}/{uuid4().hex}/cancel").status_code == 404
        assert client.get(f"/v1/chats/{uuid4().hex}/check-backs").status_code == 404


def test_an_earlier_database_gains_check_backs_on_open(tmp_path):
    store = Store(tmp_path / "bots.db")
    _bot, chat = owl(store)
    with store.connect(write=True) as db:
        db.execute("DROP TABLE check_backs")
        db.execute("PRAGMA user_version=46")  # the schema before check-backs (47)
    reopened = Store(tmp_path / "bots.db")
    with reopened.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 47
    source = ask(reopened, chat)
    assert keeper(reopened, Clock()).set(source, request(after_seconds=600))["state"] == "pending"
