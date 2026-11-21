"""Pinning, archiving and duplicating owls over HTTP: advertised, validated and owner-scoped."""

from uuid import uuid4

from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app


def test_owls_are_pinned_archived_and_duplicated_over_http(tmp_path):
    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "owl_organize" in client.get("/v1/status").json()["capabilities"]
        owl = client.post(
            "/v1/bots",
            json={"name": "Νεφέλη", "purpose": "Έρευνα", "color": "purple", "background": "stars"},
        ).json()
        assert (owl["pinned_at"], owl["archived_at"]) == (None, None)

        pinned = client.patch(f"/v1/bots/{owl['id']}/organize", json={"pinned": True}).json()
        assert pinned["pinned_at"] is not None and pinned["archived_at"] is None
        archived = client.patch(f"/v1/bots/{owl['id']}/organize", json={"archived": True}).json()
        assert archived["pinned_at"] == pinned["pinned_at"]
        assert archived["archived_at"] is not None
        listed = client.get("/v1/bots").json()
        assert [(item["pinned_at"], item["archived_at"]) for item in listed] == [
            (archived["pinned_at"], archived["archived_at"])
        ]
        # A rename from an app without these keeps them.
        renamed = client.put(f"/v1/bots/{owl['id']}", json={"name": "Σύννεφο", "purpose": "Έρευνα"})
        assert (renamed.json()["pinned_at"], renamed.json()["archived_at"]) == (
            archived["pinned_at"],
            archived["archived_at"],
        )
        # Nothing to change, a field it doesn't know, or a value that isn't a yes or no.
        for body in ({}, {"folder": "x"}, {"pinned": "yes"}, {"archived": 1}):
            assert client.patch(f"/v1/bots/{owl['id']}/organize", json=body).status_code == 422
        assert (
            client.patch(f"/v1/bots/{'0' * 32}/organize", json={"pinned": True}).status_code == 404
        )
        assert client.get("/v1/needs-you").json()["count"] == 0

        request_id = "dup_" + uuid4().hex
        body = {"name": "Σύννεφο (αντίγραφο)", "request_id": request_id}
        made = client.post(f"/v1/bots/{owl['id']}/duplicate", json=body)
        assert made.status_code == 201
        copy = made.json()
        assert (copy["name"], copy["purpose"], copy["color"], copy["background"]) == (
            "Σύννεφο (αντίγραφο)",
            "Έρευνα",
            "purple",
            "stars",
        )
        assert (copy["pinned_at"], copy["archived_at"]) == (None, None)
        # A retried duplicate answers with the copy it already made.
        assert client.post(f"/v1/bots/{owl['id']}/duplicate", json=body).json()["id"] == copy["id"]
        assert len(client.get("/v1/bots").json()) == 2
        assert (
            client.post(
                f"/v1/bots/{owl['id']}/duplicate", json={"name": "名前", "request_id": "short"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/v1/bots/{owl['id']}/duplicate",
                json={**body, "request_id": "dup_" + uuid4().hex, "chats": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/v1/bots/{'0' * 32}/duplicate", json=body | {"request_id": "x" * 12}
            ).status_code
            == 404
        )

        back = client.patch(f"/v1/bots/{owl['id']}/organize", json={"archived": False}).json()
        assert back["archived_at"] is None and back["pinned_at"] == pinned["pinned_at"]
