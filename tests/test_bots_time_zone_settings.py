"""The person's time zone over HTTP: authenticated, validated, owner-scoped and in the status."""

from dataclasses import replace

from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app

PATH = "/v1/settings/time-zone"


def test_status_advertises_the_setting_and_reports_the_saved_zone(tmp_path):
    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        status = client.get("/v1/status").json()
        assert "time_zone" in status["capabilities"]
        assert status["time_zone"] is None
        assert client.put(PATH, json={"time_zone": "Pacific/Auckland"}).status_code == 200
        assert client.get("/v1/status").json()["time_zone"] == "Pacific/Auckland"


def test_authenticated_setting_is_validated_persists_and_is_owner_scoped(tmp_path):
    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    with TestClient(create_app(settings), headers={"Authorization": "Bearer wrong"}) as client:
        assert client.get(PATH).status_code == 401
        assert client.put(PATH, json={"time_zone": "Europe/Athens"}).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.get(PATH).json() == {"time_zone": None}
        for body in [
            {},
            {"time_zone": ""},
            {"time_zone": None},
            {"time_zone": "Mars/Olympus_Mons"},
            {"time_zone": "Ευρώπη/Αθήνα"},
            {"time_zone": "../../etc/passwd"},
            {"time_zone": "localtime"},
            {"time_zone": "Europe/Athens", "owner": "other"},
        ]:
            assert client.put(PATH, json=body).status_code == 422, body
        assert client.get(PATH).json() == {"time_zone": None}
        assert client.put(PATH, json={"time_zone": "Europe/Athens"}).json() == {
            "time_zone": "Europe/Athens"
        }
        assert client.put(PATH, json={"time_zone": "America/New_York"}).json() == {
            "time_zone": "America/New_York"
        }
    for owner, expected in [("other", None), (settings.owner, "America/New_York")]:
        with TestClient(
            create_app(replace(settings, owner=owner)),
            headers={"Authorization": "Bearer " + settings.token},
        ) as client:
            assert client.get(PATH).json() == {"time_zone": expected}
