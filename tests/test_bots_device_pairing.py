"""Real API/device authority checks with scripted jobs; no live-model evidence."""

import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.local_computers import LocalComputerPending
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.transfers import FileTransfers


def test_incomplete_http_body_never_admits_a_local_command(api, monkeypatch):
    from starlette.requests import ClientDisconnect, Request

    app, client, _, device, _ = api
    row = job(api)

    async def disconnected(_request):
        raise ClientDisconnect()

    monkeypatch.setattr(Request, "json", disconnected)
    response = client.post(
        f"/v1/local-computers/{D}/requests/{row['id']}",
        headers=device,
        json={"action": "claim", "session_id": S},
    )
    assert response.status_code == 400
    with app.state.store.connect() as db:
        assert (
            db.execute("SELECT status FROM bot_local_requests WHERE id=?", (row["id"],)).fetchone()[
                0
            ]
            == "pending"
        )
        assert db.execute("SELECT count(*) FROM bot_local_admissions").fetchone()[0] == 0


def test_revocation_between_transfer_admission_and_write_prevents_publication(api, monkeypatch):
    app, client, account, device, _ = api
    row = job(api, "import_local_file", path="/tmp/test", name="test.bin")
    path = f"/v1/local-computers/{D}/requests/{row['id']}"
    assert (
        client.post(path, headers=device, json={"action": "claim", "session_id": S}).status_code
        == 200
    )
    original = FileTransfers.begin

    def revoke_before_write(self, *args, **kwargs):
        app.state.device_grants.revoke(D)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FileTransfers, "begin", revoke_before_write)
    assert (
        client.post(
            path + "/transfer",
            headers=device,
            json={"operation": "begin", "size": 4, "session_id": S},
        ).status_code
        == 401
    )
    with app.state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM file_uploads").fetchone()[0] == 0


def test_rotated_credential_cannot_claim_or_fabricate_an_old_receipt(api):
    _, client, account, device, body = api
    row = job(api)
    base = f"/v1/local-computers/{D}"
    path = base + f"/requests/{row['id']}"
    assert (
        client.post(path, headers=device, json={"action": "claim", "session_id": S}).status_code
        == 200
    )
    assert client.post(base + "/revoke", headers=account).status_code == 200
    assert (
        client.post(
            base + "/pair",
            headers=account,
            json={**body, "token_hash": hashlib.sha256(OTHER_KEY.encode()).hexdigest()},
        ).status_code
        == 200
    )
    replacement = {"Authorization": "Bearer " + OTHER_KEY}
    assert (
        client.post(
            path, headers=replacement, json={"action": "claim", "session_id": S}
        ).status_code
        == 409
    )
    assert (
        client.post(
            path,
            headers=replacement,
            json={
                "action": "result",
                "session_id": S,
                "result": {"executed": True, "status": "finished"},
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            path,
            headers=replacement,
            json={
                "action": "result",
                "session_id": S,
                "result": {"executed": "unknown", "status": "interrupted"},
            },
        ).status_code
        == 200
    )


def test_export_is_bound_to_approved_file_and_session(api):
    app, client, _, device, _ = api
    from alysis_code.bots.models import FileInput

    # Reserve the request's bot first, then replace only this synthetic fixture's
    # arguments with the actual saved Library id before admission.
    row = job(api, "export_local_file", path="/tmp/export.bin", file_id="e" * 32)
    saved = app.state.store.save_file(
        "account-one", row["bot_id"], FileInput(name="chosen.txt", content="Καλημέρα 東京")
    )
    import json

    with app.state.store.connect(write=True) as db:
        arguments = json.loads(row["arguments"])
        arguments["file_id"] = saved["id"]
        db.execute(
            "UPDATE bot_local_requests SET arguments=? WHERE id=?",
            (json.dumps(arguments), row["id"]),
        )
    path = f"/v1/local-computers/{D}/requests/{row['id']}"
    assert (
        client.post(path, headers=device, json={"action": "claim", "session_id": S}).status_code
        == 200
    )
    payload = {"session_id": S, "operation": "download", "offset": 0}
    response = client.post(path + "/transfer", headers=device, json=payload)
    assert response.status_code == 200
    assert base64.b64decode(response.json()["data_base64"]) == "Καλημέρα 東京".encode()
    assert (
        client.post(
            path + "/transfer", headers=device, json={**payload, "session_id": "f" * 32}
        ).status_code
        == 409
    )
    assert (
        client.post(
            path + "/transfer", headers=device, json={**payload, "file_id": "f" * 32}
        ).status_code
        == 422
    )


D, S = "a" * 32, "b" * 32
KEY = "device_" + "1" * 64
OTHER_KEY = "device_" + "2" * 64


@pytest.fixture
def api(tmp_path):
    settings = Settings(
        tmp_path / "api.db",
        "t" * 40,
        "scripted",
        "https://example.invalid",
        "scripted",
        owner="account-one",
    )
    app = create_app(settings)
    client = TestClient(app)
    account = {"Authorization": "Bearer " + settings.token}
    device = {"Authorization": "Bearer " + KEY}
    body = {
        "session_id": S,
        "name": "Synthetic Mac",
        "platform": "darwin",
        "token_hash": hashlib.sha256(KEY.encode()).hexdigest(),
    }
    assert (
        client.post(f"/v1/local-computers/{D}/pair", headers=account, json=body).status_code == 200
    )
    assert (
        client.post(
            f"/v1/local-computers/{D}/connect",
            headers=device,
            json={k: v for k, v in body.items() if k != "token_hash"},
        ).status_code
        == 200
    )
    yield app, client, account, device, body
    client.close()


def job(api, kind="local_command", **args):
    app, _, _, _, _ = api
    store, runtime = app.state.store, app.state.runtime
    bot = store.save_bot("account-one", BotInput(name="Test", purpose="Synthetic"))
    chat = store.create_chat("account-one", bot["id"], "Test")
    store.enqueue(
        "account-one",
        chat["id"],
        TaskInput(message="Synthetic local action", request_id=chat["id"]),
    )
    task = store.claim("account-one")
    arguments = {
        "device_id": D,
        **({"cwd": "/tmp", "command": "true"} if kind == "local_command" else args),
    }
    with pytest.raises(LocalComputerPending):
        runtime.local_computers.request(task, kind, arguments, lambda *_: None)
    runtime.local_computers.park(task, [])
    with store.connect() as db:
        row = dict(
            db.execute("SELECT * FROM bot_local_requests WHERE task_id=?", (task["id"],)).fetchone()
        )
    return row


def test_pairing_is_account_bound_idempotent_and_not_generic_api_access(api):
    app, client, account, device, body = api
    path = f"/v1/local-computers/{D}"
    assert client.post(path + "/pair", headers=account, json=body).json()["paired"]
    assert client.post(path + "/pair", headers=device, json=body).status_code == 401
    assert client.get("/v1/bots", headers=device).status_code == 401
    assert client.post(path + "/connect", headers=account, json={}).status_code == 401
    assert (
        client.post(f"/v1/local-computers/{'c' * 32}/connect", headers=device, json={}).status_code
        == 401
    )
    assert (
        client.post(
            path + "/pair",
            headers=account,
            json={**body, "token_hash": hashlib.sha256(OTHER_KEY.encode()).hexdigest()},
        ).status_code
        == 409
    )
    with app.state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM bot_local_device_grants").fetchone()[0] == 1
        assert KEY not in str(
            [tuple(r) for r in db.execute("SELECT * FROM bot_local_device_grants")]
        )


def test_revocation_fences_claims_but_retains_only_admitted_receipt_authority(api):
    app, client, account, device, _ = api
    first, second = job(api), job(api)
    path = f"/v1/local-computers/{D}"
    request = path + "/requests/" + first["id"]
    assert (
        client.post(request, headers=device, json={"session_id": S, "action": "claim"}).status_code
        == 200
    )
    assert client.post(path + "/revoke", headers=account, json={}).status_code == 200
    assert client.post(path + "/connect", headers=device, json={}).status_code == 401
    for row in [first, second]:
        assert (
            client.post(
                path + "/requests/" + row["id"],
                headers=device,
                json={"session_id": S, "action": "claim"},
            ).status_code
            == 401
        )
    receipt = {
        "session_id": S,
        "action": "result",
        "result": {"executed": True, "status": "stopped"},
    }
    assert client.post(request, headers=device, json=receipt).status_code == 200
    assert client.post(request, headers=device, json=receipt).status_code == 200
    assert (
        client.post(path + "/requests/" + second["id"], headers=device, json=receipt).status_code
        == 409
    )
    assert app.state.store.task("account-one", first["task_id"])["status"] == "cancelled"


def test_device_transfer_is_bound_to_claim_kind_and_file_and_rechecks_revocation(api):
    app, client, account, device, _ = api
    row = job(api, "import_local_file", path="/tmp/synthetic.bin", name="Synthetic.bin")
    path = f"/v1/local-computers/{D}/requests/{row['id']}"
    transfer = path + "/transfer"

    def step(operation, **values):
        return client.post(
            transfer, headers=device, json={"session_id": S, "operation": operation, **values}
        )

    assert step("begin", size=4).status_code == 409
    assert (
        client.post(path, headers=device, json={"session_id": S, "action": "claim"}).status_code
        == 200
    )
    assert step("begin", size=4).status_code == 200
    assert step("download", offset=0).status_code == 409
    assert (
        step("append", offset=0, data_base64=base64.b64encode(b"\0\xffAB").decode()).status_code
        == 200
    )
    finished = step("complete").json()
    assert step("metadata").json()["sha256"] == hashlib.sha256(b"\0\xffAB").hexdigest()
    assert (
        app.state.store.file_data("account-one", row["bot_id"], finished["file_id"]) == b"\0\xffAB"
    )
    assert step("metadata", file_id="c" * 32).status_code == 422
    client.post(f"/v1/local-computers/{D}/revoke", headers=account, json={})
    assert step("metadata").status_code == 401


def test_stolen_device_id_and_another_account_token_do_not_cross_accounts(api, tmp_path):
    _, client, account, device, body = api
    other = create_app(
        Settings(
            tmp_path / "other.db",
            "z" * 40,
            "scripted",
            "https://example.invalid",
            "scripted",
            owner="account-two",
        )
    )
    with TestClient(other) as remote:
        assert (
            remote.post(f"/v1/local-computers/{D}/pair", headers=account, json=body).status_code
            == 401
        )
        assert (
            remote.post(f"/v1/local-computers/{D}/connect", headers=device, json={}).status_code
            == 401
        )
    row = job(api)
    forged = {"Authorization": "Bearer " + OTHER_KEY}
    assert (
        client.post(
            f"/v1/local-computers/{D}/requests/{row['id']}",
            headers=forged,
            json={"session_id": S, "action": "claim"},
        ).status_code
        == 401
    )
