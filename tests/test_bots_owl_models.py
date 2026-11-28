"""The model each owl thinks with: kept with the owl, used by every kind of turn, never swapped.

Real store, runtime and peers; recording model clients. The HTTP test is
test_bots_owl_models_http.py (it needs FastAPI).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from alysis_code.bots.clarifications import AskQuestion, ClarificationStore, QuestionDecision
from alysis_code.bots.cooperation import PeerRequest, PeerRunner, PeerStore
from alysis_code.bots.models import BotCreateInput, BotInput, IntroInput, TaskInput
from alysis_code.bots.owl_models import ModelConnection, PlaceModels
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage

# A model the registry declares (vision, a large context and reply), and one it doesn't know.
ENDPOINT = "https://api.z.ai/api/coding/paas/v4"
SEEING = "glm-5.3-flash"
TEXT_ONLY = "glm-5.3"
PLACE = "place-model-unknown-to-the-registry"


def connection(model, *, default=False, label=None):
    settings = SimpleNamespace(
        model=model,
        base_url=ENDPOINT,
        protocol="openai_compat",
        provider_key=None,
        output_tokens=None,
    )
    return ModelConnection(model, label or model, settings, default=default)


class Recorder:
    """Every client made, by the model it thinks with; each answers in plain words."""

    def __init__(self):
        self.calls = []

    def client(self, model):
        recorder = self

        class Client:
            supports_tool_calling = True

            def __init__(self):
                self.model = model

            def chat(self, **kwargs):
                recorder.calls.append((model, kwargs.get("max_tokens")))
                return LLMResponse("Έτοιμο · 完了", [], {}, usage=LLMUsage(10, 5, 15))

        return Client()

    def models(self):
        return [model for model, _ in self.calls]


def place(store, recorder, offered=(SEEING, TEXT_ONLY)):
    """A runtime whose own model is PLACE and which offers `offered` too."""
    runtime = BotRuntime(store, "owner", lambda _task: recorder.client(PLACE))
    connections = [connection(PLACE, default=True), *(connection(model) for model in offered)]
    runtime.models = PlaceModels(
        lambda: list(connections), lambda _task, settings: recorder.client(settings.model)
    )
    return runtime


def run_next(store, runtime):
    task = store.claim("owner")
    runtime.run(task, EventCancellationToken(threading.Event()))
    return store.task("owner", task["id"])


def ask(store, chat, message="Γεια σου"):
    return store.enqueue("owner", chat["id"], TaskInput(message=message, request_id=uuid4().hex))


@pytest.fixture
def owl(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("owner", BotCreateInput(name="Ειδήσεις", model=SEEING))
    chat = store.create_chat("owner", bot["id"], "Chat")
    return store, bot, chat


# Kept with the owl


def test_a_picked_model_is_kept_and_listed_and_older_owls_have_none(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    picked = store.save_bot("alice", BotCreateInput(name="Νεφέλη", model="openrouter · 研究"))
    unpicked = store.save_bot("alice", BotInput(name="花子"))
    assert (picked["model"], unpicked["model"]) == ("openrouter · 研究", None)
    assert {bot["name"]: bot["model"] for bot in store.list_bots("alice")} == {
        "Νεφέλη": "openrouter · 研究",
        "花子": None,
    }
    assert Store(store.path).bot("alice", picked["id"])["model"] == "openrouter · 研究"


def test_a_change_without_a_model_keeps_it_and_null_returns_to_the_default(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Νεφέλη", model=SEEING, color="sky"))
    # An app from before models renames the owl without one: its model stays.
    renamed = store.save_bot("alice", BotInput(name="Σύννεφο", color="sky"), bot["id"])
    assert (renamed["name"], renamed["model"]) == ("Σύννεφο", SEEING)
    changed = store.save_bot("alice", BotInput(name="Σύννεφο", model=TEXT_ONLY), bot["id"])
    assert (changed["model"], changed["color"]) == (TEXT_ONLY, "sky")
    reset = store.save_bot("alice", BotInput(name="Σύννεφο", model=None), bot["id"])
    assert reset["model"] is None


def test_a_model_changes_at_once_even_while_the_owl_works(owl):
    store, bot, chat = owl
    ask(store, chat)
    assert store.claim("owner")
    # Like its name and look, a model is not part of the setup work was approved against.
    changed = store.save_bot("owner", BotInput(name="Ειδήσεις", model=TEXT_ONLY), bot["id"])
    assert changed["model"] == TEXT_ONLY
    assert changed["updated_at"] == bot["updated_at"]


@pytest.mark.parametrize("value", ["", "x" * 201, "two\nlines", "tab\tinside"])
def test_a_model_id_is_one_bounded_line(value):
    with pytest.raises(ValidationError):
        BotInput(name="Νεφέλη", model=value)


def test_a_deleted_owl_keeps_no_model(owl):
    store, bot, _ = owl
    store.delete_bot("owner", bot["id"])
    with store.connect() as db:
        assert db.execute("SELECT model FROM bots WHERE id=?", (bot["id"],)).fetchone()[0] is None


# Every kind of turn thinks with the owl's model


def test_a_chat_turn_thinks_with_the_owls_model_and_its_allowances(owl):
    store, _, chat = owl
    recorder = Recorder()
    ask(store, chat, "Τι νέα σήμερα;")
    finished = run_next(store, place(store, recorder))
    assert finished["status"] == "completed"
    fitted = RuntimeLimits.for_model(model=SEEING, base_url=ENDPOINT)
    assert recorder.calls == [(SEEING, fitted.output_tokens)]
    assert fitted.output_tokens != RuntimeLimits().output_tokens


def test_an_owl_without_a_model_thinks_with_the_places(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("owner", BotInput(name="Έρευνα"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    recorder = Recorder()
    ask(store, chat)
    runtime = place(store, recorder)
    assert run_next(store, runtime)["status"] == "completed"
    assert recorder.calls == [(PLACE, RuntimeLimits().output_tokens)]
    # Outside a turn the runtime reads the place's own allowances again.
    assert runtime.limits.output_tokens == RuntimeLimits().output_tokens


def test_the_places_own_model_is_read_again_for_every_turn(tmp_path):
    """The person chose another default model in the app while the owl works: its next turn
    thinks with it, with its allowances; every turn sends the whole conversation anyway."""
    store = Store(tmp_path / "bots.sqlite3")
    owls = [
        store.save_bot("owner", BotInput(name="Έρευνα")),
        store.save_bot("owner", BotInput(name="Σημειώσεις", model="place")),
    ]
    chats = [store.create_chat("owner", bot["id"], "Chat") for bot in owls]
    recorder = Recorder()
    started = ModelConnection("place", "place", connection(PLACE).settings, default=True)
    connections = [started]
    runtime = BotRuntime(store, "owner", lambda _task: recorder.client(PLACE + " (started)"))
    runtime.models = PlaceModels(
        lambda: list(connections), lambda _task, settings: recorder.client(settings.model)
    )
    runtime.place_settings = started.settings
    for chat in chats:
        ask(store, chat)
        assert run_next(store, runtime)["status"] == "completed"
    # Until it changes: the client the place started with, and its allowances.
    assert recorder.calls == [(PLACE + " (started)", RuntimeLimits().output_tokens)] * 2
    connections[0] = ModelConnection("place", "place", connection(SEEING).settings, default=True)
    for chat in chats:
        ask(store, chat, "Και τώρα;")
        assert run_next(store, runtime)["status"] == "completed"
    fitted = RuntimeLimits.for_model(model=SEEING, base_url=ENDPOINT)
    # The owl without a model and the one that picked the place's own both think with it now.
    assert recorder.calls[2:] == [(SEEING, fitted.output_tokens)] * 2
    assert runtime.limits.output_tokens == RuntimeLimits().output_tokens


def test_picking_the_places_own_model_is_the_place_client(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("owner", BotInput(name="Έρευνα", model=PLACE))
    ask(store, store.create_chat("owner", bot["id"], "Chat"))
    recorder = Recorder()
    assert run_next(store, place(store, recorder))["status"] == "completed"
    assert recorder.models() == [PLACE]


def test_routines_hellos_and_carrying_on_think_with_it_too(owl):
    store, bot, chat = owl
    recorder = Recorder()
    runtime = place(store, recorder)

    # Its first hello, written before anything else happens in its chat.
    store.start_intro("owner", bot["id"], IntroInput(request_id="intro_first", language="ja"))
    hello = run_next(store, runtime)
    assert hello["config"].get("intro") and hello["status"] == "completed"

    store.save_schedule(
        "owner",
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "Πρωινά νέα",
                "prompt": "Σύνοψη ειδήσεων",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules("owner", max_calls=None, now=4600) == 1
    routine = run_next(store, runtime)
    assert store.schedule_context("owner", routine["id"])
    assert routine["status"] == "completed"

    ask(store, chat, "Σχεδίασε την εβδομάδα")
    source = store.claim("owner")
    questions = ClarificationStore(store, "owner")
    question = questions.ask(source, AskQuestion(question="Για ποιον;"))
    store.finish("owner", source["id"], "completed", content="Πες μου πρώτα.")
    questions.decide(
        bot["id"], question["id"], QuestionDecision(action="close", expected_revision=1)
    )
    carried = run_next(store, runtime)
    assert carried["config"].get("skipped_question") and carried["status"] == "completed"

    assert recorder.models() == [SEEING, SEEING, SEEING]


def test_another_owl_answers_an_exchange_with_its_own_model(owl):
    store, asking, chat = owl
    helper = store.save_bot("owner", BotCreateInput(name="Βοηθός", model=TEXT_ONLY))
    ask(store, chat, "Ρώτα τη Βοηθό")
    source = store.claim("owner")
    peers = PeerStore(store, "owner")
    peers.submit(source, PeerRequest(recipient_bot_id=helper["id"], brief="Έλεγξε"), {}, 8)
    recorder = Recorder()
    runtime = place(store, recorder)
    PeerRunner(
        peers, runtime.client_factory, runtime.limits, threading.Event(), turn=runtime.turn_model
    ).run(peers.claim())
    fitted = RuntimeLimits.for_model(model=TEXT_ONLY, base_url=ENDPOINT)
    assert recorder.calls == [(TEXT_ONLY, fitted.output_tokens)]
    assert peers.for_task(source["id"])["status"] == "completed"


# A model no longer offered fails the turn; nothing thinks with another model instead


def test_a_model_no_longer_offered_fails_the_turn_with_kind_model(owl):
    store, _, chat = owl
    recorder = Recorder()
    ask(store, chat, "Τι νέα;")
    # The key for SEEING was removed: the place offers only its own model and TEXT_ONLY.
    failed = run_next(store, place(store, recorder, offered=(TEXT_ONLY,)))
    assert failed["status"] == "failed"
    assert failed["failure"] == {"kind": "model"}
    assert SEEING in failed["error"] and "settings" in failed["error"]
    assert recorder.calls == []


def test_a_place_that_offers_no_other_models_fails_it_too(owl):
    store, _, chat = owl
    recorder = Recorder()
    ask(store, chat)
    runtime = BotRuntime(store, "owner", lambda _task: recorder.client(PLACE))
    failed = run_next(store, runtime)
    assert (failed["status"], failed["failure"]) == ("failed", {"kind": "model"})
    assert recorder.calls == []


def test_a_routine_whose_model_went_away_waits_for_the_person(owl):
    store, bot, _ = owl
    item = store.save_schedule(
        "owner",
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "Πρωινά νέα",
                "prompt": "Σύνοψη",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules("owner", max_calls=None, now=4600) == 1
    recorder = Recorder()
    failed = run_next(store, place(store, recorder, offered=()))
    assert failed["failure"] == {"kind": "model"}
    routine = next(s for s in store.list_schedules("owner", bot["id"]) if s["id"] == item["id"])
    assert routine["state"] == "paused" and routine["pause_code"] == "setting"
    assert recorder.calls == []


def test_an_exchange_whose_owl_lost_its_model_fails_without_another_model(owl):
    store, _, chat = owl
    helper = store.save_bot("owner", BotCreateInput(name="Βοηθός", model="gone"))
    ask(store, chat)
    source = store.claim("owner")
    peers = PeerStore(store, "owner")
    peers.submit(source, PeerRequest(recipient_bot_id=helper["id"], brief="Έλεγξε"), {}, 8)
    recorder = Recorder()
    runtime = place(store, recorder)
    PeerRunner(
        peers, runtime.client_factory, runtime.limits, threading.Event(), turn=runtime.turn_model
    ).run(peers.claim())
    exchange = peers.for_task(source["id"])
    assert exchange["status"] == "failed" and "gone" in exchange["error"]
    assert recorder.calls == []


# What the place offers


def test_the_place_lists_its_models_with_their_declared_capacities():
    offered = PlaceModels(
        lambda: [
            connection(TEXT_ONLY, label="Ελληνικό όνομα"),
            connection(SEEING, default=True),
        ],
        lambda _task, settings: None,
    )
    listed = offered.list()
    assert [item["id"] for item in listed] == [SEEING, TEXT_ONLY]
    assert listed[0]["default"] is True and listed[1]["default"] is False
    assert listed[1]["label"] == "Ελληνικό όνομα"
    # From the registry's declared capacities, not from the names.
    assert (listed[0]["image_input"], listed[1]["image_input"]) == (True, False)
    assert listed[0]["context_window"] == 1_000_000
    # Neither connection declares image generation (owl_images.py).
    assert (listed[0]["image_generation"], listed[1]["image_generation"]) == (False, False)
    # Credentials and endpoints are never listed; each model names its connection.
    assert set(listed[0]) == {
        "id",
        "label",
        "model",
        "default",
        "image_input",
        "context_window",
        "image_generation",
        "reasoning",
        "reasoning_efforts",
        "connection",
        "connection_label",
        "model_label",
        "available",
        "unavailable",
    }
    assert (listed[1]["connection"], listed[1]["connection_label"]) == (
        TEXT_ONLY,
        "Ελληνικό όνομα",
    )
    assert offered.resolve("missing") is None and not offered.offers("missing")
