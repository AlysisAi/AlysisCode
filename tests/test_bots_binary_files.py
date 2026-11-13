import base64
import hashlib

import pytest
from fastapi.testclient import TestClient
from test_bots_cooperation import peers as peers
from test_bots_cooperation import submit

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.files import MAX_FILE_BYTES, BinaryFileInput
from alysis_code.bots.models import BotInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store


def test_binary_roundtrip_restart_authority_and_delete(tmp_path):
    path = tmp_path / "bots.db"
    store = Store(path)
    bot = store.save_bot("owner", BotInput(name="Files", purpose="Process files"))
    peer = store.save_bot("owner", BotInput(name="Peer", purpose="Other files"))
    data = bytes(range(256)) * 4096
    saved = store.save_bytes("owner", bot["id"], "Αθήνα.bin", data)
    assert saved["content"] is None and saved["binary"]
    assert saved["sha256"] == hashlib.sha256(data).hexdigest()
    assert saved["size_bytes"] == len(data)
    assert not {"data", "data_base64", "content"}.intersection(
        store.list_files("owner", bot["id"])[0]
    )
    assert Store(path).file_data("owner", bot["id"], saved["id"]) == data
    with pytest.raises(NotFound):
        store.file_data("other", bot["id"], saved["id"])
    with pytest.raises(NotFound):
        store.file_data("owner", peer["id"], saved["id"])
    with pytest.raises(Conflict, match="already exists"):
        store.save_bytes("owner", bot["id"], "Αθήνα.bin", b"replacement")
    store.delete_item("owner", bot["id"], saved["id"], kind="files")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM file_payloads").fetchone()[0] == 0


def test_binary_input_limits_and_corrupt_encoding(tmp_path):
    with pytest.raises(ValueError):
        BinaryFileInput(name="invalid", data_base64="not base64!")
    with pytest.raises(ValueError):
        BinaryFileInput(name="../escape", data_base64="")
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Files", purpose="Process files"))
    with pytest.raises(LimitReached):
        store.save_bytes("owner", bot["id"], "large", b"x" * (MAX_FILE_BYTES + 1))
    assert store.list_files("owner", bot["id"]) == []


def test_peer_binary_selection_rejects_without_empty_fake_file(peers):
    store, ps, a, b, other, task = peers
    file = store.save_bytes("owner", a["id"], "report.pdf", b"%PDF-1.7\0")
    with pytest.raises(Conflict, match="text files only"):
        submit(peers, shared_file_ids=[file["id"]])
    assert ps.list(a["id"]) == []


def test_peer_binary_selection_also_rejects_external_payload(peers, tmp_path):
    store, ps, a, b, other, task = peers
    external = Store(store.path, objects=LocalObjectStorage(tmp_path / "objects"))
    file = external.save_bytes("owner", a["id"], "report.pdf", b"%PDF-1.7\0")
    with pytest.raises(Conflict, match="text files only"):
        submit(peers, shared_file_ids=[file["id"]])
    assert ps.list(a["id"]) == []


def test_binary_http_does_not_expose_bytes_in_metadata(tmp_path):
    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    with TestClient(create_app(settings)) as client:
        bot = client.post(
            "/v1/bots",
            headers={"Authorization": "Bearer " + settings.token},
            json={"name": "Files", "purpose": "Process files"},
        ).json()
        path = f"/v1/bots/{bot['id']}/files"
        data = bytes(range(256)) * 4096
        body = {"name": "sample.bin", "data_base64": base64.b64encode(data).decode()}
        assert client.post(path + "/binary", json=body).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        response = client.post(path + "/binary", json=body)
        assert response.status_code == 201
        file = response.json()
        assert file["content"] is None and "data_base64" not in file
        result = client.get(path + f"/{file['id']}/download")
        assert result.headers["cache-control"] == "no-store"
        assert base64.b64decode(result.json()["data_base64"]) == data
        assert client.post("/v1/bots", content="x" * 512001).status_code == 413
