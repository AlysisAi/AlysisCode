"""An owl's model over HTTP: advertised, offered by the place, validated and kept. Needs FastAPI."""

from __future__ import annotations

from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.config import AppConfig, save_config

ENDPOINT = "https://api.z.ai/api/coding/paas/v4"


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
