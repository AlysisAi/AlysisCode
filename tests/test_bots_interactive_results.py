"""A selected HTML result is bound to its owner's chat, exact bytes and saved view."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import (
    INTERACTIVE_STATE_BYTES,
    BotInput,
    ConversationPageQuery,
    FileInput,
    InteractiveStateWrite,
    RegenerateInput,
    TaskInput,
)
from alysis_code.bots.queued_messages import QueuedMessageChange, QueuedMessages
from alysis_code.bots.store import Conflict, NotFound, Store


def setup_result(store, owner="person"):
    owl = store.save_bot(owner, BotInput(name="Équipe"))["id"]
    chat = store.create_chat(owner, owl, "Réunion")["id"]
    file = store.save_file(owner, owl, FileInput(name="Suivi.html", content="<h1>Réunion</h1>"))
    return owl, chat, file


def state_update(file, expected, data):
    return InteractiveStateWrite(
        revision=file["revision"], sha256=file["sha256"], expected_version=expected, data=data
    )


def selection(file, version=0):
    region = {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1}
    return {
        "file_id": file["id"],
        "revision": file["revision"],
        "sha256": file["sha256"],
        "state_version": version,
        "region": region,
        "element": {"region": region, "label": "Total du trimestre", "path": "main > h1"},
        "viewport": {"width": 960, "height": 600},
    }


def request(file, version=0, **overrides):
    return TaskInput.model_validate(
        {
            "request_id": uuid4().hex,
            "message": "Agrandis ce titre.",
            "file_ids": [file["id"]],
            "interactive_target": selection(file, version),
            **overrides,
        }
    )


def test_state_cas_restart_history_and_deletion(tmp_path):
    first = Store(tmp_path / "state.db")
    second = Store(first.path)
    owl, _, file = setup_result(first)
    assert first.interactive_state("person", owl, file["id"])["data"] is None

    def write(replica):
        try:
            return replica.interactive_state(
                "person", owl, file["id"], state_update(file, 0, {"note": "  東京  "})
            )["version"]
        except Conflict:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(write, [first, second])) == {1, "stale"}
    next_file = second.save_bytes(
        "person", owl, file["name"], b"<h1>Volume</h1>", previous_file_id=file["id"]
    )
    restarted = Store(first.path)
    old = restarted.interactive_state("person", owl, file["id"])
    current = restarted.interactive_state("person", owl, next_file["id"])
    assert old["is_latest"] is False and current["is_latest"] is True
    assert current["series_id"] == old["series_id"] == file["id"]
    assert current["data"] == old["data"] == {"note": "  東京  "}
    assert current["version"] == old["version"] == 1
    with pytest.raises(Conflict):
        restarted.interactive_state("person", owl, file["id"], state_update(file, 1, []))
    assert (
        restarted.interactive_state("person", owl, next_file["id"], state_update(next_file, 1, []))[
            "version"
        ]
        == 2
    )
    assert first.interactive_state("person", owl, file["id"])["data"] == []
    first.delete_item("person", owl, next_file["id"], kind="files")
    with first.connect() as db:
        assert db.execute("SELECT count(*) FROM interactive_result_states").fetchone()[0] == 0
    new_series = first.save_bytes("person", owl, file["name"], b"<h1>Autre</h1>")
    assert new_series["series_id"] != file["series_id"]
    assert first.interactive_state("person", owl, new_series["id"])["version"] == 0


def test_existing_library_file_and_chat_survive_state_schema_upgrade(tmp_path):
    old = Store(tmp_path / "upgrade.db")
    owl, chat, file = setup_result(old)
    task = old.enqueue("person", chat, TaskInput(message="Avant", request_id=uuid4().hex))
    with old.connect(write=True) as db:
        db.execute("DROP TABLE interactive_result_states")
        db.execute("PRAGMA user_version=56")
    upgraded = Store(old.path)
    assert upgraded.task("person", task["id"])["message"] == "Avant"
    assert upgraded.file_data("person", owl, file["id"]) == "<h1>Réunion</h1>".encode()
    assert upgraded.interactive_state("person", owl, file["id"])["version"] == 0
    assert (
        upgraded.interactive_state("person", owl, file["id"], state_update(file, 0, []))["version"]
        == 1
    )


def test_task_publish_scope_exact_retry_and_old_requests(tmp_path):
    store = Store(tmp_path / "tasks.db")
    owl, chat, file = setup_result(store)
    saved = store.interactive_state(
        "person", owl, file["id"], state_update(file, 0, {"filter": "Écarts"})
    )
    msg = request(file, saved["version"])
    task = store.enqueue("person", chat, msg)
    restarted = Store(store.path)
    running = restarted.claim("person")
    assert running["id"] == task["id"] and running["message"] == msg.message
    context = restarted.interactive_revision_context("person", running["id"])
    assert context == {
        **selection(file, 1),
        "series_id": file["id"],
        "name": file["name"],
        "state": {"filter": "Écarts"},
    }
    assert "state" not in running["config"]["interactive_target"]
    with pytest.raises(NotFound):
        restarted.interactive_revision_context("someone_else", task["id"])
    for name, base in [("Unrelated.html", file["id"]), (file["name"], None)]:
        with pytest.raises(Conflict):
            restarted.save_bytes(
                "person", owl, name, b"<h1>A</h1>", source_task_id=task["id"], previous_file_id=base
            )
    revised = restarted.save_bytes(
        "person",
        owl,
        file["name"],
        b"<h1>Volume</h1>",
        source_task_id=task["id"],
        previous_file_id=file["id"],
    )
    assert revised["revision"] == 2 and revised["series_id"] == file["id"]
    assert restarted.file_data("person", owl, file["id"]) == "<h1>Réunion</h1>".encode()
    assert restarted.interactive_state("person", owl, revised["id"])["source_chat_id"] == chat
    with pytest.raises(Conflict):
        restarted.save_file(
            "person",
            owl,
            FileInput(name=file["name"], content="again", previous_file_id=file["id"]),
            source_task_id=task["id"],
        )
    restarted.finish("person", task["id"], "completed")
    # A response lost in transit still identifies the original task after revision/state advance.
    restarted.interactive_state("person", owl, revised["id"], state_update(revised, 1, None))
    assert restarted.enqueue("person", chat, msg)["id"] == task["id"]
    for change in [
        {"interactive_target": None},
        {"interactive_target": selection(file, 2)},
        {"interactive_target": {**selection(file, 1), "element": None}},
        {"message": "Une autre demande."},
    ]:
        with pytest.raises(Conflict, match="request ID"):
            restarted.enqueue("person", chat, request(file, 1, request_id=msg.request_id, **change))
    with pytest.raises(Conflict):
        restarted.regenerate("person", task["id"], RegenerateInput(request_id=uuid4().hex))
    ordinary = TaskInput(message="", request_id=uuid4().hex, file_ids=[file["id"]])
    original = restarted.enqueue("person", chat, ordinary)
    assert restarted.enqueue("person", chat, ordinary)["id"] == original["id"]
    with pytest.raises(NotFound):
        restarted.interactive_revision_context("person", original["id"])


def test_changes_at_either_end_of_worker_execution_withhold_stale_writes(tmp_path):
    store = Store(tmp_path / "race.db")
    owl, chat, file = setup_result(store)
    pending = store.enqueue("person", chat, request(file))
    store.interactive_state("person", owl, file["id"], state_update(file, 0, {"tab": 1}))
    assert store.claim("person")["id"] == pending["id"]
    with pytest.raises(Conflict, match="state changed"):
        store.interactive_revision_context("person", pending["id"])
    store.finish("person", pending["id"], "completed")
    fresh = store.enqueue("person", chat, request(file, 1))
    assert store.claim("person")["id"] == fresh["id"]
    assert store.interactive_revision_context("person", fresh["id"])["state"] == {"tab": 1}
    store.interactive_state("person", owl, file["id"], state_update(file, 1, {"tab": 2}))
    with pytest.raises(Conflict, match="state changed"):
        store.save_file(
            "person",
            owl,
            FileInput(name=file["name"], content="new", previous_file_id=file["id"]),
            source_task_id=fresh["id"],
        )
    assert store.file("person", owl, file["id"])["version_count"] == 1


def test_regenerate_edit_and_steer_preserve_selection_boundary(tmp_path):
    store = Store(tmp_path / "changes.db")
    owl, chat, file = setup_result(store)
    changes = QueuedMessages(store, "person")
    first = store.enqueue("person", chat, TaskInput(message="First", request_id=uuid4().hex))
    store.claim("person")
    next_task = store.enqueue("person", chat, request(file))
    steer = QueuedMessageChange(action="steer", expected_revision=0)
    assert changes.change(next_task["id"], steer)["reason"] == "not_steerable"
    with pytest.raises(Conflict):
        changes.change(
            next_task["id"], QueuedMessageChange(action="edit", message="", expected_revision=0)
        )
    store.finish("person", first["id"], "completed")
    store.claim("person")
    store.finish("person", next_task["id"], "completed", content="Précisez la taille.")
    again = store.regenerate("person", next_task["id"], RegenerateInput(request_id=uuid4().hex))
    assert store.interactive_revision_context("person", again["id"])["file_id"] == file["id"]
    assert store.claim("person")["id"] == again["id"]
    ordinary = store.enqueue("person", chat, TaskInput(message="Other", request_id=uuid4().hex))
    assert changes.change(ordinary["id"], steer)["reason"] == "not_steerable"


def test_selected_part_is_a_scoped_historical_user_message_receipt(tmp_path):
    store = Store(tmp_path / "receipt.db")
    owl, chat, file = setup_result(store)
    _, other_chat, _ = setup_result(store)
    store.enqueue("person", chat, TaskInput(message="Bonjour", request_id=uuid4().hex))
    previous = store.conversation_page("person", chat, ConversationPageQuery(limit=1))
    selected = store.enqueue("person", chat, request(file))
    receipt = {"file_id": file["id"], "revision": 1, "label": "Total du trimestre"}
    delta = store.conversation_page(
        "person",
        chat,
        ConversationPageQuery(
            limit=1, after=previous["messages"][-1]["id"], version=previous["version"]
        ),
    )
    assert delta["continues"] and [m["interactive_selection"] for m in delta["messages"]] == [
        receipt
    ]
    additional = store.enqueue(
        "person",
        chat,
        request(
            file,
            interactive_target={
                **selection(file),
                "element": {**selection(file)["element"], "label": "<strong>Titre</strong>"},
            },
        ),
    )
    areas = store.enqueue(
        "person", chat, request(file, interactive_target={**selection(file), "element": None})
    )
    store.delete_item("person", owl, file["id"], kind="files")
    restarted = Store(store.path)
    history = restarted.conversation("person", chat)
    user = {m["task_id"]: m for m in history["messages"]}
    assert user[selected["id"]]["interactive_selection"] == receipt
    assert user[selected["id"]]["files"][0]["available"] is False
    assert user[additional["id"]]["interactive_selection"]["label"] == ""
    assert user[areas["id"]]["interactive_selection"]["label"] == ""
    assert "interactive_selection" not in history["messages"][0]
    assert all("interactive_target" not in t["config"] for t in history["tasks"])
    older = restarted.conversation_page(
        "person", chat, ConversationPageQuery(before=user[additional["id"]]["id"], limit=1)
    )
    assert [m["interactive_selection"] for m in older["messages"]] == [receipt]
    assert all("interactive_target" not in t["config"] for t in older["tasks"])
    assert (
        restarted.conversation_page("person", other_chat, ConversationPageQuery())["messages"] == []
    )
    with pytest.raises(NotFound):
        restarted.conversation_page("another owner", chat, ConversationPageQuery())
    with pytest.raises(NotFound):
        restarted.conversation_page(
            "person", other_chat, ConversationPageQuery(before=user[selected["id"]]["id"])
        )


def test_several_marks_remain_bound_to_one_original_and_keep_their_notes(tmp_path):
    store = Store(tmp_path / "marks.db")
    _, chat, file = setup_result(store)
    target = selection(file)
    first = {
        "element": target["element"],
        "viewport": target["viewport"],
        "note": "Agrandis ce titre.",
    }
    second = {
        **first,
        "element": {**target["element"], "path": "#cout", "label": "Coût"},
        "note": "Change l’unité.",
    }
    marks = [first, second]
    msg = request(file, interactive_target={**target, "marks": marks})
    saved = store.enqueue("person", chat, msg)
    reopened = Store(store.path)
    assert reopened.interactive_revision_context("person", saved["id"])["marks"] == marks
    assert reopened.enqueue("person", chat, msg)["id"] == saved["id"]
    history = reopened.conversation_page("person", chat, ConversationPageQuery(limit=10))
    assert history["messages"][0]["interactive_selection"]["labels"] == [
        "Total du trimestre",
        "Coût",
    ]
    for invalid in [[second, first], [first] * 9, [first, {**second, "note": "  "}]]:
        with pytest.raises(ValidationError):
            request(file, interactive_target={**target, "marks": invalid})


def test_api_authorization_state_limits_original_chat_and_input_validation(tmp_path, monkeypatch):
    settings = Settings(
        tmp_path / "web.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings)
    # Exercise real submission HTTP without starting any model workers.
    monkeypatch.setattr(type(app.state.runtime), "is_running", property(lambda self: True))
    client = TestClient(app)
    store, owner = app.state.store, settings.owner
    owl, chat, file = setup_result(store, owner)
    path = f"/v1/bots/{owl}/files/{file['id']}/interactive-state"
    assert client.get(path).status_code == 401
    client.headers["Authorization"] = "Bearer " + settings.token
    assert "owl_interactive_results" in client.get("/v1/status").json()["capabilities"]
    assert client.get(path).json() == {
        "file_id": file["id"],
        "name": file["name"],
        "series_id": file["id"],
        "revision": 1,
        "sha256": file["sha256"],
        "source_chat_id": None,
        "is_latest": True,
        "data": None,
        "version": 0,
        "max_selections": 8,
    }
    other = store.save_bot(owner, BotInput(name="Second"))["id"]
    foreign, _, foreign_file = setup_result(store, "someone_else")
    for bot_id, file_id in [
        (other, file["id"]),
        (owl, foreign_file["id"]),
        (foreign, foreign_file["id"]),
    ]:
        url = f"/v1/bots/{bot_id}/files/{file_id}/interactive-state"
        assert (
            client.get(url).status_code
            == client.patch(url, json=state_update(file, 0, {}).model_dump()).status_code
            == 404
        )
    text_file = store.save_file(owner, owl, FileInput(name="Notes.txt", content="hello"))
    assert client.get(path.replace(file["id"], text_file["id"])).status_code == 409
    payload = state_update(file, 0, "x" * (INTERACTIVE_STATE_BYTES - 2)).model_dump()
    for wrong in [{"sha256": "f" * 64}, {"revision": 2}, {"expected_version": 1}]:
        assert client.patch(path, json={**payload, **wrong, "data": None}).status_code == 409
    assert client.patch(path, json=payload).json()["version"] == 1
    assert client.patch(path, json=payload).status_code == 409
    assert client.patch(path, json={**payload, "data": payload["data"] + "x"}).status_code == 422
    assert (
        client.patch(path, json={**payload, "data": payload["data"] + "x" * 8192}).status_code
        == 413
    )
    with pytest.raises(ValidationError):
        state_update(file, 1, float("nan"))

    endpoint = f"/v1/chats/{chat}/tasks"
    msg = request(file, 1).model_dump(exclude_none=True)
    for change in [
        {"message": "  "},
        {"file_ids": []},
        {"interactive_target": {**selection(file, 1), "source_chat_id": chat}},
        {
            "interactive_target": {
                **selection(file, 1),
                "region": {"x": 0.7, "y": 0, "width": 0.4, "height": 1},
            }
        },
        {"interactive_target": {**selection(file, 1), "viewport": {"width": 50000, "height": 1}}},
        {
            "interactive_target": {
                **selection(file, 1),
                "element": {**selection(file)["element"], "label": "a" * 241},
            }
        },
    ]:
        assert client.post(endpoint, json={**msg, **change}).status_code == 422
    assert (
        client.post(endpoint, json={**msg, "interactive_target": selection(file, 0)}).status_code
        == 409
    )
    accepted = client.post(endpoint, json=msg)
    assert accepted.status_code == 202
    assert "interactive_target" not in accepted.json()["config"]
    assert (
        "interactive_target"
        not in client.get(f"/v1/tasks/{accepted.json()['id']}").json()["config"]
    )
    task = store.claim(owner)
    revised = store.save_bytes(
        owner,
        owl,
        file["name"],
        b"<h1>New</h1>",
        previous_file_id=file["id"],
        source_task_id=task["id"],
    )
    store.finish(owner, task["id"], "completed")
    assert client.post(endpoint, json=msg).json()["id"] == task["id"]
    alias = uuid4().hex
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO chats(id,bot_id,title,created_at) VALUES(?,?,?,?)",
            (alias, owl, "Older conversation", 0.0),
        )
    new_msg = request(revised, 1).model_dump(exclude_none=True)
    assert client.post(f"/v1/chats/{alias}/tasks", json=new_msg).status_code == 409
    assert client.post(endpoint, json=new_msg).status_code == 202
