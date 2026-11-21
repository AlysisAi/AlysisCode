"""The owl list says when each owl's next routine run is due, and the status advertises it."""

from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app


def test_the_owl_list_carries_each_owls_next_routine(tmp_path):
    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "next_routine_at" in client.get("/v1/status").json()["capabilities"]
        owl = client.post("/v1/bots", json={"name": "Αθηνά", "purpose": "Σύνοψη"}).json()
        listed = client.get("/v1/bots").json()
        assert [item["next_routine_at"] for item in listed] == [None]

        base = f"/v1/bots/{owl['id']}/schedules"
        payload = {
            "name": "Κάθε ώρα",
            "prompt": "Γράψε μια σύντομη σύνοψη.",
            "timing": {"kind": "interval", "every_minutes": 60},
        }
        routine = client.post(base, json=payload).json()
        # Switched off, and due sooner: it never counts.
        client.post(
            base,
            json={**payload, "timing": {"kind": "interval", "every_minutes": 5}, "enabled": False},
        )
        listed = client.get("/v1/bots").json()
        assert [item["next_routine_at"] for item in listed] == [routine["next_run_at"]]

        client.patch(f"/v1/bots/{owl['id']}/organize", json={"archived": True})
        assert [item["next_routine_at"] for item in client.get("/v1/bots").json()] == [None]
