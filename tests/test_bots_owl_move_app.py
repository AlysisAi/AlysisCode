"""The routes that move an owl between two Owls services, as the desktop app calls them."""

from __future__ import annotations

import base64

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotInput, FileInput, MemoryInput, TaskInput
from alysis_code.bots.owl_move import MOVED_PAUSE_CODE
from alysis_code.bots.scheduling import ScheduleInput

TOKEN = "t" * 40


def service(tmp_path, name):
    settings = Settings(
        tmp_path / name / "bots.db", TOKEN, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings)
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + TOKEN
    return app, client, settings.owner


def owl_with_files(app, owner):
    store = app.state.store
    bot = store.save_bot(owner, BotInput(name="Ήρα", purpose="日本語の要約", color="teal"))
    chat = store.create_chat(owner, bot["id"], "Συνομιλία")
    store.enqueue(owner, chat["id"], TaskInput(message="Καλημέρα", request_id="request-greek-1"))
    task = store.claim(owner)
    store.finish(owner, task["id"], "completed", content="おはようございます")
    store.save_file(owner, bot["id"], FileInput(name="λίστα.txt", content="ένα\nδύο\n"))
    store.save_bytes(owner, bot["id"], "画像.bin", bytes(range(256)) * 1200)
    store.save_memory(owner, bot["id"], MemoryInput(title="Όνομα", content="Με λένε Ελένη"))
    store.save_schedule(
        owner,
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "毎日",
                "prompt": "Σύνοψη ημέρας",
                "timing": {"kind": "interval", "every_minutes": 30},
            }
        ),
    )
    return bot["id"], chat["id"]


def drive(source, target, owl, request_id="move-request-http-1"):
    """What Electron main does: rows page by page, files by the resumable routes, then finish."""
    begun = target.post("/v1/owl-moves", json={"request_id": request_id, "source_bot_id": owl})
    assert begun.status_code == 200, begun.text
    move = begun.json()
    cursor, number, files, first = None, 0, [], None
    while True:
        page = source.post(f"/v1/bots/{owl}/move/export", json={"cursor": cursor}).json()
        first = first or page
        files += [record for record in page["records"] if record["kind"] == "file"]
        sent = target.post(
            f"/v1/owl-moves/{move['id']}/pages",
            json={"page": number, "last": page["cursor"] is None, "records": page["records"]},
        )
        assert sent.status_code == 200, sent.text
        number += 1
        cursor = page["cursor"]
        if cursor is None:
            break
    receipt = target.get(f"/v1/owl-moves/{move['id']}").json()
    staged = receipt["bot_id"]
    previous = {}
    for record in sorted(files, key=lambda item: (item["name"], item["revision"])):
        upload = target.post(
            f"/v1/bots/{staged}/uploads",
            json={
                "name": record["name"],
                "size": record["size_bytes"],
                "request_id": f"move-{move['id']}-{record['id']}",
                **(
                    {"previous_file_id": previous[record["name"]]}
                    if record["name"] in previous
                    else {}
                ),
            },
        ).json()
        offset = 0
        while offset < record["size_bytes"]:
            chunk = source.get(f"/v1/bots/{owl}/files/{record['id']}/chunks/{offset}").json()
            assert (
                target.put(
                    f"/v1/bots/{staged}/uploads/{upload['id']}",
                    json={"offset": offset, "data_base64": chunk["data_base64"]},
                ).status_code
                == 200
            )
            offset = chunk["next_offset"]
        done = target.post(f"/v1/bots/{staged}/uploads/{upload['id']}/complete").json()
        previous[record["name"]] = done["file_id"]
    return move, first


def test_status_lists_the_move_capability(tmp_path):
    _app, client, _owner = service(tmp_path, "here")
    assert "owl_move" in client.get("/v1/status").json()["capabilities"]


def test_an_owl_moves_between_two_services_over_http(tmp_path):
    here_app, here, owner = service(tmp_path, "here")
    there_app, there, _ = service(tmp_path, "there")
    owl, _chat = owl_with_files(here_app, owner)

    summary = here.get(f"/v1/bots/{owl}/move").json()
    assert summary["working"] is False and summary["files"] == 2 and summary["turns"] == 1
    move, first = drive(here, there, owl)
    # Hidden there until complete.
    assert there.get("/v1/bots").json() == []
    completed = there.post(f"/v1/owl-moves/{move['id']}/complete")
    assert completed.status_code == 200, completed.text
    copy = completed.json()["owl"]
    assert copy["name"] == "Ήρα" and copy["color"] == "teal"
    # The original goes through the ordinary delete path, only if it didn't change.
    assert (
        here.post(
            f"/v1/bots/{owl}/move/finish", json={"fingerprint": first["fingerprint"]}
        ).status_code
        == 204
    )
    assert here.get("/v1/bots").json() == []
    assert [item["id"] for item in there.get("/v1/bots").json()] == [copy["id"]]
    files = there.post(f"/v1/bots/{copy['id']}/files/page", json={}).json()["items"]
    assert sorted(item["name"] for item in files) == ["λίστα.txt", "画像.bin"]
    text = next(item for item in files if item["name"] == "λίστα.txt")
    assert there.get(f"/v1/bots/{copy['id']}/files/{text['id']}").json()["content"] == "ένα\nδύο\n"
    (routine,) = there.get(f"/v1/bots/{copy['id']}/schedules").json()
    assert routine["state"] == "paused" and routine["pause_code"] == MOVED_PAUSE_CODE
    # Completing again, or sending a page again, makes no second copy.
    assert there.post(f"/v1/owl-moves/{move['id']}/complete").json()["owl"]["id"] == copy["id"]
    again = there.post(
        "/v1/owl-moves", json={"request_id": "move-request-http-1", "source_bot_id": owl}
    ).json()
    assert again["id"] == move["id"] and again["state"] == "complete"
    assert len(there.get("/v1/bots").json()) == 1


def test_a_changed_or_working_owl_stays_where_it_was(tmp_path):
    here_app, here, owner = service(tmp_path, "here")
    there_app, there, _ = service(tmp_path, "there")
    owl, chat = owl_with_files(here_app, owner)
    move, first = drive(here, there, owl)
    copy = there.post(f"/v1/owl-moves/{move['id']}/complete").json()["owl"]
    here_app.state.store.save_memory(owner, owl, MemoryInput(title="Νέο", content="Αλλαγή"))
    refused = here.post(f"/v1/bots/{owl}/move/finish", json={"fingerprint": first["fingerprint"]})
    assert refused.status_code == 409
    assert [item["id"] for item in here.get("/v1/bots").json()] == [owl]
    # The app takes the copy back with the ordinary delete.
    assert there.delete(f"/v1/bots/{copy['id']}").status_code == 204
    assert there.get("/v1/bots").json() == []
    # A working owl is not read at all.
    here_app.state.store.enqueue(
        owner, chat, TaskInput(message="Και κάτι ακόμα", request_id="request-greek-2")
    )
    assert here.get(f"/v1/bots/{owl}/move").json()["working"] is True
    assert here.post(f"/v1/bots/{owl}/move/export", json={}).status_code == 409


def test_move_routes_refuse_tampered_oversized_and_unauthorised_requests(tmp_path):
    here_app, here, owner = service(tmp_path, "here")
    _there_app, there, _ = service(tmp_path, "there")
    owl, _chat = owl_with_files(here_app, owner)
    page = here.post(f"/v1/bots/{owl}/move/export", json={}).json()
    move = there.post(
        "/v1/owl-moves", json={"request_id": "move-request-http-2", "source_bot_id": owl}
    ).json()
    tampered = [dict(page["records"][0], purpose="x" * 1001), *page["records"][1:]]
    refused = there.post(
        f"/v1/owl-moves/{move['id']}/pages", json={"page": 0, "last": True, "records": tampered}
    )
    assert refused.status_code == 422
    assert "x" * 50 not in refused.text
    assert (
        there.post(
            f"/v1/owl-moves/{move['id']}/pages", json={"page": 0, "last": True, "records": "all"}
        ).status_code
        == 422
    )
    assert (
        there.post("/v1/owl-moves", json={"request_id": "short", "source_bot_id": owl}).status_code
        == 422
    )
    assert (
        here.post(f"/v1/bots/{owl}/move/export", json={"cursor": "bm90IGEgY3Vyc29y"}).status_code
        == 422
    )
    assert (
        here.post(f"/v1/bots/{owl}/move/finish", json={"fingerprint": "0" * 64}).status_code == 409
    )
    # Pages may be larger than other requests, but are bounded too.
    huge = '{"page":0,"last":true,"records":[' + '"' + "a" * (4 * 1024 * 1024) + '"]}'
    assert (
        there.post(
            f"/v1/owl-moves/{move['id']}/pages",
            content=huge,
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    anonymous = TestClient(here_app)
    assert anonymous.get(f"/v1/bots/{owl}/move").status_code == 401
    assert anonymous.post("/v1/owl-moves", json={}).status_code == 401
    # Cancelling deletes the copy that was never shown; the original is untouched.
    good = there.post(
        f"/v1/owl-moves/{move['id']}/pages",
        json={"page": 0, "last": page["cursor"] is None, "records": page["records"]},
    )
    assert good.status_code == 200
    assert there.delete(f"/v1/owl-moves/{move['id']}").json()["state"] == "cancelled"
    assert there.get(f"/v1/owl-moves/{move['id']}").json()["state"] == "cancelled"
    assert [item["id"] for item in here.get("/v1/bots").json()] == [owl]
    assert base64.b64decode(
        here.get(f"/v1/bots/{owl}/files/{page_file(here, owl)}/chunks/0").json()["data_base64"]
    )


def page_file(client, owl):
    items = client.post(f"/v1/bots/{owl}/files/page", json={}).json()["items"]
    return next(item["id"] for item in items if item["name"] == "画像.bin")
