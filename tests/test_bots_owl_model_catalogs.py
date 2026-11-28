"""An owl may think with any model its connections' catalogs offer the person: the account's
models through the account's own connection, each with what its declared capacities say it can
do, and with a plain notice when the catalog no longer opens the one it picked.

Real store, runtime, service and moves; a fake account catalog; recording model clients.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from alysis_code import alysis_cloud
from alysis_code.bots.app import Settings, create_app, offered_models
from alysis_code.bots.browser_images import declared_image_support
from alysis_code.bots.local_service import saved_connections, settings_from_bootstrap
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_models import (
    PENDING,
    REMOVED,
    UNCHECKED,
    CachedCatalog,
    CatalogModel,
    ModelConnection,
    PlaceModels,
    image_input,
    model_protocol,
)
from alysis_code.bots.owl_move import (
    MoveBegin,
    MoveExport,
    MovePage,
    OwlMoves,
    export_page,
)
from alysis_code.bots.owl_move import fingerprint as owl_fingerprint
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.config import AppConfig, save_config
from alysis_code.llm.types import LLMResponse, LLMUsage
from alysis_code.model_registry import ModelRegistry

GATEWAY = alysis_cloud.gateway_base_url()
# Another provider's endpoint, made from a provider preset with reviewed models.
ENDPOINT = "https://api.z.ai/api/coding/paas/v4"
OWN = "glm-5.3-flash"
STRONG = "gpt-6.1-sol"
LATER = "claude-sonnet-5-5"
KEY = "slk_catalog_fixture"
OWNER = "owner"


def catalog_rows(*, strong=None, later=PENDING, listed=(OWN, STRONG, LATER)):
    """The account's catalog: its own model, one open to it and one its plan lists for later."""
    rows = {
        OWN: {"model": OWN, "label": "GLM 5.3 Flash", "unavailable": None},
        STRONG: {"model": STRONG, "label": "GPT-6.1 Sol", "unavailable": strong},
        LATER: {"model": LATER, "label": "Claude Sonnet 5.5", "unavailable": later},
    }
    return [rows[model] for model in listed]


class Account:
    """A fake account catalog: what the gateway and the plan say, and each read of it."""

    def __init__(self, rows):
        self.rows, self.reads = rows, []

    def __call__(self, key):
        self.reads.append(key)
        return self.rows


def gateway_settings(tmp_path):
    return Settings(
        tmp_path / "bots.sqlite3",
        "t" * 40,
        KEY,
        GATEWAY,
        OWN,
        provider_key="alysis",
        owner=OWNER,
    )


def account_connection(settings, account, *, default=True):
    def read():
        rows = account(settings.api_key)
        if rows is None:
            return None
        return [CatalogModel(row["model"], row["label"], row["unavailable"]) for row in rows]

    return ModelConnection(
        "alysis", "alysis", settings, default=default, catalog=CachedCatalog(read)
    )


class Recorder:
    """Every client made: the model, endpoint and key it was made for; each answers plainly."""

    def __init__(self):
        self.made, self.calls = [], []

    def client(self, settings):
        recorder = self
        self.made.append((settings.model, settings.base_url, settings.api_key))

        class Client:
            supports_tool_calling = True
            model = settings.model
            base_url = settings.base_url

            def chat(self, **kwargs):
                recorder.calls.append((settings.model, kwargs.get("max_tokens")))
                return LLMResponse("Έτοιμο · 完了", [], {}, usage=LLMUsage(10, 5, 15))

        return Client()

    def models(self):
        return [model for model, _ in self.calls]


def account_place(store, settings, account, recorder):
    """A runtime on the account's connection (its default), whose catalog the account gives."""
    runtime = BotRuntime(store, OWNER, lambda _task: recorder.client(settings))
    connection = account_connection(settings, account)
    runtime.models = PlaceModels(
        lambda: [connection], lambda _task, chosen: recorder.client(chosen)
    )
    return runtime


def run_with(store, runtime, model):
    bot = store.save_bot(OWNER, BotInput(name="Ειδήσεις 毎朝", model=model))
    chat = store.create_chat(OWNER, bot["id"], "Chat")
    store.enqueue(OWNER, chat["id"], TaskInput(message="Τι νέα;", request_id=uuid4().hex))
    task = store.claim(OWNER)
    runtime.run(task, EventCancellationToken(threading.Event()))
    finished = store.task(OWNER, task["id"])
    finished["fallbacks"] = [
        event["data"] for event in finished["events"] if event["kind"] == "model_fallback"
    ]
    return finished


def registry(model, base_url=GATEWAY):
    return ModelRegistry(cfg=AppConfig(model=model, base_url=base_url)).get(
        model, include_provider_auth=False
    )


# What a connection's catalog offers


def test_the_accounts_catalog_is_offered_under_its_connection_with_each_models_capacities(
    tmp_path,
):
    account = Account(catalog_rows())
    connection = account_connection(gateway_settings(tmp_path), account)
    offered = PlaceModels(lambda: [connection], lambda *_: None)
    listed = offered.list()
    # The connection's own model first, as the place's default; then its catalog's others.
    assert [(item["id"], item["model"], item["default"]) for item in listed] == [
        ("alysis", OWN, True),
        ("alysis/" + STRONG, STRONG, False),
        ("alysis/" + LATER, LATER, False),
    ]
    assert {(item["connection"], item["connection_label"]) for item in listed} == {
        ("alysis", "alysis")
    }
    assert [item["model_label"] for item in listed] == [
        "GLM 5.3 Flash",
        "GPT-6.1 Sol",
        "Claude Sonnet 5.5",
    ]
    # The one the plan lists for later is listed, closed, with why.
    assert [(item["available"], item["unavailable"]) for item in listed] == [
        (True, None),
        (True, None),
        (False, PENDING),
    ]
    # Each model's own declared capacities (the registry's, for that model on that endpoint),
    # never its connection's default model's.
    for item in listed:
        declared = registry(item["model"])
        assert item["context_window"] == declared.context_window_tokens
        assert item["reasoning"] is (declared.supports_reasoning is True)
    assert listed[0]["context_window"] != listed[1]["context_window"]
    assert listed[0]["image_input"] is declared_image_support(OWN, GATEWAY) is True
    assert listed[1]["image_input"] is declared_image_support(STRONG, GATEWAY) is True
    # Credentials and endpoints are never listed; the catalog was read once.
    assert KEY not in str(listed) and GATEWAY not in str(listed)
    offered.list()
    assert account.reads == [KEY]


def test_a_new_pick_must_be_open_to_the_person_and_todays_ids_stay(tmp_path):
    offered = PlaceModels(
        lambda: [account_connection(gateway_settings(tmp_path), Account(catalog_rows()))],
        lambda *_: None,
    )
    assert offered.offers("alysis")
    assert offered.offers("alysis/" + STRONG)
    assert offered.offers("alysis/" + OWN)
    assert not offered.offers("alysis/" + LATER)
    assert not offered.offers("alysis/not-in-the-catalog")
    assert not offered.offers("elsewhere/" + STRONG)
    assert not offered.offers(STRONG)


@pytest.mark.parametrize(
    ("model", "protocol"),
    [("claude-haiku-5-5", "anthropic_messages"), ("gpt-6-luna", "openai_responses")],
)
def test_native_image_adapters_require_declared_vision(tmp_path, monkeypatch, model, protocol):
    settings = replace(gateway_settings(tmp_path), model=model)
    assert model_protocol(settings) == protocol
    assert image_input(settings)
    monkeypatch.setattr("alysis_code.bots.owl_models.declared_image_support", lambda *_: False)
    assert not image_input(settings)


def test_a_catalog_that_cant_be_read_offers_nothing_new_and_reads_again_later(tmp_path):
    now = [0.0]
    answers = [None, catalog_rows(), None]
    reads = []

    def read():
        reads.append(now[0])
        rows = answers.pop(0) if answers else catalog_rows(listed=(OWN,))
        return None if rows is None else [CatalogModel(**row) for row in rows]

    catalog = CachedCatalog(read, fresh_s=60, retry_s=15, clock=lambda: now[0])
    assert catalog().read is False
    now[0] = 5
    assert catalog().read is False and reads == [0.0]
    now[0] = 16
    assert [model.model for model in catalog().models] == [OWN, STRONG, LATER]
    # A failed read keeps the last good one, and is tried again after a short wait.
    now[0] = 80
    assert [model.model for model in catalog().models] == [OWN, STRONG, LATER]
    now[0] = 96
    assert [model.model for model in catalog().models] == [OWN]
    assert reads == [0.0, 16, 80, 96]


def test_owls_on_this_computer_offer_each_saved_connections_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_CATALOG_TEST_KEY", KEY)
    monkeypatch.setenv("ALYSIS_OTHER_TEST_KEY", "placeholder")
    cfg = AppConfig(model=OWN, base_url=GATEWAY)
    cfg.extra_fields["profiles"] = {
        "alysis": {
            "protocol": "openai_compat",
            "base_url": GATEWAY,
            "api_key_env": "ALYSIS_CATALOG_TEST_KEY",
            "default_model": OWN,
        },
        "research": {
            "protocol": "openai_compat",
            "base_url": ENDPOINT,
            "api_key_env": "ALYSIS_OTHER_TEST_KEY",
            "default_model": "glm-5.3",
        },
    }
    cfg.extra_fields["active_profile"] = "alysis"
    save_config(cfg)
    settings = settings_from_bootstrap(
        {"directory": str(tmp_path / "data"), "token": "t" * 40, "profile": "alysis"}
    )
    account = Account(catalog_rows())
    offered = PlaceModels(saved_connections(settings, account_catalog=account), lambda *_: None)
    listed = offered.list()
    # The account's connection offers its catalog, read with its own key; the other connection
    # the reviewed models of the provider it was made from. Each under its own connection.
    assert [(item["connection"], item["id"]) for item in listed] == [
        ("alysis", "alysis"),
        ("alysis", "alysis/" + STRONG),
        ("alysis", "alysis/" + LATER),
        ("research", "research"),
        ("research", "research/glm-5.4"),
        ("research", "research/glm-5.3-flash"),
    ]
    assert all(item["available"] for item in listed if item["connection"] == "research")
    # Connections are read again each time; the account's catalog at most once a minute.
    assert offered.list() == listed
    assert account.reads == [KEY]


def test_a_new_default_model_for_the_services_connection_counts_from_the_next_turn(
    tmp_path, monkeypatch
):
    """The person chose another default model in the app while owls run: owls without a model
    of their own think with it from their next turn, under the same connection id."""
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_OTHER_TEST_KEY", "placeholder")

    def save(default_model):
        cfg = AppConfig(model=OWN, base_url=ENDPOINT)
        cfg.extra_fields["profiles"] = {
            "zai": {
                "protocol": "openai_compat",
                "base_url": ENDPOINT,
                "api_key_env": "ALYSIS_OTHER_TEST_KEY",
                "default_model": default_model,
            },
        }
        cfg.extra_fields["active_profile"] = "zai"
        save_config(cfg)

    save(OWN)
    settings = settings_from_bootstrap(
        {"directory": str(tmp_path / "data"), "token": "t" * 40, "profile": "zai"}
    )
    offered = PlaceModels(saved_connections(settings), lambda *_: None)
    assert (offered.default().id, offered.default().settings.model) == ("zai", OWN)
    save("glm-5.4")
    assert (offered.default().id, offered.default().settings.model) == ("zai", "glm-5.4")
    assert [row["model"] for row in offered.list() if row["default"]] == ["glm-5.4"]
    # The service's own settings stay as it started.
    assert settings.model == OWN


# Every turn thinks with the picked model, through its connection


def test_an_owl_set_to_a_catalog_model_thinks_with_it_through_the_accounts_connection(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    settings = gateway_settings(tmp_path)
    recorder = Recorder()
    runtime = account_place(store, settings, Account(catalog_rows()), recorder)
    finished = run_with(store, runtime, "alysis/" + STRONG)
    assert finished["status"] == "completed" and finished["fallbacks"] == []
    # Same endpoint and key as the connection: only the model differs. Its own allowances.
    assert (STRONG, GATEWAY, KEY) in recorder.made
    fitted = RuntimeLimits.for_model(model=STRONG, base_url=GATEWAY)
    assert recorder.calls == [(STRONG, fitted.output_tokens)]


@pytest.mark.parametrize("model", ["alysis", None])
def test_todays_ids_still_mean_the_connections_own_model(tmp_path, model):
    store = Store(tmp_path / "bots.sqlite3")
    recorder = Recorder()
    runtime = account_place(store, gateway_settings(tmp_path), Account(catalog_rows()), recorder)
    finished = run_with(store, runtime, model)
    assert finished["status"] == "completed" and finished["fallbacks"] == []
    assert recorder.calls == [(OWN, RuntimeLimits().output_tokens)]


@pytest.mark.parametrize(
    ("rows", "reason", "picked"),
    [
        # The plan lists it, but it isn't open to this account now.
        (catalog_rows(), PENDING, "Claude Sonnet 5.5"),
        # The catalog no longer lists it at all: it is named by its id.
        (catalog_rows(listed=(OWN, STRONG)), REMOVED, LATER),
    ],
)
def test_a_model_the_catalog_no_longer_opens_gives_way_to_its_connections_with_a_notice(
    tmp_path, rows, reason, picked
):
    store = Store(tmp_path / "bots.sqlite3")
    recorder = Recorder()
    runtime = account_place(store, gateway_settings(tmp_path), Account(rows), recorder)
    finished = run_with(store, runtime, "alysis/" + LATER)
    assert finished["status"] == "completed"
    assert recorder.models() == [OWN]
    [notice] = finished["fallbacks"]
    assert notice["reason"] == reason
    assert (notice["picked"], notice["used"]) == (picked, "GLM 5.3 Flash")
    # Plain words for an app that doesn't word it itself.
    assert notice["message"].startswith(picked) and "GLM 5.3 Flash" in notice["message"]
    assert "settings" in notice["message"]


@pytest.mark.parametrize("unread", [True, False])
def test_a_pick_whose_catalog_cant_say_now_is_used_as_picked(tmp_path, unread):
    store = Store(tmp_path / "bots.sqlite3")
    recorder = Recorder()
    account = Account(None if unread else catalog_rows(strong=UNCHECKED))
    runtime = account_place(store, gateway_settings(tmp_path), account, recorder)
    finished = run_with(store, runtime, "alysis/" + STRONG)
    assert finished["status"] == "completed" and finished["fallbacks"] == []
    assert recorder.models() == [STRONG]
    # ...but nobody can newly pick it until it can be checked.
    assert not runtime.models.offers("alysis/" + STRONG)


def test_an_owl_whose_connection_is_gone_still_fails_without_another_model(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    recorder = Recorder()
    runtime = account_place(store, gateway_settings(tmp_path), Account(catalog_rows()), recorder)
    finished = run_with(store, runtime, "removed-key/" + STRONG)
    assert (finished["status"], finished["failure"]) == ("failed", {"kind": "model"})
    assert recorder.calls == []


# Over HTTP


def test_the_service_lists_and_validates_catalog_models(tmp_path):
    settings = gateway_settings(tmp_path)
    account = Account(catalog_rows())
    app = create_app(settings, model_connections=lambda: [account_connection(settings, account)])
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        listed = client.get("/v1/models").json()["models"]
        assert [item["id"] for item in listed] == ["alysis", "alysis/" + STRONG, "alysis/" + LATER]
        assert KEY not in str(listed)
        made = client.post("/v1/bots", json={"name": "花子", "model": "alysis/" + STRONG})
        assert made.status_code == 201 and made.json()["model"] == "alysis/" + STRONG
        closed = client.post("/v1/bots", json={"name": "花子", "model": "alysis/" + LATER})
        assert closed.status_code == 409 and LATER in closed.json()["detail"]
        assert client.post("/v1/bots", json={"name": "Νέα", "model": "alysis"}).status_code == 201
        capabilities = client.get("/v1/status").json()["capabilities"]
        assert {"owl_models", "owl_move_model"} <= set(capabilities)


# Moving: the copy keeps the model where the other place offers it


def owl_with_model(store, model):
    bot = store.save_bot(OWNER, BotInput(name="Αθηνά 毎朝", model=model))
    store.create_chat(OWNER, bot["id"], "Συνομιλία")
    return bot["id"]


def move_to(source, source_models, target, target_models, owl, *, carry=True):
    pages, cursor = [], None
    while True:
        page = export_page(
            source,
            OWNER,
            owl,
            MoveExport(cursor=cursor, model=carry, reasoning_effort=carry),
            models=source_models,
        )
        pages.append(page)
        cursor = page["cursor"]
        if cursor is None:
            break
    moves = OwlMoves(target, OWNER, models=target_models)
    started = moves.begin(MoveBegin(request_id="move-" + uuid4().hex, source_bot_id=owl))
    for number, page in enumerate(pages):
        moves.receive(
            started["id"],
            MovePage(page=number, last=page["cursor"] is None, records=page["records"]),
        )
    done = moves.complete(started["id"])
    owl_record = next(r for page in pages for r in page["records"] if r["kind"] == "owl")
    return target.bot(OWNER, done["owl"]["id"]), owl_record


def test_a_moved_owl_keeps_its_model_where_offered_and_otherwise_the_places_own(tmp_path):
    settings = gateway_settings(tmp_path)
    here = PlaceModels(lambda: [account_connection(settings, Account(catalog_rows()))], None)
    source = Store(tmp_path / "here.sqlite3")
    owl = owl_with_model(source, "alysis/" + STRONG)
    source.save_bot(OWNER, BotInput(name="Apollo", reasoning_effort="high"), owl)

    # A place that offers the same connection and catalog keeps the same id.
    same = Store(tmp_path / "same.sqlite3")
    there = PlaceModels(lambda: [account_connection(settings, Account(catalog_rows()))], None)
    moved, record = move_to(source, here, same, there, owl)
    assert (record["model"], record["model_name"]) == ("alysis/" + STRONG, STRONG)
    assert moved["model"] == "alysis/" + STRONG
    assert record["reasoning_effort"] == moved["reasoning_effort"] == "high"

    # A place whose ids are its models' own names keeps the same model by its id there.
    cloud = replace(settings, models=(STRONG,))
    named = Store(tmp_path / "named.sqlite3")
    moved, _ = move_to(source, here, named, PlaceModels(offered_models(cloud), None), owl)
    assert moved["model"] == STRONG

    # A place that doesn't offer it, or doesn't open it: the copy thinks with its own model.
    for rows in (catalog_rows(listed=(OWN,)), catalog_rows(strong=PENDING)):
        closed = Store(tmp_path / f"closed-{uuid4().hex}.sqlite3")
        elsewhere = PlaceModels(
            lambda rows=rows: [account_connection(settings, Account(rows))], None
        )
        moved, _ = move_to(source, here, closed, elsewhere, owl)
        assert moved["model"] is None
        assert moved["reasoning_effort"] is None


def test_an_app_that_doesnt_ask_carries_no_model(tmp_path):
    settings = gateway_settings(tmp_path)
    models = PlaceModels(lambda: [account_connection(settings, Account(catalog_rows()))], None)
    source = Store(tmp_path / "here.sqlite3")
    owl = owl_with_model(source, "alysis/" + STRONG)
    moved, record = move_to(
        source, models, Store(tmp_path / "there.sqlite3"), models, owl, carry=False
    )
    assert "model" not in record and moved["model"] is None


def test_changing_the_model_while_it_moves_is_a_change(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    owl = owl_with_model(store, "alysis/" + STRONG)
    with store.connect() as db:
        before = owl_fingerprint(db, owl)
    store.save_bot(OWNER, BotInput(name="Αθηνά 毎朝", model="alysis"), owl)
    with store.connect() as db:
        assert owl_fingerprint(db, owl) != before


# The account's catalog, as the account gateway and the plan give it


def test_the_account_catalog_is_the_plans_models_open_where_the_gateway_serves_them(monkeypatch):
    from alysis_code import account_images, account_login
    from alysis_code.account_login import TrialStatus

    paid = {
        "credit_unit_version": "paid-base-display-v1",
        "models": [
            {"model": OWN, "label": "GLM 5.3 Flash"},
            {"model": STRONG, "label": "GPT-6.1 Sol"},
            {"model": LATER, "label": "Claude Sonnet 5.5", "available": False},
            # The plan also lists the model its pictures are made with: nobody thinks with it.
            {"model": "picture-model", "label": "Pictures"},
        ],
    }
    monkeypatch.setattr(account_images, "picture_models", lambda key: frozenset({"picture-model"}))
    status = TrialStatus(None, None, None, None, None, None, billing={"paid": paid})
    served = [OWN]
    keys = []
    monkeypatch.setattr(account_login, "gateway_models", lambda key: keys.append(key) or served)
    monkeypatch.setattr(
        account_login, "fetch_trial_status", lambda _cfg, key: keys.append(key) or status
    )
    rows = account_login.account_model_catalog(KEY)
    assert [(row["model"], row["unavailable"]) for row in rows] == [
        (OWN, None),
        (STRONG, PENDING),
        (LATER, PENDING),
    ]
    assert rows[1]["label"] == "GPT-6.1 Sol" and set(keys) == {KEY}
    # The gateway couldn't be asked: whether they are open is unknown, not refused.
    served = None
    assert [row["unavailable"] for row in account_login.account_model_catalog(KEY)] == [
        UNCHECKED,
        UNCHECKED,
        PENDING,
    ]
    # Without a plan's catalog, what the gateway serves this key is the catalog.
    served, status = [OWN, STRONG], None
    assert account_login.account_model_catalog(KEY) == [
        {"model": OWN, "label": "GLM 5.3 Flash", "unavailable": None},
        {"model": STRONG, "label": "GPT-6.1 Sol", "unavailable": None},
    ]
    served = None
    assert account_login.account_model_catalog(KEY) is None
