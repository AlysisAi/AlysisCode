"""About me: the person's own words about themselves, read by every one of their owls.

Real store, runtime and peers; scripted model. The HTTP test at the end needs FastAPI.
"""

from __future__ import annotations

import json
import threading
from uuid import uuid4

import pytest
from bots_task_state import task_state
from pydantic import ValidationError

from alysis_code.bots.about_me import (
    MAX_FACT_CHARACTERS,
    MAX_FACTS,
    MAX_SUGGESTIONS_PER_REPLY,
    TOKEN_BUDGET,
    AboutMeDecision,
    AboutMeSettings,
    AboutMeStore,
    AboutMeSuggestion,
    within_budget,
)
from alysis_code.bots.clarifications import AskQuestion, ClarificationStore, QuestionDecision
from alysis_code.bots.cooperation import PeerRequest, PeerStore
from alysis_code.bots.models import BotInput, IntroInput, MemoryInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, tool_schema
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall
from alysis_code.token_budget import estimate_tokens

GREEK = "Με λένε Ελένη και είμαι νοσηλεύτρια στη Θεσσαλονίκη."
JAPANESE = "回答は短く、箇条書きでお願いします。"
LABEL = "About me, written by the person about themselves"


class ScriptedClient:
    model = "test-model"
    supports_tool_calling = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(
            json.loads(json.dumps({"messages": kwargs["messages"], "tools": kwargs.get("tools")}))
        )
        return next(self.responses)


def response(content="", calls=()):
    return LLMResponse(content, list(calls), {}, usage=LLMUsage(10, 5, 15))


def offered(request) -> set[str]:
    return {tool["function"]["name"] for tool in request.get("tools") or []}


def write(about: AboutMeStore, facts, version="v1"):
    return about.write(
        AboutMeSettings(facts=facts, version=version, expected_revision=about.read()["revision"])
    )


@pytest.fixture
def owl(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Κουκουβάγια", purpose="Βοηθός"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    return store, bot, chat


def enqueue(store, chat, message="Γεια σου"):
    return store.enqueue("owner", chat["id"], TaskInput(message=message, request_id=uuid4().hex))


def run(store, client):
    task = store.claim("owner")
    BotRuntime(store, "owner", lambda _: client).run(
        task, EventCancellationToken(threading.Event())
    )
    return store.task("owner", task["id"])


# Storage, validation and limits


def test_about_me_is_kept_per_owner_in_order_and_survives_reopening(tmp_path):
    store = Store(tmp_path / "bots.db")
    mine, theirs = AboutMeStore(store, "owner"), AboutMeStore(store, "other")
    assert mine.read()["facts"] == [] and mine.version() is None and not mine.ready()
    written = write(mine, [GREEK, JAPANESE, "I prefer metric units."], version="a1")
    assert written["facts"] == [GREEK, JAPANESE, "I prefer metric units."]
    assert (written["version"], written["revision"], written["added"]) == ("a1", 1, [])
    assert written["limits"] == {
        "facts": MAX_FACTS,
        "characters": MAX_FACT_CHARACTERS,
        "tokens": TOKEN_BUDGET,
    }
    write(theirs, ["Someone else entirely."], version="b1")
    # Re-ordered by the person: the order is theirs.
    write(mine, [JAPANESE, GREEK], version="a2")
    reopened = AboutMeStore(Store(tmp_path / "bots.db"), "owner")
    assert reopened.read()["facts"] == [JAPANESE, GREEK]
    assert reopened.version() == "a2" and reopened.read()["revision"] == 2
    assert AboutMeStore(store, "other").facts() == ["Someone else entirely."]
    assert AboutMeStore(store, "nobody").facts() is None
    # An empty About me is still one the app keeps: owls may suggest additions to it.
    write(mine, [], version="a3")
    assert mine.facts() == [] and mine.ready()


def test_a_write_from_an_outdated_read_is_refused(owl):
    store, _, _ = owl
    about = AboutMeStore(store, "owner")
    write(about, [GREEK])
    with pytest.raises(Conflict):
        about.write(AboutMeSettings(facts=[JAPANESE], version="v2", expected_revision=0))
    assert about.facts() == [GREEK]


@pytest.mark.parametrize(
    "body",
    [
        {"facts": ["Two\nlines"], "version": "v", "expected_revision": 0},
        {"facts": ["Tab\tinside"], "version": "v", "expected_revision": 0},
        {"facts": ["Separator inside"], "version": "v", "expected_revision": 0},
        {"facts": ["   "], "version": "v", "expected_revision": 0},
        {"facts": ["α" * (MAX_FACT_CHARACTERS + 1)], "version": "v", "expected_revision": 0},
        {"facts": ["fact"] * (MAX_FACTS + 1), "version": "v", "expected_revision": 0},
        {"facts": [GREEK], "version": "not a version!", "expected_revision": 0},
        {"facts": [GREEK], "version": "v", "expected_revision": -1},
        {"facts": [GREEK], "version": "v"},
        {"facts": [GREEK], "version": "v", "expected_revision": 0, "owner": "other"},
    ],
)
def test_facts_are_short_single_lines_and_capped(body):
    with pytest.raises(ValidationError):
        AboutMeSettings.model_validate(body)


def test_limits_hold_at_their_edge_in_any_script():
    longest = "日" * MAX_FACT_CHARACTERS
    settings = AboutMeSettings(
        facts=[f"  {longest}  ", *[f"Γεγονός {n}" for n in range(MAX_FACTS - 1)]],
        version="v",
        expected_revision=0,
    )
    assert settings.facts[0] == longest and len(settings.facts) == MAX_FACTS
    # Joiners inside a word (emoji, Persian, Indic scripts) are not line breaks.
    assert AboutMeSettings(facts=["👩‍⚕️ می‌خواهم"], version="v", expected_revision=0)


# What every turn reads


def test_every_chat_turn_reads_it_labelled_and_apart_from_the_owls_memories(owl):
    store, bot, chat = owl
    store.save_memory("owner", bot["id"], MemoryInput(title="Project", content="Ο κήπος"))
    enqueue(store, chat)
    runtime = BotRuntime(store, "owner", lambda _: None)
    task = store.claim("owner")
    before = runtime._context(task)
    assert LABEL not in before[0]["content"]

    write(AboutMeStore(store, "owner"), [GREEK, JAPANESE])
    messages = runtime._context(task)
    guidance, state = messages[0]["content"], task_state(messages)
    assert LABEL in guidance
    assert (
        json.dumps({"facts": [GREEK, JAPANESE]}, ensure_ascii=False, separators=(",", ":"))
        in guidance
    )
    # Kept apart from the owl's own memories, which stay in the task state.
    assert GREEK not in state and "Ο κήπος" in state
    assert "Ο κήπος" not in guidance.split(LABEL, 1)[1]
    # The same for every turn until the person changes it, so it can be cached.
    assert runtime._context(task)[0]["content"] == guidance


def test_a_long_about_me_is_trimmed_from_the_end_within_the_budget(owl):
    store, _, chat = owl
    facts = [
        f"{n:02d} " + ("Μου αρέσει η θάλασσα, τα βουνά και 日本の料理。" * 4)[:196]
        for n in range(30)
    ]
    written = write(AboutMeStore(store, "owner"), facts)
    fit = written["included"]
    assert 0 < fit < len(facts) and fit == within_budget(facts)
    # Whole facts, first ones first: everything kept fits, the next one would not.
    assert sum(estimate_tokens(f) for f in facts[:fit]) <= TOKEN_BUDGET
    assert sum(estimate_tokens(f) for f in facts[: fit + 1]) > TOKEN_BUDGET
    enqueue(store, chat)
    guidance = BotRuntime(store, "owner", lambda _: None)._context(store.claim("owner"))[0][
        "content"
    ]
    shown = {"facts": facts[:fit], "left_out": len(facts) - fit}
    assert json.dumps(shown, ensure_ascii=False, separators=(",", ":")) in guidance
    assert facts[fit] not in guidance


def test_routines_hellos_and_carrying_on_read_it_too(owl):
    store, bot, chat = owl
    write(AboutMeStore(store, "owner"), [GREEK])
    runtime = BotRuntime(store, "owner", lambda _: None)

    store.save_schedule(
        "owner",
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "Πρωινά",
                "prompt": "Σύνοψη",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules("owner", max_calls=10, now=4600) == 1
    routine = store.claim("owner")
    assert LABEL in runtime._context(routine)[0]["content"]
    assert GREEK in runtime._context(routine)[0]["content"]
    store.finish("owner", routine["id"], "completed", content="Έγινε")

    other = store.save_bot("owner", BotInput(name="Δεύτερη", purpose=""))
    store.start_intro("owner", other["id"], IntroInput(request_id="intro_second", language="ja"))
    intro = store.claim("owner")
    assert intro["config"].get("intro") and GREEK in runtime._context(intro)[0]["content"]
    store.finish("owner", intro["id"], "completed", content="こんにちは")


def test_carrying_on_after_a_skipped_question_reads_it(owl):
    store, bot, chat = owl
    write(AboutMeStore(store, "owner"), [JAPANESE])
    enqueue(store, chat, "Σχεδίασε την εβδομάδα")
    source = store.claim("owner")
    questions = ClarificationStore(store, "owner")
    question = questions.ask(source, AskQuestion(question="Για ποιον;"))
    store.finish("owner", source["id"], "completed", content="Πες μου πρώτα.")
    questions.decide(
        bot["id"], question["id"], QuestionDecision(action="close", expected_revision=1)
    )
    carried = store.claim("owner")
    assert carried["config"].get("skipped_question")
    assert JAPANESE in BotRuntime(store, "owner", lambda _: None)._context(carried)[0]["content"]


def test_owls_of_the_same_person_read_it_in_exchanges_and_nobody_else_does(owl):
    store, bot, chat = owl
    write(AboutMeStore(store, "owner"), [GREEK])
    helper = store.save_bot("owner", BotInput(name="Βοηθός", purpose="Έλεγχος"))
    enqueue(store, chat, "Ρώτα τη Βοηθό")
    source = store.claim("owner")
    peers = PeerStore(store, "owner")
    exchange = peers.submit(
        source, PeerRequest(recipient_bot_id=helper["id"], brief="Έλεγξε"), {}, 8
    )
    _, messages, fingerprint = peers.context(exchange["id"])
    system = messages[0]["content"]
    assert "written by the person who owns both owls" in system and GREEK in system
    # A changed About me is read again before the exchange is charged.
    write(AboutMeStore(store, "owner"), [GREEK, JAPANESE], version="v2")
    assert peers.context(exchange["id"])[2] != fingerprint

    # Another person's owl in the same service never sees it.
    stranger = store.save_bot("other", BotInput(name="Ξένη", purpose=""))
    theirs = store.create_chat("other", stranger["id"], "Chat")
    store.enqueue("other", theirs["id"], TaskInput(message="Ποιος είμαι;", request_id=uuid4().hex))
    their_task = store.claim("other")
    their_context = BotRuntime(store, "other", lambda _: None)._context(their_task)
    assert all(GREEK not in m["content"] for m in their_context if isinstance(m["content"], str))
    assert LABEL not in their_context[0]["content"]


# Suggestions


def test_the_owl_suggests_in_a_chat_and_nothing_is_added_without_a_yes(owl):
    store, bot, chat = owl
    about = AboutMeStore(store, "owner")
    write(about, [JAPANESE])
    enqueue(store, chat, GREEK)
    client = ScriptedClient(
        [
            response(calls=[ToolCall("s1", "suggest_about_me", {"fact": GREEK})]),
            response("Χάρηκα, Ελένη!"),
        ]
    )
    finished = run(store, client)
    assert finished["status"] == "completed"
    assert "suggest_about_me" in offered(client.requests[0])
    [tool_result] = [
        json.loads(m["content"]) for m in client.requests[1]["messages"] if m["role"] == "tool"
    ]
    assert tool_result["status"] == "pending"
    # Waiting under the reply; About me is unchanged until the person says yes.
    task = store.conversation("owner", chat["id"])["tasks"][-1]
    [card] = task["about_me_suggestions"]
    assert (card["fact"], card["status"]) == (GREEK, "pending")
    assert about.facts() == [JAPANESE] and about.version() == "v1"

    decided = about.decide(bot["id"], card["id"], AboutMeDecision(decision="accept"))
    assert decided["suggestion"]["status"] == "accepted"
    assert about.facts() == [JAPANESE, GREEK]
    view = about.read()
    assert view["added"] == [{"suggestion_id": card["id"], "fact": GREEK}]
    # No version: the app sees a change and takes the fact into its own copy.
    assert about.version() is None
    shown = store.conversation("owner", chat["id"])["tasks"][-1]["about_me_suggestions"]
    assert shown == [{**card, "status": "accepted"}]
    # Deciding again the same way changes nothing; the other way is refused.
    assert about.decide(bot["id"], card["id"], AboutMeDecision(decision="accept"))
    with pytest.raises(Conflict):
        about.decide(bot["id"], card["id"], AboutMeDecision(decision="decline"))
    assert about.facts() == [JAPANESE, GREEK]
    # The app writes its copy back, with the fact: added ends there.
    written = write(about, [JAPANESE, GREEK], version="v2")
    assert written["added"] == [] and about.version() == "v2"


def test_a_declined_suggestion_adds_nothing_and_is_not_offered_again(owl):
    store, bot, chat = owl
    about = AboutMeStore(store, "owner")
    write(about, [])
    runtime = BotRuntime(store, "owner", lambda _: None)
    enqueue(store, chat, JAPANESE)
    task = store.claim("owner")
    made = runtime._tool(task, "suggest_about_me", {"fact": JAPANESE}, ["suggest_about_me"])
    revision = about.read()["revision"]
    decided = about.decide(bot["id"], made["id"], AboutMeDecision(decision="decline"))
    assert decided["suggestion"]["status"] == "declined"
    assert about.facts() == [] and about.read()["revision"] == revision
    assert about.version() == "v1"
    # A declined card leaves the conversation; the same fact, however written, isn't offered again.
    store.finish("owner", task["id"], "completed", content="OK")
    assert "about_me_suggestions" not in store.conversation("owner", chat["id"])["tasks"][-1]
    enqueue(store, chat, JAPANESE)
    again = store.claim("owner")
    retried = runtime._tool(
        again, "suggest_about_me", {"fact": f"  {JAPANESE.upper()} "}, ["suggest_about_me"]
    )
    assert retried["status"] == "already_suggested"
    # Something About me already says is not suggested either.
    write(about, ["Ich heiße Jürgen."], version="v2")
    same = runtime._tool(
        again, "suggest_about_me", {"fact": "ICH HEISSE JÜRGEN."}, ["suggest_about_me"]
    )
    assert same["status"] == "already_in_about_me"


def test_suggestions_are_capped_per_reply_and_by_a_full_about_me(owl):
    store, bot, chat = owl
    about = AboutMeStore(store, "owner")
    write(about, [f"Γεγονός {n}" for n in range(MAX_FACTS - 1)])
    runtime = BotRuntime(store, "owner", lambda _: None)
    enqueue(store, chat)
    task = store.claim("owner")
    made = [
        runtime._tool(task, "suggest_about_me", {"fact": f"事実 {n}"}, ["suggest_about_me"])
        for n in range(MAX_SUGGESTIONS_PER_REPLY)
    ]
    with pytest.raises(LimitReached):
        runtime._tool(task, "suggest_about_me", {"fact": "もう一つ"}, ["suggest_about_me"])
    about.decide(bot["id"], made[0]["id"], AboutMeDecision(decision="accept"))
    assert len(about.facts()) == MAX_FACTS
    # Full: accepting another says so and adds nothing; the card stays for later.
    with pytest.raises(LimitReached, match="About me is full"):
        about.decide(bot["id"], made[1]["id"], AboutMeDecision(decision="accept"))
    assert len(about.facts()) == MAX_FACTS
    store.finish("owner", task["id"], "completed", content="OK")
    statuses = [
        s["status"]
        for s in store.conversation("owner", chat["id"])["tasks"][-1]["about_me_suggestions"]
    ]
    assert statuses == ["accepted", "pending", "pending"]
    enqueue(store, chat)
    later = store.claim("owner")
    assert (
        runtime._tool(later, "suggest_about_me", {"fact": "Νέο"}, ["suggest_about_me"])["status"]
        == "about_me_full"
    )


def test_only_the_owner_decides_and_only_while_the_reply_is_being_written(owl):
    store, bot, chat = owl
    about = AboutMeStore(store, "owner")
    write(about, [])
    enqueue(store, chat)
    task = store.claim("owner")
    made = AboutMeStore(store, "owner").suggest(task, AboutMeSuggestion(fact=GREEK))
    store.finish("owner", task["id"], "completed", content="OK")
    with pytest.raises(Conflict):
        about.suggest(task, AboutMeSuggestion(fact=JAPANESE))
    with pytest.raises(NotFound):
        AboutMeStore(store, "other").decide(
            bot["id"], made["id"], AboutMeDecision(decision="accept")
        )
    assert about.facts() == []
    # Deleting the owl removes its suggestions; About me itself stays the person's.
    write(about, [GREEK], version="v2")
    store.delete_bot("owner", bot["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM about_me_suggestions").fetchone()[0] == 0
    assert about.facts() == [GREEK]


def test_suggesting_is_offered_only_in_chats_with_an_app_that_keeps_about_me(owl):
    store, bot, chat = owl
    enqueue(store, chat)
    client = ScriptedClient([response("Γεια")])
    run(store, client)
    # No About me from the person's app yet: an older app could not show the card.
    assert "suggest_about_me" not in offered(client.requests[0])

    write(AboutMeStore(store, "owner"), [])
    store.save_schedule(
        "owner",
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "Ώρα",
                "prompt": "Πες την ώρα",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules("owner", max_calls=10, now=4600) == 1
    routine_client = ScriptedClient([response("12:00")])
    run(store, routine_client)
    assert routine_client.requests[0]["tools"]
    assert "suggest_about_me" not in offered(routine_client.requests[0])

    enqueue(store, chat, GREEK)
    chat_client = ScriptedClient([response("Χάρηκα")])
    run(store, chat_client)
    assert "suggest_about_me" in offered(chat_client.requests[0])


def test_the_tool_takes_one_fact_on_one_line():
    schema = tool_schema("suggest_about_me")["function"]
    assert schema["parameters"]["required"] == ["fact"]
    with pytest.raises(ValidationError):
        AboutMeSuggestion(fact="one\ntwo")
    with pytest.raises(ValidationError):
        AboutMeSuggestion(fact=GREEK, extra="no")


def test_about_me_over_http(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "about.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    store = app.state.store
    bot = store.save_bot(settings.owner, BotInput(name="Μίρα", purpose=""))
    chat = store.create_chat(settings.owner, bot["id"], "Chat")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        status = client.get("/v1/status").json()
        assert "about_me" in status["capabilities"] and status["about_me_version"] is None
        assert client.get("/v1/settings/about-me").json()["facts"] == []
        put = client.put(
            "/v1/settings/about-me",
            json={"facts": [GREEK, JAPANESE], "version": "app-1", "expected_revision": 0},
        )
        assert put.status_code == 200 and put.json()["revision"] == 1
        assert put.json()["included"] == 2
        assert client.get("/v1/status").json()["about_me_version"] == "app-1"
        stale = client.put(
            "/v1/settings/about-me",
            json={"facts": [], "version": "app-2", "expected_revision": 0},
        )
        assert stale.status_code == 409
        refused = client.put(
            "/v1/settings/about-me",
            json={"facts": ["a\nb"], "version": "app-2", "expected_revision": 1},
        )
        assert refused.status_code == 422

    # A reply that offered a fact, made while no worker runs.
    store.enqueue(settings.owner, chat["id"], TaskInput(message=GREEK, request_id=uuid4().hex))
    task = store.claim(settings.owner)
    made = AboutMeStore(store, settings.owner).suggest(
        task, AboutMeSuggestion(fact="Δουλεύω νύχτες.")
    )
    store.finish(settings.owner, task["id"], "completed", content="Εντάξει")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        page = client.post(f"/v1/chats/{chat['id']}/page", json={}).json()
        assert page["tasks"][-1]["about_me_suggestions"][0]["fact"] == "Δουλεύω νύχτες."
        path = f"/v1/bots/{bot['id']}/about-me/suggestions/{made['id']}"
        assert client.patch(path, json={"decision": "maybe"}).status_code == 422
        accepted = client.patch(path, json={"decision": "accept"})
        assert accepted.status_code == 200
        assert accepted.json()["about_me"]["facts"] == [GREEK, JAPANESE, "Δουλεύω νύχτες."]
        assert client.get("/v1/status").json()["about_me_version"] is None
        missing = client.patch(
            f"/v1/bots/{bot['id']}/about-me/suggestions/{uuid4().hex}",
            json={"decision": "decline"},
        )
        assert missing.status_code == 404
