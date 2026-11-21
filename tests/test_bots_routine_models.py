"""A routine may think with a model of its own, picked from those its owl's place offers.

Its runs think with it (as it was when each run was queued), by the owl's own rules: a model no
longer offered fails the run, a catalog model no longer open gives way with the same notice. The
person picks it; the owl may give one only from what the place offers, and the person's approval
is still what turns the routine on. It moves with the owl only where the other place offers it.

Real store, runtime, service and moves; recording model clients.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotCreateInput, TaskInput
from alysis_code.bots.owl_models import (
    PENDING,
    CatalogModel,
    ModelCatalog,
    ModelConnection,
    PlaceModels,
)
from alysis_code.bots.owl_move import MoveBegin, MoveExport, MovePage, OwlMoves, export_page
from alysis_code.bots.routine_suggestions import RoutineSuggestionDecision
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.scheduling import ScheduleInput, ScheduleModelChoice
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.store import Conflict, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage

OWNER = "owner"
# Two models the registry declares differently on one endpoint: one sees images and reads a
# million tokens at once, the other is text only. The place's own model is one it doesn't know.
ENDPOINT = "https://api.z.ai/api/coding/paas/v4"
SEEING = "glm-5.3-flash"
TEXT_ONLY = "glm-5.3"
PLACE = "place-model-unknown-to-the-registry"
EVERY_HOUR = {"kind": "interval", "every_minutes": 60}


@dataclass(frozen=True)
class Endpoint:
    """A model connection's service settings; a catalog model changes only its model."""

    model: str
    base_url: str = ENDPOINT
    protocol: str = "openai_compat"
    provider_key: str | None = None
    output_tokens: int | None = None


def connection(model, *, default=False, connection_id=None, catalog=None):
    return ModelConnection(
        connection_id or model, connection_id or model, Endpoint(model), default, catalog
    )


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


def place(store, recorder, connections=None):
    """A runtime whose own model is PLACE and which offers SEEING and TEXT_ONLY too."""
    runtime = BotRuntime(store, OWNER, lambda _task: recorder.client(PLACE))
    offered = connections or [
        connection(PLACE, default=True),
        connection(SEEING),
        connection(TEXT_ONLY),
    ]
    runtime.models = PlaceModels(
        lambda: list(offered), lambda _task, settings: recorder.client(settings.model)
    )
    return runtime


def run_next(store, runtime):
    task = store.claim(OWNER)
    runtime.run(task, EventCancellationToken(threading.Event()))
    finished = store.task(OWNER, task["id"])
    finished["fallbacks"] = [
        event["data"] for event in finished["events"] if event["kind"] == "model_fallback"
    ]
    return finished


def owl(store, model=None, name="Ειδήσεις 毎朝"):
    bot = store.save_bot(OWNER, BotCreateInput(name=name, model=model))
    return bot, store.create_chat(OWNER, bot["id"], "Συνομιλία")


def routine(store, bot, model=None, name="Πρωινά νέα"):
    fields = {"name": name, "prompt": "Σύνοψη ειδήσεων · ニュースの要約", "timing": EVERY_HOUR}
    if model is not None:
        fields["model"] = model
    return store.save_schedule(OWNER, bot["id"], ScheduleInput.model_validate(fields), now=1000)


def schedule(store, bot, schedule_id):
    return next(s for s in store.list_schedules(OWNER, bot["id"]) if s["id"] == schedule_id)


def ask(store, chat, message="Τι νέα;"):
    return store.enqueue(OWNER, chat["id"], TaskInput(message=message, request_id=uuid4().hex))


# A routine's run thinks with its own model


@pytest.mark.parametrize(("owl_model", "routine_model"), [(SEEING, TEXT_ONLY), (TEXT_ONLY, SEEING)])
def test_a_routines_run_thinks_with_its_own_model_and_that_models_allowances(
    tmp_path, owl_model, routine_model
):
    store = Store(tmp_path / "bots.sqlite3")
    bot, chat = owl(store, owl_model)
    recorder = Recorder()
    runtime = place(store, recorder)
    # What each model can do comes from its declared capacities, and they differ.
    listed = {row["id"]: row for row in runtime.models.list()}
    assert listed[owl_model]["image_input"] is not listed[routine_model]["image_input"]
    item = routine(store, bot, routine_model)
    assert item["model"] == routine_model

    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=4600) == 1
    run = run_next(store, runtime)
    assert (run["status"], run["fallbacks"]) == ("completed", [])
    fitted = RuntimeLimits.for_model(model=routine_model, base_url=ENDPOINT)
    assert recorder.calls == [(routine_model, fitted.output_tokens)]
    # Its run history says which model the run was queued with.
    [record] = store.schedule_runs(OWNER, bot["id"], item["id"])["runs"]
    assert record["config"]["model"] == routine_model

    # The owl's own turns still think with the owl's model.
    ask(store, chat)
    assert run_next(store, runtime)["status"] == "completed"
    own = RuntimeLimits.for_model(model=owl_model, base_url=ENDPOINT)
    assert recorder.calls[-1] == (owl_model, own.output_tokens)


def test_a_routine_without_a_model_thinks_with_its_owls(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot, _ = owl(store, SEEING)
    item = routine(store, bot)
    assert item["model"] is None
    recorder = Recorder()
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=4600) == 1
    assert run_next(store, place(store, recorder))["status"] == "completed"
    assert recorder.models() == [SEEING]
    assert "model" not in store.schedule_runs(OWNER, bot["id"], item["id"])["runs"][0]["config"]


def test_a_run_already_queued_keeps_the_model_it_was_queued_with(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot, _ = owl(store)
    item = routine(store, bot, TEXT_ONLY)
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=4600) == 1
    store.set_schedule_model(
        OWNER, bot["id"], item["id"], ScheduleModelChoice(model=SEEING, expected_revision=1)
    )
    recorder = Recorder()
    assert run_next(store, place(store, recorder))["status"] == "completed"
    assert recorder.models() == [TEXT_ONLY]


def test_only_the_routines_own_owl_thinks_with_the_routines_model(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot, _ = owl(store)
    helper, _ = owl(store, SEEING, name="Βοηθός")
    routine(store, bot, TEXT_ONLY)
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=4600) == 1
    run = store.claim(OWNER)
    runtime = place(store, Recorder())
    # Another owl's turn tied to this run (as a peer's answer is tied to the turn that asked)
    # thinks with that owl's own model.
    assert runtime.turn_model(run["id"], bot["id"])[0].model == TEXT_ONLY
    assert runtime.turn_model(run["id"], helper["id"])[0].model == SEEING


# By the owl's rules: a closed catalog model gives way, a model gone fails the run


def catalog_place(store, recorder, unavailable=PENDING):
    """The place's own connection lists a catalog: one model open now, one closed."""
    listed = ModelCatalog(
        (
            CatalogModel(PLACE, "Place model"),
            CatalogModel(SEEING, "GLM 5.3 Flash"),
            CatalogModel(TEXT_ONLY, "GLM 5.3", unavailable),
        )
    )
    return place(
        store,
        recorder,
        [connection(PLACE, default=True, connection_id="account", catalog=lambda: listed)],
    )


def test_a_routines_catalog_model_no_longer_open_gives_way_with_the_same_notice(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot, _ = owl(store, "account/" + SEEING)
    routine(store, bot, "account/" + TEXT_ONLY)
    recorder = Recorder()
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=4600) == 1
    run = run_next(store, catalog_place(store, recorder))
    assert run["status"] == "completed"
    # It thought with its connection's own model, never the owl's.
    assert recorder.models() == [PLACE]
    [notice] = run["fallbacks"]
    assert (notice["picked"], notice["used"], notice["reason"]) == (
        "GLM 5.3",
        "Place model",
        PENDING,
    )
    assert notice["routine"] is True
    assert "routine" in notice["message"] and "owl's settings" not in notice["message"]


def test_a_routines_open_catalog_model_is_used_as_picked(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot, _ = owl(store)
    routine(store, bot, "account/" + SEEING)
    recorder = Recorder()
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=4600) == 1
    run = run_next(store, catalog_place(store, recorder))
    assert (run["status"], run["fallbacks"]) == ("completed", [])
    assert recorder.models() == [SEEING]


def test_a_routine_whose_model_went_away_fails_and_waits_for_the_person_to_pick_another(
    tmp_path,
):
    store = Store(tmp_path / "bots.sqlite3")
    bot, _ = owl(store, SEEING)
    item = routine(store, bot, "removed-connection")
    recorder = Recorder()
    runtime = place(store, recorder)
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=4600) == 1
    failed = run_next(store, runtime)
    assert (failed["status"], failed["failure"]) == ("failed", {"kind": "model"})
    assert "removed-connection" in failed["error"] and "routine" in failed["error"]
    # Nothing thought with another model instead, not even the owl's.
    assert recorder.calls == []
    paused = schedule(store, bot, item["id"])
    assert (paused["state"], paused["pause_code"]) == ("paused", "setting")

    # The person picks another model: only the model changes, and the pause stays until they
    # turn the routine back on.
    picked = store.set_schedule_model(
        OWNER,
        bot["id"],
        item["id"],
        ScheduleModelChoice(model=TEXT_ONLY, expected_revision=paused["revision"]),
    )
    assert (picked["model"], picked["revision"]) == (TEXT_ONLY, paused["revision"] + 1)
    shown = schedule(store, bot, item["id"])
    assert (shown["state"], shown["pause_code"]) == ("paused", "setting")
    with pytest.raises(Conflict):
        store.set_schedule_model(
            OWNER,
            bot["id"],
            item["id"],
            ScheduleModelChoice(model=SEEING, expected_revision=paused["revision"]),
        )
    store.decide_schedule(OWNER, bot["id"], item["id"], "active", picked["revision"], now=5000)
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=9000) == 1
    assert run_next(store, runtime)["status"] == "completed"
    assert recorder.models() == [TEXT_ONLY]


# The owl may give a routine a model, only one the place offers; the person still turns it on


def test_the_owl_gives_a_routine_only_a_model_this_place_offers(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot, chat = owl(store, SEEING)
    runtime = place(store, Recorder())
    ask(store, chat, "Κάθε ώρα, με το απλό μοντέλο, φτιάξε σύνοψη.")
    task = store.claim(OWNER)
    tools = ["create_schedule", "edit_schedule", "list_schedules", "propose_schedule"]
    fields = {"name": "Σύνοψη", "prompt": "Σύνοψη · 要約", "timing": EVERY_HOUR}

    # The owl reads what a routine can think with here, by id, with what each can do.
    listing = runtime._tool(task, "list_schedules", {}, tools)
    assert listing["schedules"] == []
    models = listing["models"]
    assert models["yours"] == SEEING
    assert [(m["id"], m["sees_images"]) for m in models["offered"]] == [
        (PLACE, False),
        (SEEING, True),
        (TEXT_ONLY, False),
    ]
    assert all(m["context_tokens"] > 0 for m in models["offered"])

    with pytest.raises(Conflict, match="list_schedules"):
        runtime._tool(task, "create_schedule", {**fields, "model": "not-offered"}, tools)
    created = runtime._tool(task, "create_schedule", {**fields, "model": TEXT_ONLY}, tools)
    # Saved off: only the person's own click turns it on.
    assert (created["model"], created["state"]) == (TEXT_ONLY, "draft")
    assert "not on yet" in created["message"]

    edit = {**fields, "schedule_id": created["id"], "expected_revision": 1, "enabled": False}
    # An edit that leaves the model out keeps it; null sets it back to the owl's.
    kept = runtime._tool(task, "edit_schedule", {**edit, "prompt": "Νέα σύνοψη"}, tools)
    assert kept["model"] == TEXT_ONLY
    with pytest.raises(Conflict, match="list_schedules"):
        runtime._tool(
            task, "edit_schedule", {**edit, "expected_revision": 2, "model": "gone"}, tools
        )
    reset = runtime._tool(
        task, "edit_schedule", {**edit, "expected_revision": 2, "model": None}, tools
    )
    assert reset["model"] is None
    assert runtime._tool(task, "list_schedules", {}, tools)["schedules"][0]["model"] is None
    store.finish(OWNER, task["id"], "completed", content="Έτοιμο.")

    # A suggestion carries the model to its draft; the person's card turns it on as before.
    ask(store, chat, "Κάθε πρωί ελέγχω τις τιμές.")
    asked = store.claim(OWNER)
    suggestion = {**fields, "name": "Τιμές", "prompt": "Έλεγξε τις τιμές · 価格"}
    with pytest.raises(Conflict, match="list_schedules"):
        runtime._tool(asked, "propose_schedule", {**suggestion, "model": "nope"}, tools)
    offered = runtime._tool(asked, "propose_schedule", {**suggestion, "model": PLACE}, tools)
    store.finish(OWNER, asked["id"], "completed", content="Να το κάνω ρουτίνα;")
    draft = schedule(store, bot, offered["id"])
    assert (draft["state"], draft["model"]) == ("draft", PLACE)
    # Picking another model in Routines keeps it a draft, and its card open.
    store.set_schedule_model(
        OWNER, bot["id"], draft["id"], ScheduleModelChoice(model=TEXT_ONLY, expected_revision=1)
    )
    assert schedule(store, bot, draft["id"])["state"] == "draft"
    turned_on = runtime.routine_suggestions.decide(
        bot["id"],
        offered["suggestion_id"],
        RoutineSuggestionDecision(action="turn_on", expected_revision=1, request_id="turn-on-01"),
    )
    assert turned_on["status"] == "on"
    on = schedule(store, bot, draft["id"])
    assert (on["state"], on["model"]) == ("active", TEXT_ONLY)


# The person picks it over HTTP


def test_the_person_picks_a_routines_model_over_http(tmp_path):
    settings = Settings(
        tmp_path / "bots.db",
        "t" * 40,
        "fixture-key",
        ENDPOINT,
        TEXT_ONLY,
        models=[SEEING, TEXT_ONLY],
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "routine_models" in client.get("/v1/status").json()["capabilities"]
        bot = client.post("/v1/bots", json={"name": "Αθηνά"}).json()
        base = f"/v1/bots/{bot['id']}/schedules"
        body = {"name": "Νέα", "prompt": "Σύνοψη", "timing": EVERY_HOUR, "enabled": False}

        made = client.post(base, json={**body, "model": SEEING})
        assert made.status_code == 201 and made.json()["model"] == SEEING
        refused = client.post(base, json={**body, "name": "Άλλη", "model": "elsewhere"})
        assert refused.status_code == 409 and "elsewhere" in refused.json()["detail"]
        assert client.post(base, json={**body, "name": "Τρίτη", "model": ""}).status_code == 422
        # An app from before routine models: its routine thinks with the owl's model, and its
        # edits keep a model picked elsewhere.
        plain = client.post(base, json={**body, "name": "Παλιά"}).json()
        assert plain["model"] is None
        item = made.json()
        edited = client.put(
            f"{base}/{item['id']}", json={**body, "prompt": "Νέα σύνοψη", "expected_revision": 1}
        ).json()
        assert (edited["model"], edited["revision"]) == (SEEING, 2)

        path = f"{base}/{item['id']}/model"
        assert client.put(path, json={"model": "gone", "expected_revision": 2}).status_code == 409
        assert (
            client.put(path, json={"model": TEXT_ONLY, "expected_revision": 1}).status_code == 409
        )
        picked = client.put(path, json={"model": TEXT_ONLY, "expected_revision": 2}).json()
        assert (picked["model"], picked["revision"], picked["state"]) == (TEXT_ONLY, 3, "paused")
        # The same choice again changes nothing.
        assert client.put(path, json={"model": TEXT_ONLY, "expected_revision": 2}).json() == picked
        back = client.put(path, json={"model": None, "expected_revision": 3}).json()
        assert back["model"] is None
        assert {s["name"]: s["model"] for s in client.get(base).json()} == {
            "Νέα": None,
            "Παλιά": None,
        }


# It moves with the owl only where the other place offers it


def move(source, source_models, target, target_models, bot_id, *, carry=True):
    pages, cursor = [], None
    while True:
        page = export_page(
            source,
            OWNER,
            bot_id,
            MoveExport(cursor=cursor, model=True, routine_models=carry),
            models=source_models,
        )
        pages.append(page)
        cursor = page["cursor"]
        if cursor is None:
            break
    moves = OwlMoves(target, OWNER, models=target_models)
    started = moves.begin(MoveBegin(request_id="move-" + uuid4().hex, source_bot_id=bot_id))
    for number, page in enumerate(pages):
        moves.receive(
            started["id"],
            MovePage(page=number, last=page["cursor"] is None, records=page["records"]),
        )
    done = moves.complete(started["id"])
    records = [r for page in pages for r in page["records"] if r["kind"] == "routine"]
    moved = {s["name"]: s["model"] for s in target.list_schedules(OWNER, done["owl"]["id"])}
    return target.bot(OWNER, done["owl"]["id"]), moved, records


def test_a_moved_routine_keeps_its_model_only_where_the_other_place_offers_it(tmp_path):
    here = PlaceModels(
        lambda: [connection(PLACE, default=True), connection(SEEING), connection(TEXT_ONLY)],
        None,
    )
    # The other place offers SEEING by the same id, and TEXT_ONLY as its own model, under
    # another id; PLACE not at all.
    there = PlaceModels(
        lambda: [connection(TEXT_ONLY, default=True, connection_id="cloud"), connection(SEEING)],
        None,
    )
    source = Store(tmp_path / "here.sqlite3")
    bot, _ = owl(source, SEEING)
    for name, model in (
        ("same-id", SEEING),
        ("same-model", TEXT_ONLY),
        ("not-there", PLACE),
        ("owls", None),
    ):
        routine(source, bot, model, name=name)

    moved_owl, moved, records = move(
        source, here, Store(tmp_path / "there.sqlite3"), there, bot["id"]
    )
    assert moved == {
        "same-id": SEEING,
        # Where it is the other place's own model, the routine keeps it by that id there:
        # none would mean its owl's.
        "same-model": "cloud",
        "not-there": None,
        "owls": None,
    }
    assert moved_owl["model"] == SEEING
    by_name = {record["name"]: record for record in records}
    assert (by_name["same-model"]["model"], by_name["same-model"]["model_name"]) == (
        TEXT_ONLY,
        TEXT_ONLY,
    )
    assert "model" not in by_name["owls"]

    # An app that doesn't ask (the other place can't take it) carries no routine's model.
    _, moved, records = move(
        source, here, Store(tmp_path / "older.sqlite3"), there, bot["id"], carry=False
    )
    assert set(moved.values()) == {None}
    assert all("model" not in record for record in records)


# The database


def test_an_older_database_gets_routine_models_and_its_routines_keep_their_owls(tmp_path):
    path = tmp_path / "bots.sqlite3"
    store = Store(path)
    bot, _ = owl(store, SEEING)
    item = routine(store, bot)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE schedules DROP COLUMN model")
        db.execute("PRAGMA user_version=50")
    reopened = Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 51
    assert schedule(reopened, bot, item["id"])["model"] is None
    picked = reopened.set_schedule_model(
        OWNER, bot["id"], item["id"], ScheduleModelChoice(model=TEXT_ONLY, expected_revision=1)
    )
    assert picked["model"] == TEXT_ONLY
    # A routine saved by an app that never sends a model.
    other = reopened.save_schedule(
        OWNER,
        bot["id"],
        ScheduleInput.model_validate({"name": "Β", "prompt": "Σύνοψη", "timing": EVERY_HOUR}),
        now=1000,
    )
    assert other["model"] is None


@pytest.mark.parametrize("value", ["", "x" * 201, "two\nlines", "tab\tinside"])
def test_a_routine_model_id_is_one_bounded_line(value):
    with pytest.raises(ValueError):
        ScheduleModelChoice(model=value, expected_revision=1)
    with pytest.raises(ValueError):
        ScheduleInput.model_validate(
            {"name": "Νέα", "prompt": "Σύνοψη", "timing": EVERY_HOUR, "model": value}
        )
