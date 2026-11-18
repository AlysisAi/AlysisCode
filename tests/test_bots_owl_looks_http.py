"""An owl's look over HTTP: advertised, validated and kept when an app leaves it out."""

from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app


def test_the_colour_is_advertised_validated_and_kept(tmp_path):
    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "owl_looks" in client.get("/v1/status").json()["capabilities"]
        bot = client.post(
            "/v1/bots", json={"name": "Νεφέλη", "color": "yellow", "background": "stars"}
        ).json()
        assert (bot["color"], bot["background"]) == ("yellow", "stars")
        assert client.post("/v1/bots", json={"name": "花子", "color": "Κίτρινο"}).status_code == 422
        assert (
            client.post("/v1/bots", json={"name": "花子", "background": "#000"}).status_code == 422
        )
        # An app from before colours renames without one: the face stays.
        renamed = client.put(f"/v1/bots/{bot['id']}", json={"name": "Σύννεφο"}).json()
        assert (renamed["name"], renamed["color"], renamed["background"]) == (
            "Σύννεφο",
            "yellow",
            "stars",
        )
        recoloured = client.put(
            f"/v1/bots/{bot['id']}", json={"name": "Σύννεφο", "color": "sky"}
        ).json()
        assert recoloured["color"] == "sky"
        assert [(owl["color"], owl["background"]) for owl in client.get("/v1/bots").json()] == [
            ("sky", "stars")
        ]
