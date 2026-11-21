"""An owl offers a routine for a need the person shows repeats; only the person turns it on."""

from __future__ import annotations

import json
import threading
from uuid import uuid4

import pytest
from pydantic import ValidationError

from alysis_code.bots.models import (
    BotInput,
    IntroInput,
    RegenerateInput,
    TaskInput,
    TimeZoneSettings,
)
from alysis_code.bots.routine_suggestions import (
    RoutineSuggestionDecision,
    RoutineSuggestions,
    fingerprints,
)
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, NotFound, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

OWNER = "owner"
SITE = "https://shop.example"

# The person writes in Greek; the owl offers the routine in their language.
GREEK = {
    "name": "Πρωινά τιμολόγια",
    "prompt": "Έλεγξε τα εισερχόμενα για νέα τιμολόγια και γράψε μου ποια ήρθαν.",
    "timing": {"kind": "daily", "time": "08:00"},
}
JAPANESE = {
    "name": "毎朝の請求書チェック",
    "prompt": "受信箱の新しい請求書を確認して、一覧にする。",
    "timing": {"kind": "weekly", "time": "09:30", "weekdays": [0, 1, 2, 3, 4]},
}


def owl(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot(OWNER, BotInput(name="Γλαύκα", purpose="Βοηθός γραφείου"))
    chat = store.create_chat(OWNER, bot["id"], "Chat")
    store.time_zone_settings(OWNER, TimeZoneSettings(time_zone="Europe/Athens"))
    return store, bot, chat


def ask(store, chat, text):
    task = store.enqueue(OWNER, chat["id"], TaskInput(message=text, request_id=uuid4().hex))
    assert store.claim(OWNER)["id"] == task["id"]
    return store.task(OWNER, task["id"])


def reply(store, task, text="Θέλεις να το κάνω ρουτίνα;"):
    store.finish(OWNER, task["id"], "completed", content=text)


def given(timing):
    """A timing's fields that are set."""
    return {key: value for key, value in timing.items() if value is not None}


def runtime(store, **kwargs):
    return BotRuntime(store, OWNER, lambda _: None, **kwargs)


def suggest(rt, task, arguments):
    return rt._tool(task, "propose_schedule", arguments, ["propose_schedule"])


def card(store, chat, task_id):
    tasks = store.conversation(OWNER, chat["id"])["tasks"]
    return next(task for task in tasks if task["id"] == task_id).get("routine_suggestion")


def turn_on(rt, bot, shown, request_id="turn-on-0001", **changes):
    return rt.routine_suggestions.decide(
        bot["id"],
        shown["id"],
        RoutineSuggestionDecision(
            action="turn_on",
            expected_revision=shown["revision"],
            request_id=request_id,
            **changes,
        ),
    )


def decline(rt, bot, shown):
    return rt.routine_suggestions.decide(
        bot["id"],
        shown["id"],
        RoutineSuggestionDecision(action="decline", expected_revision=shown["revision"]),
    )


def offered(store, chat, text, arguments, rt=None):
    """The person's message, the owl's suggestion while it answers, then its reply."""
    rt = rt or runtime(store)
    task = ask(store, chat, text)
    result = suggest(rt, task, arguments)
    reply(store, task)
    return task, result, card(store, chat, task["id"])


def test_a_greek_conversation_gets_one_card_under_the_reply_and_nothing_runs(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    task = ask(store, chat, "Κάθε πρωί ελέγχω τα εισερχόμενα για τιμολόγια.")
    result = suggest(rt, task, GREEK)
    assert result["state"] == "draft" and "never say it is on" in result["message"]
    # While the owl still writes its reply the card is there, but can't be answered yet.
    waiting = card(store, chat, task["id"])
    assert (waiting["status"], waiting["ready"]) == ("open", False)
    with pytest.raises(Conflict, match="finish its reply"):
        turn_on(rt, bot, waiting)
    reply(store, task)
    shown = card(store, chat, task["id"])
    assert shown["status"] == "open" and shown["ready"] is True
    assert (shown["name"], shown["prompt"]) == (GREEK["name"], GREEK["prompt"])
    # The time is the person's own, from their time zone; it uses nothing beyond chat.
    assert given(shown["timing"]) == {"kind": "daily", "time": "08:00", "timezone": "Europe/Athens"}
    assert shown["to_allow"] is None
    assert shown["needs"] == {"offline_computer": False, "online_browser": False, "sites": []}
    # The Routines tab lists it as a draft; it never runs, and Needs you leaves it out.
    [draft] = store.list_schedules(OWNER, bot["id"])
    assert (draft["id"], draft["state"], draft["next_run_at"]) == (result["id"], "draft", None)
    assert shown["schedule_id"] == draft["id"]
    assert store.enqueue_due_schedules(OWNER, max_calls=10, now=draft["created_at"] + 90000) == 0
    assert store.needs_you(OWNER)["items"] == []


def test_the_same_turn_keeps_one_card_and_cannot_offer_a_second(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    task = ask(store, chat, "Κάθε πρωί ελέγχω τα εισερχόμενα για τιμολόγια.")
    first = suggest(rt, task, GREEK)
    assert suggest(rt, task, GREEK) == first
    with pytest.raises(Conflict, match="already suggested a routine in this turn"):
        suggest(rt, task, JAPANESE)
    assert len(store.list_schedules(OWNER, bot["id"])) == 1


def test_turn_on_twice_makes_one_routine_from_a_japanese_job(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    task, result, shown = offered(store, chat, "毎朝、請求書を確認しています。", JAPANESE)
    on = turn_on(rt, bot, shown)
    assert on["status"] == "on" and on["decided_at"] is not None
    again = turn_on(rt, bot, shown)
    assert again == on
    [routine] = store.list_schedules(OWNER, bot["id"])
    assert routine["id"] == result["id"] and routine["state"] == "active"
    assert routine["name"] == "毎朝の請求書チェック" and routine["next_run_at"] is not None
    assert routine["revision"] == 2
    # Another click (another window) after it was decided changes nothing.
    with pytest.raises(Conflict, match="already turned on"):
        turn_on(rt, bot, shown, request_id="turn-on-0002")
    with pytest.raises(Conflict, match="no longer open"):
        decline(rt, bot, on)
    # A decided card stays decided under the reply.
    assert card(store, chat, task["id"])["status"] == "on"
    assert [e["kind"] for e in store.task(OWNER, task["id"])["events"]].count(
        "routine_suggestion_turned_on"
    ) == 1


def test_not_now_removes_the_draft_and_the_same_offer_is_never_made_again(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    task, _result, shown = offered(store, chat, "Κάθε πρωί ελέγχω τα τιμολόγια.", GREEK)
    declined = decline(rt, bot, shown)
    assert declined["status"] == "declined" and declined["schedule_id"] is None
    assert decline(rt, bot, shown) == declined
    assert store.list_schedules(OWNER, bot["id"]) == []
    with pytest.raises(Conflict, match="no longer open"):
        turn_on(rt, bot, shown)
    # The same job and time, written in another case, form or spacing, is the same offer.
    variants = [
        {**GREEK, "name": GREEK["name"].upper()},
        {**GREEK, "prompt": "  " + GREEK["prompt"].upper().replace(" ", "   ") + " "},
        # Rewording the job alone still matches by its name, and the other way round.
        {**GREEK, "prompt": "Κοίτα αν ήρθαν τιμολόγια."},
        {**GREEK, "name": "Τιμολόγια κάθε πρωί"},
        {**GREEK, "timing": {**GREEK["timing"], "timezone": "Europe/Athens"}},
    ]
    for variant in variants:
        task = ask(store, chat, "Πάλι τα τιμολόγια.")
        with pytest.raises(Conflict, match="Not now"):
            suggest(rt, task, variant)
        reply(store, task, "Εντάξει.")
    # Something else, or the same job at another time, can still be offered.
    task, _result, other = offered(
        store, chat, "Κάθε βράδυ;", {**GREEK, "timing": {"kind": "daily", "time": "20:00"}}
    )
    assert other["status"] == "open"
    assert card(store, chat, shown["source_task_id"])["status"] == "declined"


def test_full_width_and_case_forms_share_one_fingerprint():
    weekly = {"kind": "weekly", "time": "09:30", "timezone": "Asia/Tokyo", "weekdays": [2]}
    one_day = {"kind": "weekly", "time": "09:30", "timezone": "Asia/Tokyo", "weekday": 2}
    assert fingerprints("ＲＥＰＯＲＴ", "請求書", weekly) == fingerprints(
        "report", "請求書", one_day
    )
    assert (
        fingerprints("report", "請求書", weekly)[0]
        != fingerprints("report", "請求書", {**weekly, "time": "10:00"})[0]
    )


def test_a_routine_the_owl_already_has_is_not_offered(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    store.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate(
            {**GREEK, "timing": {**GREEK["timing"], "timezone": "Europe/Athens"}}
        ),
    )
    task = ask(store, chat, "Κάθε πρωί ελέγχω τα τιμολόγια.")
    with pytest.raises(Conflict, match="already has this routine"):
        suggest(rt, task, GREEK)


def test_a_newer_suggestion_replaces_the_open_one(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    _task, first_result, first = offered(store, chat, "Κάθε πρωί τα τιμολόγια.", GREEK)
    _task, second_result, second = offered(store, chat, "毎朝、請求書。", JAPANESE)
    assert second["status"] == "open"
    replaced = card(store, chat, first["source_task_id"])
    assert replaced["status"] == "closed" and replaced["revision"] == 2
    assert [s["id"] for s in store.list_schedules(OWNER, bot["id"])] == [second_result["id"]]
    assert first_result["id"] != second_result["id"]
    with pytest.raises(Conflict, match="no longer open"):
        turn_on(rt, bot, first)
    # A replaced suggestion was never declined: it may be offered again later.
    _task, _result, again = offered(store, chat, "Τα τιμολόγια;", GREEK)
    assert again["status"] == "open"


def test_a_suggestion_needing_a_website_goes_through_routine_access(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store, browser_computers=object())
    arguments = {**GREEK, "needs": {"sites": [SITE]}}
    _task, result, shown = offered(store, chat, "Κάθε πρωί ελέγχω τις τιμές.", arguments, rt)
    expected = {"offline_computer": False, "online_browser": True, "sites": [SITE]}
    assert shown["needs"] == expected and shown["to_allow"] == expected
    # Turn on asks first: without the person allowing exactly what the card showed, it stays off.
    with pytest.raises(Conflict, match="allow it"):
        turn_on(rt, bot, shown)
    with pytest.raises(Conflict, match="allow it"):
        turn_on(rt, bot, shown, allow={"online_browser": True})
    assert store.list_schedules(OWNER, bot["id"])[0]["state"] == "draft"
    on = turn_on(rt, bot, shown, allow=expected)
    assert on["status"] == "on" and on["to_allow"] is None
    [routine] = store.list_schedules(OWNER, bot["id"])
    assert routine["id"] == result["id"] and routine["state"] == "active"
    assert routine["access"]["allowed"] == expected and routine["access"]["to_allow"] is None


def test_change_turns_on_the_persons_own_version(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    _task, result, shown = offered(store, chat, "Κάθε πρωί τα τιμολόγια.", GREEK)
    timing = {"kind": "weekly", "time": "07:15", "timezone": "Europe/Athens", "weekdays": [0, 3]}
    on = turn_on(rt, bot, shown, name="Τιμολόγια Δευτέρας", timing=timing)
    assert (on["status"], on["name"], given(on["timing"])) == ("on", "Τιμολόγια Δευτέρας", timing)
    assert on["prompt"] == GREEK["prompt"]
    [routine] = store.list_schedules(OWNER, bot["id"])
    assert (routine["id"], routine["state"]) == (result["id"], "active")
    assert given(routine["timing"]) == timing and routine["name"] == "Τιμολόγια Δευτέρας"


def test_a_stale_or_unknown_card_is_refused(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    _task, _result, shown = offered(store, chat, "Κάθε πρωί τα τιμολόγια.", GREEK)
    with pytest.raises(Conflict, match="changed"):
        turn_on(rt, bot, {**shown, "revision": 7})
    with pytest.raises(NotFound):
        turn_on(rt, bot, {**shown, "id": uuid4().hex})
    other = store.save_bot(OWNER, BotInput(name="Άλλη"))
    with pytest.raises(NotFound):
        turn_on(rt, other, shown)
    with pytest.raises(NotFound):
        RoutineSuggestions(store, "someone-else").decide(
            bot["id"],
            shown["id"],
            RoutineSuggestionDecision(action="decline", expected_revision=1),
        )
    for wrong in (
        {"action": "turn_on", "expected_revision": 1},
        {"action": "decline", "expected_revision": 1, "name": "x"},
        {"action": "decline", "expected_revision": 1, "request_id": "turn-on-0001"},
        {"action": "turn_on", "expected_revision": 1, "request_id": "turn-on-1", "x": 1},
    ):
        with pytest.raises(ValidationError):
            RoutineSuggestionDecision.model_validate(wrong)


def test_the_card_follows_what_the_person_did_in_routines(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    _task, result, shown = offered(store, chat, "Κάθε πρωί τα τιμολόγια.", GREEK)
    store.decide_schedule(OWNER, bot["id"], result["id"], "active", 1)
    assert card(store, chat, shown["source_task_id"])["status"] == "on"
    _task, result, other = offered(store, chat, "毎朝、請求書。", JAPANESE)
    store.delete_schedule(OWNER, bot["id"], result["id"])
    assert card(store, chat, other["source_task_id"])["status"] == "closed"
    with pytest.raises(Conflict, match="no longer open"):
        turn_on(rt, bot, other)


def test_only_the_persons_own_message_can_lead_to_a_suggestion(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    # A first hello.
    intro = store.start_intro(OWNER, bot["id"], IntroInput(request_id=uuid4().hex, language="el"))
    assert store.claim(OWNER)["id"] == intro["id"]
    with pytest.raises(Conflict, match="person's own message"):
        suggest(rt, store.task(OWNER, intro["id"]), GREEK)
    reply(store, intro, "Γεια!")
    # Another answer to a message already answered.
    task = ask(store, chat, "Κάθε πρωί τα τιμολόγια.")
    reply(store, task)
    again = store.regenerate(OWNER, task["id"], RegenerateInput(request_id=uuid4().hex))
    assert store.claim(OWNER)["id"] == again["id"]
    with pytest.raises(Conflict, match="person's own message"):
        suggest(rt, store.task(OWNER, again["id"]), GREEK)
    reply(store, again)
    # A routine's own run.
    routine = store.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate(
            {"name": "r", "prompt": "p", "timing": {"kind": "interval", "every_minutes": 60}}
        ),
    )
    store.run_schedule_now(OWNER, bot["id"], routine["id"], uuid4().hex, max_calls=None)
    run = store.claim(OWNER)
    with pytest.raises(Conflict, match="person's own message"):
        suggest(rt, run, JAPANESE)
    store.finish(OWNER, run["id"], "completed", content="Έγινε.")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM routine_suggestions").fetchone()[0] == 0


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


def run(store, model):
    rt = BotRuntime(store, OWNER, lambda _: model, limits=RuntimeLimits(calls_per_task=8))
    rt.reply_review = None
    rt.run(store.claim(OWNER), EventCancellationToken(threading.Event()))
    return rt


def test_in_a_chat_turn_the_owl_offers_and_its_guidance_and_task_state_say_when(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(
            message="Κάθε πρωί ελέγχω τα εισερχόμενα για τιμολόγια.", request_id="message-0001"
        ),
    )
    model = Model(
        [
            response(calls=[ToolCall("s", "propose_schedule", GREEK)]),
            response("Θέλεις να το κάνω κάθε πρωί; Δες την κάρτα."),
        ]
    )
    run(store, model)
    assert store.task(OWNER, task["id"])["status"] == "completed"
    assert "propose_schedule" in model.tools()
    # When to offer one is said once, beside the tool the model reads in the same request.
    (offer,) = [
        t for t in model.requests[0]["tools"] if t["function"]["name"] == "propose_schedule"
    ]
    assert "UNSOLICITED routine for a need they show repeats" in offer["function"]["description"]
    assert "Only while answering the person's own message" in offer["function"]["description"]
    result = next(m for m in model.requests[1]["messages"] if m.get("role") == "tool")
    assert json.loads(result["content"])["state"] == "draft"
    shown = card(store, chat, task["id"])
    assert shown["status"] == "open" and shown["ready"] is True
    # The card speaks for the call: no status line repeats it.
    assert not [e for e in store.task(OWNER, task["id"])["events"] if e["kind"] == "progress_note"]
    decline(BotRuntime(store, OWNER, lambda _: None), bot, shown)
    # The next turn knows what became of it.
    store.enqueue(OWNER, chat["id"], TaskInput(message="Ευχαριστώ", request_id="message-0002"))
    later = Model([response("Παρακαλώ.")])
    run(store, later)
    state = "\n".join(str(m.get("content")) for m in later.requests[0]["messages"])
    assert "Routine suggestions you made" in state and '"status":"declined"' in state
    assert GREEK["name"] in state
    # Another answer to that message offers nothing new, though its other tools are there.
    latest = store.conversation(OWNER, chat["id"])["tasks"][-1]
    store.regenerate(OWNER, latest["id"], RegenerateInput(request_id=uuid4().hex))
    another = Model([response("Παρακαλώ πολύ.")])
    run(store, another)
    assert "propose_setup" in another.tools() and "propose_schedule" not in another.tools()
    # A first hello and a routine's own run are not the person asking: no suggestion tool.
    fresh = store.save_bot(OWNER, BotInput(name="Νέα"))
    store.start_intro(OWNER, fresh["id"], IntroInput(request_id=uuid4().hex, language="el"))
    hello = Model([response("Γεια!")])
    run(store, hello)
    assert "propose_schedule" not in hello.tools()
    routine = store.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate(
            {"name": "r", "prompt": "p", "timing": {"kind": "interval", "every_minutes": 60}}
        ),
    )
    store.run_schedule_now(OWNER, bot["id"], routine["id"], uuid4().hex, max_calls=None)
    quiet = Model([response("Έγινε.")])
    run(store, quiet)
    assert "propose_schedule" not in quiet.tools()


def test_deleting_the_owl_erases_its_suggestions(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    _task, _result, shown = offered(store, chat, "Κάθε πρωί τα τιμολόγια.", GREEK)
    decline(rt, bot, shown)
    offered(store, chat, "毎朝、請求書。", JAPANESE)
    store.delete_bot(OWNER, bot["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM routine_suggestions").fetchone()[0] == 0


def test_an_earlier_database_gains_suggestions_on_open(tmp_path):
    store, bot, chat = owl(tmp_path)
    with store.connect(write=True) as db:
        db.execute("DROP TABLE routine_suggestions")
        db.execute("PRAGMA user_version=43")
    reopened = Store(tmp_path / "bots.db")
    _task, _result, shown = offered(reopened, chat, "毎朝、請求書。", JAPANESE)
    assert shown["status"] == "open"


def test_the_decision_route_and_its_capability(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "suggest.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    store, owner = app.state.store, settings.owner
    store.time_zone_settings(owner, TimeZoneSettings(time_zone="Asia/Tokyo"))
    bot = store.save_bot(owner, BotInput(name="フクロウ"))
    chat = store.create_chat(owner, bot["id"], "Chat")
    store.enqueue(owner, chat["id"], TaskInput(message="毎朝、請求書。", request_id=uuid4().hex))
    task = store.claim(owner)
    BotRuntime(store, owner, lambda _: None)._tool(
        task, "propose_schedule", JAPANESE, ["propose_schedule"]
    )
    store.finish(owner, task["id"], "completed", content="ルーティンにしますか?")
    [shown] = [t["routine_suggestion"] for t in store.conversation(owner, chat["id"])["tasks"]]
    path = f"/v1/bots/{bot['id']}/routine-suggestions/{shown['id']}"
    body = {"action": "turn_on", "expected_revision": 1, "request_id": "turn-on-http-1"}
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "routine_suggestions" in client.get("/v1/status").json()["capabilities"]
        assert client.patch(path, json={**body, "extra": 1}).status_code == 422
        assert client.patch(path, json={**body, "expected_revision": 5}).status_code == 409
        on = client.patch(path, json=body)
        assert on.status_code == 200 and on.json()["status"] == "on"
        assert client.patch(path, json=body).json() == on.json()
        assert client.patch(path, json={**body, "request_id": "turn-on-http-2"}).status_code == 409
        page = client.post(f"/v1/chats/{chat['id']}/page", json={}).json()
        assert page["tasks"][0]["routine_suggestion"]["status"] == "on"
        missing = client.patch(
            f"/v1/bots/{bot['id']}/routine-suggestions/{uuid4().hex}",
            json={"action": "decline", "expected_revision": 1},
        )
        assert missing.status_code == 404
    assert store.list_schedules(owner, bot["id"])[0]["state"] == "active"


# ------------------------------------------------- the person acts on a routine while the owl works


class ActsWhileWriting(Model):
    """The person acts in Routines while the model writes response number ``during``."""

    def __init__(self, responses, act, during=(1,)):
        super().__init__(responses)
        self.act, self.during = act, set(during)

    def chat(self, **kwargs):
        if len(self.requests) in self.during:
            self.act()
        return super().chat(**kwargs)


ROUTINES_NOTE = "Routines note: the routines this turn saved or suggested, as they are now"


def routines_note(request):
    notes = [
        m["content"]
        for m in request["messages"]
        if m.get("role") == "system" and str(m.get("content", "")).startswith(ROUTINES_NOTE)
    ]
    assert len(notes) <= 1
    return json.loads(notes[0].split("\n", 1)[1].split("\n", 1)[0]) if notes else None


ROUTINES_CHANGED = "Routines changed while you worked"


def routine_update(request):
    """What the turn was told last about routines that changed under it, if anything: the
    update must come after every tool result, where the turn reads last."""
    messages = request["messages"]
    updates = [
        index
        for index, m in enumerate(messages)
        if m.get("role") == "system" and str(m.get("content", "")).startswith(ROUTINES_CHANGED)
    ]
    if not updates:
        return None
    last = updates[-1]
    assert all(m.get("role") != "tool" for m in messages[last + 1 :])
    content = messages[last]["content"]
    return json.loads(content.split(": ", 1)[1].rsplit(". Describe", 1)[0])


def rewritten(task):
    return [
        e
        for e in task["events"]
        if e["kind"] == "routine_refreshed" and e["data"]["response_discarded"]
    ]


def test_a_routine_turned_on_while_the_owl_writes_reaches_its_reply_as_on(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(
            message="Κάθε πρωί ελέγχω τα εισερχόμενα για τιμολόγια.", request_id="message-0101"
        ),
    )

    def turn_on_in_routines():
        [draft] = store.list_schedules(OWNER, bot["id"])
        store.decide_schedule(OWNER, bot["id"], draft["id"], "active", draft["revision"])

    model = ActsWhileWriting(
        [
            response(calls=[ToolCall("s", "propose_schedule", GREEK)]),
            # Written while the person turned it on: it describes a draft that is gone.
            response("Την αποθήκευσα ως πρόχειρη ρουτίνα· ενεργοποίησέ την."),
            response("Η ρουτίνα είναι πλέον ενεργή: κάθε πρωί στις 08:00."),
        ],
        turn_on_in_routines,
    )
    run(store, model)
    done = store.task(OWNER, task["id"])
    assert done["status"] == "completed"
    # The response written over the old state is neither published nor kept.
    assert done["output"] == "Η ρουτίνα είναι πλέον ενεργή: κάθε πρωί στις 08:00."
    assert len(rewritten(done)) == 1
    # Before any routine there is no note; after the suggestion, the draft; then what is true now.
    assert routines_note(model.requests[0]) is None
    [draft] = routines_note(model.requests[1])
    assert (draft["name"], draft["state"], draft["kind"]) == (GREEK["name"], "draft", "schedule")
    [now] = routines_note(model.requests[2])
    assert (now["id"], now["state"]) == (draft["id"], "active")
    # And it is said again last, after the tool result that said it was not on yet.
    assert routine_update(model.requests[1]) is None
    [update] = routine_update(model.requests[2])
    assert (update["name"], update["state"]) == (GREEK["name"], "active")
    assert len(model.requests) == 3


def test_a_routine_deleted_while_tools_run_is_read_as_gone_without_rewriting(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(message="毎朝、請求書を確認しています。", request_id="message-0102"),
    )
    model = Model(
        [
            response(calls=[ToolCall("s", "propose_schedule", JAPANESE)]),
            response(calls=[ToolCall("l", "list_schedules", {})]),
            response("ルーティンは削除されました。"),
        ]
    )
    rt = BotRuntime(store, OWNER, lambda _: model, limits=RuntimeLimits(calls_per_task=8))
    rt.reply_review = None
    deleted = []
    run_tool = rt._tool

    def tool(running, name, *args, **kwargs):
        result = run_tool(running, name, *args, **kwargs)
        if name == "list_schedules":
            # The person deletes the draft in Routines while the owl's tools run.
            [draft] = store.list_schedules(OWNER, bot["id"])
            store.delete_schedule(OWNER, bot["id"], draft["id"])
            deleted.append(draft["id"])
        return result

    rt._tool = tool
    rt.run(store.claim(OWNER), EventCancellationToken(threading.Event()))
    done = store.task(OWNER, task["id"])
    assert done["status"] == "completed" and done["output"] == "ルーティンは削除されました。"
    # Nothing was being written when it changed, so nothing is thrown away; the next request
    # reads it as gone.
    assert rewritten(done) == [] and len(model.requests) == 3
    assert [r["state"] for r in routines_note(model.requests[1])] == ["draft"]
    [gone] = routines_note(model.requests[2])
    assert (gone["id"], gone["state"]) == (deleted[0], "deleted")
    # The turn's own new draft is no news; its deletion is, said after the tool results.
    assert routine_update(model.requests[1]) is None
    assert [item["state"] for item in routine_update(model.requests[2])] == ["deleted"]


def test_a_routine_that_keeps_changing_rewrites_the_reply_a_bounded_number_of_times(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(
            message="Κάθε πρωί ελέγχω τα εισερχόμενα για τιμολόγια.", request_id="message-0103"
        ),
    )

    def toggle():
        [routine] = store.list_schedules(OWNER, bot["id"])
        state = "paused" if routine["state"] == "active" else "active"
        store.decide_schedule(OWNER, bot["id"], routine["id"], state, routine["revision"])

    model = ActsWhileWriting(
        [
            response(calls=[ToolCall("s", "propose_schedule", GREEK)]),
            response("Πρώτη."),
            response("Δεύτερη."),
            response("Τρίτη."),
        ],
        toggle,
        during=(1, 2, 3),
    )
    run(store, model)
    done = store.task(OWNER, task["id"])
    # Past the bound the response is kept: a busy Routines tab cannot loop the turn.
    assert done["status"] == "completed" and done["output"] == "Τρίτη."
    assert len(rewritten(done)) == 2


def test_the_note_names_a_suggestion_the_person_declined(tmp_path):
    store, bot, chat = owl(tmp_path)
    rt = runtime(store)
    task, result, shown = offered(store, chat, "Κάθε πρωί ελέγχω τιμολόγια.", GREEK, rt)
    assert [r["state"] for r in store.turn_routines(OWNER, task["id"])] == ["draft"]
    decline(rt, bot, shown)
    [declined] = store.turn_routines(OWNER, task["id"])
    # The declined draft leaves Routines; its card is what the note still names.
    assert (declined["id"], declined["name"], declined["state"]) == (
        result["suggestion_id"],
        GREEK["name"],
        "declined",
    )
