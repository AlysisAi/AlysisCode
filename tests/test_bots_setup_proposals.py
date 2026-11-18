"""An owl proposes its own setup from the person's words; only the person's Apply changes it."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError

from alysis_code.bots.models import (
    BotInput,
    ConversationPageQuery,
    IntroInput,
    TaskInput,
    TimeZoneSettings,
)
from alysis_code.bots.proposals import SetupDecision, SetupProposal, SetupProposals
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits, tool_schema
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store, encode
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

OWNER = "owner"

# The person's words, in Greek and Japanese: the owl writes its setup in their language.
GREEK = {
    "purpose": "Συγκεντρώνει τα τιμολόγια από τα εισερχόμενα σε ένα φύλλο",
    "instructions": "Κάθε φορά έλεγξε τα νέα μηνύματα για τιμολόγια.\n"
    "Πρόσθεσε ημερομηνία, προμηθευτή και ποσό στο φύλλο «Τιμολόγια».",
    "routine": {
        "name": "Πρωινά τιμολόγια",
        "prompt": "Έλεγξε τα εισερχόμενα για νέα τιμολόγια και πρόσθεσέ τα στο φύλλο.",
        "timing": {"kind": "daily", "time": "08:00"},
    },
}
JAPANESE = {
    "purpose": "毎週の売上レポートをまとめる",
    "instructions": "売上ファイルを読み、合計と前週比を短くまとめる。",
}


def owl(tmp_path, **setup):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot(OWNER, BotInput(name="Γλαύκα", **setup))
    chat = store.create_chat(OWNER, bot["id"], "Chat")
    store.time_zone_settings(OWNER, TimeZoneSettings(time_zone="Europe/Athens"))
    return store, bot, chat


def ask(store, chat, text):
    task = store.enqueue(OWNER, chat["id"], TaskInput(message=text, request_id=uuid4().hex))
    assert store.claim(OWNER)["id"] == task["id"]
    return store.task(OWNER, task["id"])


def reply(store, task, text="Να ρυθμιστώ έτσι;"):
    store.finish(OWNER, task["id"], "completed", content=text)


def runtime(store):
    return BotRuntime(store, OWNER, lambda _: None)


def propose(rt, task, arguments):
    return rt._tool(task, "propose_setup", arguments, ["propose_setup"])


def card(store, chat, task_id):
    tasks = store.conversation(OWNER, chat["id"])["tasks"]
    return next(task for task in tasks if task["id"] == task_id).get("setup_proposal")


def apply(rt, bot, proposal, revision=1, **changes):
    return rt.setups.decide(
        bot["id"],
        proposal["id"],
        SetupDecision(action="apply", expected_revision=revision, **changes),
    )


def decline(rt, bot, proposal, revision=1):
    return rt.setups.decide(
        bot["id"], proposal["id"], SetupDecision(action="decline", expected_revision=revision)
    )


def test_the_tool_schema_is_one_object_every_provider_accepts():
    parameters = tool_schema("propose_setup")["function"]["parameters"]
    assert parameters["type"] == "object"
    assert not {"anyOf", "oneOf", "allOf"} & set(parameters)
    assert set(parameters["required"]) == {"purpose", "instructions"}
    # A daily routine takes the person's own time zone when the owl leaves it out.
    zoned = [d for d in parameters["$defs"].values() if "timezone" in d.get("properties", {})]
    assert zoned and all("timezone" not in d.get("required", []) for d in zoned)


@pytest.mark.parametrize(
    "arguments",
    [
        {"purpose": "", "instructions": "Κάτι"},
        {"purpose": "Δύο\nγραμμές", "instructions": "Κάτι"},
        {"purpose": "一行", "instructions": ""},
        {"purpose": "一行", "instructions": "手順", "routine": {"name": "x", "prompt": "y"}},
        {"purpose": "一行", "instructions": "手順", "extra": True},
    ],
)
def test_the_tool_refuses_what_is_not_a_setup(tmp_path, arguments):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "Ρύθμισε τον εαυτό σου")
    with pytest.raises(ValidationError):
        propose(runtime(store), task, arguments)
    assert card(store, chat, task["id"]) is None


def test_a_routine_in_the_past_or_without_a_known_time_zone_is_refused(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "Υπενθύμισέ μου")
    rt = runtime(store)
    past = {"kind": "once", "at": "2001-01-01T08:00:00+00:00"}
    with pytest.raises(Conflict, match="future time"):
        propose(rt, task, {**GREEK, "routine": {**GREEK["routine"], "timing": past}})
    with store.connect(write=True) as db:
        db.execute("DELETE FROM bot_time_zone_settings")
    with pytest.raises(Conflict, match="time zone is not known"):
        propose(rt, task, GREEK)
    assert card(store, chat, task["id"]) is None


def test_the_card_shows_what_changes_and_waits_for_the_person(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "Κάθε πρωί έλεγξε τα εισερχόμενα για τιμολόγια και βάλ' τα σε φύλλο")
    rt = runtime(store)
    receipt = propose(rt, task, GREEK)
    assert receipt["state"] == "pending" and receipt["revision"] == 1
    assert "Nothing about you has changed" in receipt["message"]
    # Nothing changed: not the owl, not its routines.
    assert store.bot(OWNER, bot["id"])["purpose"] == ""
    assert store.list_schedules(OWNER, bot["id"]) == []
    # While the owl still writes its reply, the card can't be answered and nobody is asked.
    shown = card(store, chat, task["id"])
    assert shown["status"] == "pending" and shown["ready"] is False
    assert store.needs_you(OWNER)["items"] == []
    with pytest.raises(Conflict, match="finish its reply"):
        apply(rt, bot, shown)
    reply(store, task)
    shown = card(store, chat, task["id"])
    assert shown["ready"] is True
    assert shown["before"] == {"purpose": "", "instructions": ""}
    assert (shown["purpose"], shown["instructions"]) == (GREEK["purpose"], GREEK["instructions"])
    assert shown["routine"] == {
        **GREEK["routine"],
        "timing": {"kind": "daily", "time": "08:00", "timezone": "Europe/Athens", "weekday": None},
    }
    # The paged read carries the same card, from the same decoration.
    page = store.conversation_page(OWNER, chat["id"], ConversationPageQuery())
    assert next(t for t in page["tasks"] if t["id"] == task["id"])["setup_proposal"] == shown
    [item] = store.needs_you(OWNER)["items"]
    assert item["kind"] == "proposal" and item["setup"] is True
    assert item["id"] == shown["id"] and item["task_id"] == task["id"]
    assert item["text"] == GREEK["purpose"]
    assert store.list_bots(OWNER)[0]["asks_you"] == {"kind": "approval", "text": GREEK["purpose"]}
    events = [e["kind"] for e in store.task(OWNER, task["id"])["events"]]
    assert "setup_proposed" in events


def test_apply_changes_exactly_purpose_instructions_and_the_routine_proposal(tmp_path):
    store, bot, chat = owl(tmp_path)
    store.save_bot(
        OWNER,
        BotInput(name="Γλαύκα", memory_enabled=False, files_enabled=True),
        bot["id"],
    )
    before = store.bot(OWNER, bot["id"])
    task = ask(store, chat, "Κάθε πρωί έλεγξε τα τιμολόγια")
    rt = runtime(store)
    proposal = propose(rt, task, GREEK)
    reply(store, task)
    applied = apply(rt, bot, proposal)
    after = store.bot(OWNER, bot["id"])
    assert after["purpose"] == GREEK["purpose"]
    assert after["instructions"] == GREEK["instructions"]
    unchanged = ("id", "name", "memory_enabled", "files_enabled", "created_at")
    assert {k: after[k] for k in unchanged} == {k: before[k] for k in unchanged}
    # The routine went through the routine proposal flow: a draft from that turn, never on.
    [routine] = store.list_schedules(OWNER, bot["id"])
    assert routine["state"] == "draft" and routine["next_run_at"] is None
    assert routine["source_task_id"] == task["id"]
    assert (routine["name"], routine["prompt"]) == (
        GREEK["routine"]["name"],
        GREEK["routine"]["prompt"],
    )
    assert routine["timing"]["timezone"] == "Europe/Athens"
    assert applied["status"] == "applied" and applied["revision"] == 2
    assert applied["applied"] == {
        "purpose": GREEK["purpose"],
        "instructions": GREEK["instructions"],
        "schedule_id": routine["id"],
    }
    assert card(store, chat, task["id"]) == applied
    # The routine now waits to be turned on, as any routine an owl suggests; the card doesn't.
    assert [(i["kind"], i["id"]) for i in store.needs_you(OWNER)["items"]] == [
        ("routine", routine["id"])
    ]
    assert "setup_applied" in [e["kind"] for e in store.task(OWNER, task["id"])["events"]]


def test_applying_twice_or_from_two_windows_makes_one_change(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "毎週の売上をまとめて")
    rt = runtime(store)
    proposal = propose(
        rt,
        task,
        {
            **JAPANESE,
            "routine": {
                "name": "週次レポート",
                "prompt": "売上をまとめる",
                "timing": {"kind": "weekly", "time": "09:00", "weekdays": [0]},
            },
        },
    )
    reply(store, task, "この設定でよろしいですか?")
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: apply(rt, bot, proposal), range(4)))
    assert {r["status"] for r in results} == {"applied"}
    assert len({r["applied"]["schedule_id"] for r in results}) == 1
    assert len(store.list_schedules(OWNER, bot["id"])) == 1
    assert store.bot(OWNER, bot["id"])["purpose"] == JAPANESE["purpose"]
    # The same answer again, with the revision the card had, returns the change already made.
    assert apply(rt, bot, proposal) == results[0]
    with pytest.raises(Conflict):
        decline(rt, bot, proposal, revision=2)


def test_a_stale_card_never_overwrites_newer_edits(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "Γίνε ο βοηθός μου για τα τιμολόγια")
    rt = runtime(store)
    proposal = propose(rt, task, {k: GREEK[k] for k in ("purpose", "instructions")})
    reply(store, task)
    with pytest.raises(Conflict, match="changed"):
        apply(rt, bot, proposal, revision=2)
    # The person edits the owl's setup by hand after the card appeared.
    store.save_bot(
        OWNER,
        BotInput(name="Γλαύκα", purpose="Δική μου περιγραφή", instructions="Δικές μου"),
        bot["id"],
    )
    assert card(store, chat, task["id"])["status"] == "outdated"
    with pytest.raises(Conflict, match="no longer current"):
        apply(rt, bot, proposal)
    with pytest.raises(Conflict, match="no longer current"):
        decline(rt, bot, proposal)
    current = store.bot(OWNER, bot["id"])
    assert (current["purpose"], current["instructions"]) == ("Δική μου περιγραφή", "Δικές μου")
    assert store.needs_you(OWNER)["items"] == []
    # A rename is only a label: it leaves a card current.
    other = ask(store, chat, "Ξανά, παρακαλώ")
    again = propose(rt, other, {k: GREEK[k] for k in ("purpose", "instructions")})
    reply(store, other)
    store.save_bot(
        OWNER,
        BotInput(name="Νέο όνομα", purpose="Δική μου περιγραφή", instructions="Δικές μου"),
        bot["id"],
    )
    assert apply(rt, bot, again)["status"] == "applied"
    assert store.bot(OWNER, bot["id"])["name"] == "Νέο όνομα"


def test_change_applies_the_persons_own_wording_and_can_leave_the_routine_out(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "Κάθε πρωί έλεγξε τα τιμολόγια")
    rt = runtime(store)
    proposal = propose(rt, task, GREEK)
    reply(store, task)
    applied = apply(
        rt,
        bot,
        proposal,
        purpose="Τιμολόγια μόνο από προμηθευτές",
        instructions="",
        include_routine=False,
    )
    after = store.bot(OWNER, bot["id"])
    assert (after["purpose"], after["instructions"]) == ("Τιμολόγια μόνο από προμηθευτές", "")
    assert store.list_schedules(OWNER, bot["id"]) == []
    assert applied["applied"]["schedule_id"] is None
    # The owl's own proposal stays as it was, beside what the person applied.
    assert applied["purpose"] == GREEK["purpose"]


def test_the_persons_routine_wording_goes_into_the_routine(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "毎朝メールを確認して")
    rt = runtime(store)
    proposal = propose(
        rt,
        task,
        {
            **JAPANESE,
            "routine": {
                "name": "朝の確認",
                "prompt": "メールを確認する",
                "timing": {"kind": "daily", "time": "07:30", "timezone": "Asia/Tokyo"},
            },
        },
    )
    reply(store, task)
    apply(rt, bot, proposal, routine_prompt="請求書だけを確認する")
    [routine] = store.list_schedules(OWNER, bot["id"])
    assert routine["prompt"] == "請求書だけを確認する"
    assert routine["timing"]["timezone"] == "Asia/Tokyo"


def test_apply_lands_whole_or_not_at_all(tmp_path):
    store, bot, chat = owl(tmp_path)
    timing = {"kind": "interval", "every_minutes": 60}
    for index in range(20):
        store.save_schedule(
            OWNER,
            bot["id"],
            ScheduleInput.model_validate({"name": f"r{index}", "prompt": "p", "timing": timing}),
        )
    task = ask(store, chat, "Κάθε πρωί έλεγξε τα τιμολόγια")
    rt = runtime(store)
    proposal = propose(rt, task, GREEK)
    reply(store, task)
    with pytest.raises(LimitReached):
        apply(rt, bot, proposal)
    # The routine couldn't be saved, so the purpose and instructions didn't change either.
    assert store.bot(OWNER, bot["id"])["purpose"] == ""
    assert card(store, chat, task["id"])["status"] == "pending"
    # Work in progress holds a setup change, exactly as for an edit in the owl's settings.
    busy = ask(store, chat, "Κάτι άλλο")
    with pytest.raises(Conflict, match="Wait for this bot's work"):
        apply(rt, bot, proposal, include_routine=False)
    reply(store, busy, "Έγινε.")
    assert apply(rt, bot, proposal, include_routine=False)["status"] == "applied"


def test_not_now_changes_nothing_and_the_same_setup_is_never_proposed_again(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "Κάθε πρωί έλεγξε τα τιμολόγια")
    rt = runtime(store)
    proposal = propose(rt, task, GREEK)
    reply(store, task)
    declined = decline(rt, bot, proposal)
    assert declined["status"] == "declined" and declined["applied"] is None
    assert decline(rt, bot, proposal) == declined
    with pytest.raises(Conflict):
        apply(rt, bot, proposal, revision=2)
    assert store.bot(OWNER, bot["id"])["purpose"] == ""
    assert store.list_schedules(OWNER, bot["id"]) == []
    assert store.needs_you(OWNER)["items"] == []
    later = ask(store, chat, "Ευχαριστώ, τι ώρα είναι;")
    # The same setup again, even with other letter case and spacing, is refused.
    with pytest.raises(Conflict, match="Not now"):
        propose(
            rt,
            later,
            {
                **GREEK,
                "purpose": "  " + GREEK["purpose"].upper(),
                "instructions": GREEK["instructions"].replace("\n", "\n\n "),
            },
        )
    assert card(store, chat, later["id"]) is None
    with pytest.raises(ValidationError):
        SetupDecision(action="decline", expected_revision=1, purpose="Άλλο")


def test_the_owls_state_says_what_it_proposed_and_what_the_person_chose(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "Κάθε πρωί έλεγξε τα τιμολόγια")
    rt = runtime(store)
    proposal = propose(rt, task, GREEK)
    reply(store, task)
    decline(rt, bot, proposal)
    [recent] = rt.setups.recent(bot["id"])
    assert (recent["id"], recent["status"]) == (proposal["id"], "declined")
    later = ask(store, chat, "Γεια")
    state = encode(rt._context(store.task(OWNER, later["id"])))
    assert "Setup suggestions you made" in state
    assert proposal["id"] in state and '\\"status\\":\\"declined\\"' in state


def test_a_turn_corrects_its_own_card_and_a_newer_card_replaces_a_waiting_one(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "Γίνε βοηθός τιμολογίων")
    rt = runtime(store)
    first = propose(rt, task, {k: GREEK[k] for k in ("purpose", "instructions")})
    assert propose(rt, task, {k: GREEK[k] for k in ("purpose", "instructions")})["revision"] == 1
    corrected = propose(rt, task, GREEK)
    assert corrected["id"] == first["id"] and corrected["revision"] == 2
    reply(store, task)
    with pytest.raises(Conflict, match="changed"):
        apply(rt, bot, first, revision=1)
    newer_task = ask(store, chat, "Άλλαξε το: μόνο τα σημαντικά τιμολόγια")
    newer = propose(rt, newer_task, {**JAPANESE})
    reply(store, newer_task)
    assert card(store, chat, task["id"])["status"] == "outdated"
    assert [i["id"] for i in store.needs_you(OWNER)["items"]] == [newer["id"]]
    with pytest.raises(Conflict, match="no longer current"):
        apply(rt, bot, first, revision=3)
    assert apply(rt, bot, newer)["status"] == "applied"
    # A finished turn can't put up another card.
    with pytest.raises(Conflict, match="person's own request"):
        propose(rt, store.task(OWNER, newer_task["id"]), JAPANESE)


def test_only_the_persons_own_request_can_propose(tmp_path):
    store, bot, chat = owl(tmp_path)
    setups = SetupProposals(store, OWNER)
    data = SetupProposal.model_validate({k: GREEK[k] for k in ("purpose", "instructions")})
    # A first hello has no request from the person.
    intro = store.start_intro(OWNER, bot["id"], IntroInput(request_id=uuid4().hex, language="el"))
    assert store.claim(OWNER)["id"] == intro["id"]
    with pytest.raises(Conflict):
        setups.propose(store.task(OWNER, intro["id"]), data)
    reply(store, intro, "Γεια!")
    # A routine's own run.
    routine = store.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate(
            {"name": "r", "prompt": "p", "timing": {"kind": "interval", "every_minutes": 60}}
        ),
    )
    run = store.run_schedule_now(OWNER, bot["id"], routine["id"], uuid4().hex, max_calls=None)
    assert store.claim(OWNER)["id"] == run["task_id"]
    with pytest.raises(Conflict):
        setups.propose(store.task(OWNER, run["task_id"]), data)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM setup_proposals").fetchone()[0] == 0


def test_nothing_to_apply_is_not_proposed(tmp_path):
    store, bot, chat = owl(
        tmp_path, purpose=JAPANESE["purpose"], instructions=JAPANESE["instructions"]
    )
    task = ask(store, chat, "設定を覚えて")
    with pytest.raises(Conflict, match="already your setup"):
        propose(runtime(store), task, JAPANESE)


def test_a_card_of_another_owl_or_a_missing_one_is_not_found(tmp_path):
    store, bot, chat = owl(tmp_path)
    other = store.save_bot(OWNER, BotInput(name="Άλλη"))
    task = ask(store, chat, "Γίνε βοηθός τιμολογίων")
    rt = runtime(store)
    proposal = propose(rt, task, {k: GREEK[k] for k in ("purpose", "instructions")})
    reply(store, task)
    with pytest.raises(NotFound):
        apply(rt, other, proposal)
    with pytest.raises(NotFound):
        apply(rt, bot, {"id": uuid4().hex})
    with pytest.raises(NotFound):
        SetupProposals(store, "someone-else").decide(
            bot["id"], proposal["id"], SetupDecision(action="apply", expected_revision=1)
        )


def test_deleting_the_owl_erases_its_setup_cards(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = ask(store, chat, "Γίνε βοηθός τιμολογίων")
    propose(runtime(store), task, {k: GREEK[k] for k in ("purpose", "instructions")})
    reply(store, task)
    store.delete_bot(OWNER, bot["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM setup_proposals").fetchone()[0] == 0


def test_an_earlier_database_gains_setup_cards_on_open(tmp_path):
    store, bot, chat = owl(tmp_path)
    with store.connect(write=True) as db:
        db.execute("DROP TABLE setup_proposals")
        db.execute("PRAGMA user_version=41")
    reopened = Store(tmp_path / "bots.db")
    task = ask(reopened, chat, "Γίνε βοηθός τιμολογίων")
    assert propose(runtime(reopened), task, JAPANESE)["state"] == "pending"


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


def test_in_a_chat_turn_the_owl_proposes_and_its_reply_carries_the_card(tmp_path):
    store, bot, chat = owl(tmp_path)
    task = store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(message="Κάθε πρωί έλεγξε τα τιμολόγια μου", request_id=uuid4().hex),
    )
    model = Model(
        [
            response(calls=[ToolCall("s", "propose_setup", GREEK)]),
            response("Σου πρότεινα μια ρύθμιση: δες την κάρτα."),
        ]
    )
    rt = BotRuntime(store, OWNER, lambda _: model, limits=RuntimeLimits(calls_per_task=8))
    rt.reply_review = None
    rt.run(store.claim(OWNER), EventCancellationToken(threading.Event()))
    assert store.task(OWNER, task["id"])["status"] == "completed"
    assert "propose_setup" in {t["function"]["name"] for t in model.requests[0]["tools"]}
    result = next(m for m in model.requests[1]["messages"] if m.get("role") == "tool")
    assert json.loads(result["content"])["state"] == "pending"
    shown = card(store, chat, task["id"])
    assert shown["ready"] is True and shown["routine"]["timing"]["timezone"] == "Europe/Athens"
    assert store.bot(OWNER, bot["id"])["purpose"] == ""
    # A routine's own run is not the person asking: it gets no setup tool.
    routine = store.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate(
            {"name": "r", "prompt": "p", "timing": {"kind": "interval", "every_minutes": 60}}
        ),
    )
    store.run_schedule_now(OWNER, bot["id"], routine["id"], uuid4().hex, max_calls=None)
    quiet = Model([response("Έγινε.")])
    rt = BotRuntime(store, OWNER, lambda _: quiet, limits=RuntimeLimits(calls_per_task=8))
    rt.reply_review = None
    rt.run(store.claim(OWNER), EventCancellationToken(threading.Event()))
    assert "propose_setup" not in {t["function"]["name"] for t in quiet.requests[0]["tools"]}


def test_the_apply_route_and_its_capability(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "setup.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    store, owner = app.state.store, settings.owner
    bot = store.save_bot(owner, BotInput(name="Γλαύκα"))
    chat = store.create_chat(owner, bot["id"], "Chat")
    store.enqueue(owner, chat["id"], TaskInput(message="Ρύθμισε", request_id=uuid4().hex))
    task = store.claim(owner)
    proposal = SetupProposals(store, owner).propose(
        task, SetupProposal.model_validate({k: GREEK[k] for k in ("purpose", "instructions")})
    )
    store.finish(owner, task["id"], "completed", content="Να ρυθμιστώ έτσι;")
    path = f"/v1/bots/{bot['id']}/setup-proposals/{proposal['id']}"
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "owl_setup" in client.get("/v1/status").json()["capabilities"]
        refused = client.patch(path, json={"action": "apply", "expected_revision": 1, "x": 1})
        assert refused.status_code == 422
        stale = client.patch(path, json={"action": "apply", "expected_revision": 5})
        assert stale.status_code == 409
        applied = client.patch(path, json={"action": "apply", "expected_revision": 1})
        assert applied.status_code == 200 and applied.json()["status"] == "applied"
        again = client.patch(path, json={"action": "apply", "expected_revision": 1})
        assert again.json() == applied.json()
        page = client.post(f"/v1/chats/{chat['id']}/page", json={}).json()
        assert page["tasks"][0]["setup_proposal"]["status"] == "applied"
        missing = client.patch(
            f"/v1/bots/{bot['id']}/setup-proposals/{uuid4().hex}",
            json={"action": "decline", "expected_revision": 1},
        )
        assert missing.status_code == 404
    assert store.bot(owner, bot["id"])["purpose"] == GREEK["purpose"]
