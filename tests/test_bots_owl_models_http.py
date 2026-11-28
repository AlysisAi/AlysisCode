"""An owl's model over HTTP: advertised, offered by the place, validated and kept. Needs FastAPI."""

from __future__ import annotations

import json
import sqlite3

import httpx
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.config import AppConfig, save_config

ENDPOINT = "https://api.z.ai/api/coding/paas/v4"


def test_gateway_efforts_come_from_its_model_catalog_once_for_the_picker(tmp_path, monkeypatch):
    from alysis_code import provider_model_catalog as catalog
    from alysis_code.bots.owl_models import _openrouter_contracts

    calls = []
    contract = catalog.openrouter_reasoning_contract(
        {"supported_efforts": ["high"], "mandatory": True}
    )

    def discover(**options):
        calls.append(options)
        return (catalog.ProviderModelOption("custom-model", "Custom", reasoning_contract=contract),)

    monkeypatch.setattr(catalog, "discover_provider_models", discover)
    _openrouter_contracts.cache_clear()
    try:
        settings = Settings(
            tmp_path / "bots.db",
            "t" * 40,
            "fixture",
            "https://openrouter.ai/api/v1",
            "custom-model",
            models=["other"],
        )
        with TestClient(create_app(settings)) as api:
            api.headers["Authorization"] = "Bearer " + settings.token
            rows = api.get("/v1/models").json()["models"]
            assert [e["id"] for e in rows[0]["reasoning_efforts"]] == ["high"]
            assert rows[1]["reasoning_efforts"] == []
            assert len(calls) == 1 and calls[0]["api_key"] is None
    finally:
        _openrouter_contracts.cache_clear()


def test_toggle_only_effort_survives_work_client_and_stays_on_its_owl(tmp_path):
    from alysis_code.bots.owl_models import work_client

    settings = Settings(
        tmp_path / "bots.db",
        "t" * 40,
        "fixture",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "qwen3-32b",
    )
    app = create_app(settings)
    with TestClient(app) as api:
        api.headers["Authorization"] = "Bearer " + settings.token
        choices = api.get("/v1/models").json()["models"][0]["reasoning_efforts"]
        assert [option["id"] for option in choices] == ["none", "enabled"]
        for option, expected in (("none", ("none", False)), ("enabled", (None, True))):
            owl = api.post("/v1/bots", json={"name": "Toggle", "reasoning_effort": option}).json()
            chosen, _ = app.state.runtime.turn_model("task", owl["id"])
            client = work_client(chosen)
            assert (client.reasoning_effort, client.enable_thinking) == expected
        assert settings.reasoning_effort is None


def test_existing_owls_inherit_their_previous_thinking_when_the_database_upgrades(tmp_path):
    from alysis_code.bots.models import BotInput
    from alysis_code.bots.store import Store

    path = tmp_path / "bots.db"
    original = Store(path).save_bot("owner", BotInput(name="Apollo", model="existing"))
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE bots DROP COLUMN reasoning_effort")
        db.execute("PRAGMA user_version=51")
    upgraded = Store(path).bot("owner", original["id"])
    assert (upgraded["model"], upgraded["reasoning_effort"]) == ("existing", None)


def test_saved_effort_reaches_the_next_provider_request_after_restart(tmp_path):
    from alysis_code.bots.owl_models import work_client

    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", ENDPOINT, "glm-5.3-flash", models=["glm-5.4"]
    )
    with TestClient(create_app(settings)) as api:
        api.headers["Authorization"] = "Bearer " + settings.token
        assert "owl_reasoning_effort" in api.get("/v1/status").json()["capabilities"]
        models = api.get("/v1/models").json()["models"]
        assert [e["id"] for e in models[1]["reasoning_efforts"]] == ["low", "high", "max"]
        owl = api.post("/v1/bots", json={"name": "Apollo"}).json()
        path = f"/v1/bots/{owl['id']}"
        assert api.put(
            path, json={"name": "Apollo", "model": "glm-5.4", "reasoning_effort": "high"}
        ).is_success
        invalid = api.put(path, json={"name": "Apollo", "reasoning_effort": "unsupported"})
        assert invalid.status_code == 422
        assert api.put(path, json={"name": "Apollo II"}).json()["reasoning_effort"] == "high"

    app = create_app(settings)
    with TestClient(app) as api:
        api.headers["Authorization"] = "Bearer " + settings.token
        stored = api.get("/v1/bots").json()[0]
        assert (stored["model"], stored["reasoning_effort"]) == ("glm-5.4", "high")
        sent = []

        def record(request):
            sent.append(json.loads(request.content))
            return httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
            )

        client, _ = app.state.runtime.turn_model("next-turn", owl["id"])
        client._transport = httpx.MockTransport(record)
        work_client(client).chat(messages=[{"role": "user", "content": "Hello"}])
        assert (sent[0]["model"], sent[0]["reasoning_effort"]) == ("glm-5.4", "high")
        # Model-only clients clear the effort as part of the same save.
        assert (
            api.put(path, json={"name": "Apollo", "model": None}).json()["reasoning_effort"] is None
        )
        assert api.put(path, json={"name": "Apollo", "reasoning_effort": "low"}).is_success
        default_client, _ = app.state.runtime.turn_model("another-turn", owl["id"])
        assert (default_client.model, default_client.reasoning_effort) == ("glm-5.3-flash", "low")


def test_the_model_is_advertised_offered_validated_and_kept(tmp_path):
    settings = Settings(
        tmp_path / "bots.db",
        "t" * 40,
        "fixture-key",
        ENDPOINT,
        "glm-5.3",
        models=["glm-5.3-flash", "glm-5.3"],
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "owl_models" in client.get("/v1/status").json()["capabilities"]
        listed = client.get("/v1/models").json()["models"]
        # The service's own model first, as the default; each model once.
        assert [(item["id"], item["default"]) for item in listed] == [
            ("glm-5.3", True),
            ("glm-5.3-flash", False),
        ]
        # What each can do comes from its declared capacities; credentials never appear.
        assert [item["image_input"] for item in listed] == [False, True]
        assert all(item["context_window"] > 0 for item in listed)
        assert "fixture-key" not in str(listed)

        bot = client.post("/v1/bots", json={"name": "Ειδήσεις", "model": "glm-5.3-flash"}).json()
        assert bot["model"] == "glm-5.3-flash"
        refused = client.post("/v1/bots", json={"name": "花子", "model": "not-offered-here"})
        assert refused.status_code == 409
        assert "not-offered-here" in refused.json()["detail"]
        assert client.post("/v1/bots", json={"name": "花子", "model": ""}).status_code == 422

        # An app from before models saves the owl without one: its model stays.
        renamed = client.put(f"/v1/bots/{bot['id']}", json={"name": "Νέα"}).json()
        assert (renamed["name"], renamed["model"]) == ("Νέα", "glm-5.3-flash")
        path = f"/v1/bots/{bot['id']}"
        assert client.put(path, json={"name": "Νέα", "model": "elsewhere"}).status_code == 409
        assert client.put(path, json={"name": "Νέα", "model": None}).json()["model"] is None
        assert [owl["model"] for owl in client.get("/v1/bots").json()] == [None]


def test_a_kept_model_no_longer_offered_does_not_block_other_settings(tmp_path):
    offered = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", ENDPOINT, "glm-5.3", models=["glm-5.3-flash"]
    )
    with TestClient(create_app(offered)) as client:
        client.headers["Authorization"] = "Bearer " + offered.token
        bot = client.post("/v1/bots", json={"name": "Ειδήσεις", "model": "glm-5.3-flash"}).json()
    # The service no longer offers that model.
    narrowed = Settings(tmp_path / "bots.db", "t" * 40, "fixture", ENDPOINT, "glm-5.3")
    with TestClient(create_app(narrowed)) as client:
        client.headers["Authorization"] = "Bearer " + narrowed.token
        saved = client.put(
            f"/v1/bots/{bot['id']}",
            json={"name": "Ειδήσεις", "purpose": "Πρωινά νέα", "model": "glm-5.3-flash"},
        )
        assert saved.status_code == 200 and saved.json()["purpose"] == "Πρωινά νέα"
        assert [item["id"] for item in client.get("/v1/models").json()["models"]] == ["glm-5.3"]


def test_owls_on_this_computer_pick_from_the_saved_model_connections(tmp_path, monkeypatch):
    from alysis_code.bots.local_service import saved_connections, settings_from_bootstrap

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_LOCAL_TEST_KEY", "placeholder")
    cfg = AppConfig(model="glm-5.3", base_url=ENDPOINT)
    profile = {
        "protocol": "openai_compat",
        "base_url": ENDPOINT,
        "api_key_env": "ALYSIS_LOCAL_TEST_KEY",
    }
    cfg.extra_fields["profiles"] = {
        "fast": {**profile, "default_model": "glm-5.3"},
        "research": {**profile, "default_model": "glm-5.3-flash"},
        "no-key": {**profile, "default_model": "glm-5.3", "api_key_env": "ALYSIS_UNSET_TEST_KEY"},
        "plain-http": {**profile, "base_url": "http://models.example.invalid/v1"},
    }
    cfg.extra_fields["active_profile"] = "fast"
    save_config(cfg)
    settings = settings_from_bootstrap(
        {"directory": str(tmp_path / "data"), "token": "t" * 40, "profile": "fast"}
    )
    listed = saved_connections(settings)()
    # The connection the owls started with is the default; others need a key and an endpoint
    # the service could start with.
    assert [(item.id, item.default, item.settings.model) for item in listed] == [
        ("fast", True, "glm-5.3"),
        ("research", False, "glm-5.3-flash"),
    ]
    assert listed[1].settings.provider_key == "research"
    # A key removed in the app counts from the next read.
    monkeypatch.delenv("ALYSIS_LOCAL_TEST_KEY")
    assert [item.id for item in saved_connections(settings)()] == ["fast"]
