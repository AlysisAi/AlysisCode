from contextlib import contextmanager
from uuid import uuid4

from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.managed_cloud import AccountApplications, AccountAuthority
from alysis_code.bots.publications import public_origin
from alysis_code.bots.sharing_service import RemotePublications
from alysis_code.bots.store import Store


def test_publication_origin_rejects_invalid_operator_settings():
    for value in (
        None,
        "https://[",
        "https://reader.test:65536",
        "https://*.test",
        "http://reader.test",
    ):
        assert public_origin(value) is None
    assert public_origin("https://Reader.test:8443/") == "https://reader.test:8443"


def test_shared_snapshot_and_published_dashboard_through_managed_account_boundary(
    tmp_path, monkeypatch
):
    identities = [str(uuid4()) for _ in range(4)]
    keys = ["slk_" + uuid4().hex for _ in identities]
    registry = Store(tmp_path / "account-registry")
    authority = AccountAuthority(
        registry,
        b"x" * 32,
        lookup=lambda key: {
            "account_id": identities[keys.index(key)],
            **(
                {"status": "upgrade_required", "plan": "free"}
                if key == keys[3]
                else {
                    "status": "available",
                    "plan": "alpha",
                    "address": "https://reader.example.test",
                }
            ),
        },
    )
    monkeypatch.setattr(
        "alysis_code.bots.sharing_service.sharing_location",
        lambda key: ("https://reader.example.test", True),
    )
    resolved = []

    def recipients(owner, emails, *, credential=None):
        resolved.append((owner, emails))
        return [identities[1]] if emails == ["reader@example.test"] else []

    authority.publication_recipients = recipients

    def factory(owner, credential=None):
        settings = Settings(
            database=tmp_path / owner.replace(":", "-") / "bots.sqlite",
            owner=owner,
            token="synthetic-" + "x" * 40,
            model="synthetic",
            base_url="https://model.example.test/v1",
            api_key="synthetic",
        )
        app = create_app(settings)
        app.state.account_token = settings.token
        return app

    def service(reader_origin="https://reader.example.test"):
        return TestClient(
            AccountApplications(
                factory("operator"), authority, factory, reader_origin=reader_origin
            ).application()
        )

    def path(i):
        return "/accounts/" + identities[i] + "/v1/sharing"

    def auth(i):
        return {"Authorization": "Bearer " + keys[i]}

    source = uuid4().hex
    chat = dict(
        kind="conversation",
        source=source,
        title="Revisión",
        content="Lisboa · 14",
        version=0,
        revocation_epoch=0,
        recipients=["Reader@example.test"],
    )
    with service() as client:
        assert client.post(path(0), json=chat).status_code == 401
        assert client.post(path(0), json=chat, headers=auth(1)).status_code == 401
        result = client.post(path(0), json=chat, headers=auth(0))
        assert result.status_code == 200, result.text
        shared = result.json()
        assert resolved == [("account:" + identities[0], ["reader@example.test"])]
        assert "url" not in shared
        assert client.get("/published/" + shared["id"]).status_code == 404
        assert client.get(path(2) + "/" + shared["id"], headers=auth(2)).status_code == 404
        assert (
            client.get(path(1) + "/" + shared["id"], headers=auth(1)).json()["content"]
            == chat["content"]
        )
        assert client.get(path(1), headers=auth(1)).json()["received"][0]["id"] == shared["id"]

        # The same snapshot is available to a local recipient over the dedicated, signed-in
        # account boundary. It needs neither a running Cloud Owl nor a stored cloud credential.
        @contextmanager
        def transport(**kwargs):
            yield client

        remote = RemotePublications(
            lambda: keys[1], lambda key: ("https://reader.example.test", True), transport
        )
        assert remote.browse()["received"][0]["id"] == shared["id"]
        assert remote.receive(shared["id"])["content"] == chat["content"]
        assert client.get("/sharing-api/v1/sharing").status_code == 401
        assert client.get("/sharing-api/v1/sharing", headers=auth(3)).json()["received"] == []
        assert client.get(path(3), headers=auth(3)).status_code == 403
        assert (
            client.post(
                "/sharing-api/v1/sharing/inbox",
                headers=auth(1),
                json={"before_time": shared["updated_at"], "before_id": shared["id"]},
            ).json()["received"]
            == []
        )
        assert client.post(path(0), json=chat, headers=auth(0)).json()["id"] == shared["id"]
        assert (
            client.post(path(0), json={**chat, "content": "outdated"}, headers=auth(0)).status_code
            == 409
        )
        changed = client.post(
            path(0),
            json={**chat, "content": "東京 · 12", "version": 1, "expected_id": shared["id"]},
            headers=auth(0),
        ).json()
        assert changed["id"] == shared["id"] and changed["version"] == 2
        assert (
            client.post(
                path(0),
                json={**chat, "version": 2, "recipients": ["unknown@example.test"]},
                headers=auth(0),
            ).status_code
            == 422
        )
        dashboard = dict(
            kind="dashboard",
            source=source,
            title="Σύνολο",
            content="<p>Résumé</p>",
            version=0,
            revocation_epoch=0,
        )
        public = client.post(path(0), json=dashboard, headers=auth(0)).json()
        assert public["url"] == "https://reader.example.test/published/" + public["id"]
        client.post(
            path(0),
            headers=auth(0),
            json={
                **dashboard,
                "version": 1,
                "expected_id": public["id"],
                "content": "<h1>Αύριο</h1>",
            },
        ).raise_for_status()
    with service() as client:
        page = client.get("/published/" + public["id"])
        assert page.status_code == 200 and "Αύριο" in page.text
        assert 'sandbox="allow-scripts"' in page.text and "allow-same-origin" not in page.text
        assert page.headers["cache-control"] == "no-store"
        for item in [changed, {**public, "version": 2}]:
            revoke = "/" + item["id"] + "/revoke"
            assert (
                client.post(path(2) + revoke, headers=auth(2), json={"version": 2}).status_code
                == 409
            )
            assert client.post(path(0) + revoke, headers=auth(0), json={"version": 2}).json()[
                "revoked"
            ]
        assert client.get("/published/" + public["id"]).status_code == 404
        assert client.get(path(1), headers=auth(1)).json()["received"] == []
        assert client.post(path(0), json=chat, headers=auth(0)).status_code == 409
        fresh_epoch = client.get(path(0), headers=auth(0)).json()["revocation_epoch"]
        fresh = client.post(
            path(0), json={**chat, "revocation_epoch": fresh_epoch}, headers=auth(0)
        ).json()
        assert fresh["id"] != shared["id"] and fresh["version"] == 1
        delayed_edit = {
            **chat,
            "version": 1,
            "expected_id": shared["id"],
            "content": "Stale shared text",
        }
        assert client.post(path(0), json=delayed_edit, headers=auth(0)).status_code == 409
    with service(None) as client:
        assert not client.get(path(0), headers=auth(0)).json()["can_publish"]
        assert client.post(path(0), headers=auth(0), json=dashboard).status_code == 503
