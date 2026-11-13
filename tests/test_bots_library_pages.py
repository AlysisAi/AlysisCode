"""Real SQL paging/account isolation. Scripted clients are not model evidence."""

import json

import pytest
from bots_task_state import task_state
from test_bots_memory_changes import case as shared_case

from alysis_code.bots.files import FileCursor, FileQuery
from alysis_code.bots.models import BotInput, FileInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import NotFound

case = shared_case


def test_large_library_pages_survive_deleted_cursor_and_preserve_payloads(case):
    store, owner, bot, task = case
    originals = [
        store.save_file(
            owner,
            bot["id"],
            FileInput(name=f"東京-{i:03}.txt", content="private body " + str(i)),
            source_task_id=task["id"] if i % 2 else None,
        )
        for i in range(125)
    ]
    first = store.files_page(owner, bot["id"], FileQuery(limit=19))
    assert first["total_count"] == first["matched_count"] == 125
    assert len(first["items"]) == 19
    assert "private body" not in json.dumps(first)
    cursor = first["next_before"]
    store.delete_item(owner, bot["id"], cursor["id"], kind="files")
    new = store.save_bytes(owner, bot["id"], "new.bin", b"\x00\xff")
    seen = [f["id"] for f in first["items"]]
    while cursor:
        page = store.files_page(owner, bot["id"], FileQuery(before=FileCursor(**cursor), limit=19))
        seen.extend(f["id"] for f in page["items"])
        cursor = page["next_before"]
    assert len(seen) == len(set(seen)) == 125
    assert set(seen) == {f["id"] for f in originals}
    assert new["id"] not in seen
    assert store.files_page(owner, bot["id"], FileQuery())["items"][0]["id"] == new["id"]
    assert store.file_data(owner, bot["id"], originals[0]["id"]) == b"private body 0"
    assert store.library_usage(owner)["file_count"] == 125


def test_unicode_filename_search_task_scope_and_authority(case):
    store, owner, bot, task = case
    for name in ("Straße.csv", "ΑΘΗΝΑ.csv", "東京.csv", "e\u0301té.txt"):
        store.save_bytes(owner, bot["id"], name, b"secret", source_task_id=task["id"])
    store.save_bytes(owner, bot["id"], "other.bin", b"x")
    for query in ("STRASSE", "αθηνα", "東京", "été"):
        found = store.files_page(owner, bot["id"], FileQuery(query=query))
        assert found["matched_count"] == len(found["items"]) == 1
        assert found["next_before"] is None
        assert found["retrieval"] == "filename_unicode_substring"
    assert store.files_page(owner, bot["id"], FileQuery(query="secret"))["items"] == []
    assert (
        store.files_page(owner, bot["id"], FileQuery(source_task_id=task["id"]))["total_count"] == 4
    )
    second = store.save_bot(owner, BotInput(name="Second", purpose="Separate files"))
    with pytest.raises(NotFound):
        store.files_page(owner, second["id"], FileQuery(source_task_id=task["id"]))
    with pytest.raises(NotFound):
        store.files_page("foreign", bot["id"], FileQuery())
    with pytest.raises(NotFound):
        store.file_name_exists("foreign", bot["id"], "東京.csv")


def test_equal_timestamp_order_and_bounded_model_file_index(case):
    store, owner, bot, task = case
    for i in range(42):
        store.save_bytes(owner, bot["id"], f"long-{'名' * 100}-{i}.bin", b"body")
    with store.connect(write=True) as db:
        db.execute("UPDATE files SET created_at=? WHERE bot_id=?", (1000, bot["id"]))
    first = store.files_page(owner, bot["id"], FileQuery(limit=21))
    second = store.files_page(
        owner, bot["id"], FileQuery(before=FileCursor(**first["next_before"]), limit=21)
    )
    assert not set(f["id"] for f in first["items"]) & set(f["id"] for f in second["items"])
    assert second["next_before"] is None
    runtime = BotRuntime(store, owner, lambda _: None)
    context = task_state(runtime._context(task))
    index = json.loads(context.split("Recent Library page", 1)[1].split(": ", 1)[1])
    assert index["total_count"] == 42 and len(index["items"]) == 10
    assert index["next_before"]
    assert "content" not in index["items"][0]


def test_http_large_library_requires_paging_and_reports_task_file_counts(tmp_path):
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Files", "purpose": "Browse"}).json()
        path = f"/v1/bots/{bot['id']}/files"
        for i in range(101):
            response = client.post(path, json={"name": f"file-{i}.txt", "content": "hidden"})
            assert response.status_code == 201
        assert client.get(path).status_code == 409
        page = client.post(path + "/page", json={"limit": 20}).json()
        assert page["total_count"] == 101 and len(page["items"]) == 20
        assert "hidden" not in json.dumps(page)
        assert client.post(path + "/page", json={"limit": 101}).status_code == 422
        assert client.post(path + "/page", json={"before": {"id": "x"}}).status_code == 422
        client.headers.pop("Authorization")
        assert client.post(path + "/page", json={}).status_code == 401
