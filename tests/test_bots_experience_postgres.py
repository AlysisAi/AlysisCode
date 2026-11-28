"""Real PostgreSQL checks for competing Owl controllers. Requires a disposable PostgreSQL 18 database."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from alysis_code.bots import answer_sections
from alysis_code.bots.managed_cloud import AccountAuthority
from alysis_code.bots.models import BotInput, NoteInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.publications import (
    Publication,
    Publications,
    PublicationVersion,
    install_reader,
)
from alysis_code.bots.rooms import RoomCreate, RoomMessage, Rooms
from alysis_code.bots.sharing_service import sharing_gateway
from alysis_code.bots.store import Conflict, NotFound


@pytest.fixture
def controllers(tmp_path):
    dsn = os.environ.get("ALYSIS_BOTS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set ALYSIS_BOTS_TEST_POSTGRES_DSN to use disposable PostgreSQL 18")
    objects = LocalObjectStorage(tmp_path / "objects")
    one = PostgresStore(dsn, objects=objects, state_directory=tmp_path / "one")
    two = PostgresStore(dsn, objects=objects, state_directory=tmp_path / "two")
    try:
        with one.connect() as a, two.connect() as b:
            assert (
                a.execute("SELECT pg_backend_pid()").fetchone()[0]
                != b.execute("SELECT pg_backend_pid()").fetchone()[0]
            )
            assert int(a.execute("SHOW server_version_num").fetchone()[0]) >= 180000
        yield one, two, "account:" + str(uuid4())
    finally:
        one.close()
        two.close()


@pytest.mark.parametrize("winner", ["save", "revoke"])
def test_concurrent_save_and_revoke_serialize_without_regrant(controllers, winner, monkeypatch):
    first, second, owner = controllers
    reader, other = str(uuid4()), str(uuid4())
    accounts = {"one@example.test": reader, "two@example.test": other}

    def publications(store, who):
        return Publications(
            store, who, lambda emails: [accounts[e] for e in emails], "https://reader.example.test"
        )

    a, b = publications(first, owner), publications(second, owner)
    old_reader = publications(first, "account:" + reader)
    new_reader = publications(second, "account:" + other)
    original = Publication(
        kind="conversation",
        source=uuid4().hex,
        version=0,
        revocation_epoch=0,
        title="Invented brief",
        content="Original",
        recipients=["one@example.test"],
    )
    saved = a.save(original)
    identity = saved["id"]
    update = original.model_copy(
        update={
            "version": 1,
            "expected_id": identity,
            "content": "Revised",
            "recipients": ["two@example.test"],
        }
    )
    assert old_reader.receive(identity)["content"] == "Original"

    # Pause the first controller after its genuine transaction takes the publication lock.
    # Wait for PostgreSQL itself to report the second controller's blocked writer before release.
    lock_acquired, release = Event(), Event()
    real_lock = a._lock_owner

    def hold(db):
        real_lock(db)
        lock_acquired.set()
        assert release.wait(5)

    monkeypatch.setattr(a, "_lock_owner", hold)

    def operate(service, verb):
        try:
            return (
                service.save(update)
                if verb == "save"
                else service.revoke(identity, PublicationVersion(version=1))
            )
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_write = pool.submit(operate, a, winner)
        try:
            assert lock_acquired.wait(3)
            second_write = pool.submit(operate, b, "revoke" if winner == "save" else "save")
            until = time.monotonic() + 3
            while time.monotonic() < until:
                with first.connect() as db:
                    contended = db.execute(
                        "SELECT 1 FROM pg_locks WHERE database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
                        "AND locktype='advisory' AND NOT granted LIMIT 1"
                    ).fetchone()
                if contended:
                    break
                time.sleep(0.01)
            assert contended, "PostgreSQL did not observe the overlapping writes"
        finally:
            release.set()
        result = first_write.result(timeout=5)
        assert second_write.result(timeout=5) == "conflict"
    if winner == "save":
        assert result["version"] == 2 and new_reader.receive(identity)["content"] == "Revised"
        with pytest.raises(NotFound):
            old_reader.receive(identity)
        assert b.revoke(identity, PublicationVersion(version=2))["revoked"]
    else:
        assert result["revoked"]
    for reader_service in (old_reader, new_reader):
        assert reader_service.inbox()["received"] == []
        with pytest.raises(NotFound):
            reader_service.receive(identity)
    assert b.browse()["sent"] == []
    # A delayed original create, or delayed update for the revoked identity, cannot grant access.
    for stale in (original, update):
        with pytest.raises(Conflict):
            b.save(stale)
    epoch = b.browse()["revocation_epoch"]
    assert epoch == 1
    shared_again = b.save(original.model_copy(update={"revocation_epoch": epoch}))
    assert (
        shared_again["id"] != identity
        and old_reader.receive(shared_again["id"])["content"] == "Original"
    )
    with pytest.raises(Conflict):
        a.save(update)


def test_authenticated_gateways_and_public_reader_observe_the_other_controllers_revocation(
    controllers,
):
    first, second, _ = controllers
    tokens = {"slk_" + uuid4().hex: str(uuid4()) for _ in range(3)}
    sender, reader, stranger = tokens

    def gateway(store):
        authority = AccountAuthority(
            store,
            b"x" * 32,
            lookup=lambda key: {"account_id": tokens[key], "status": "available", "plan": "alpha"},
        )
        authority.publication_recipients = lambda owner, emails, *, credential: (
            [tokens[reader]]
            if credential == sender and emails == ["owl-reader@example.test"]
            else []
        )
        app = sharing_gateway(authority, "https://reader.example.test")
        install_reader(app, store)
        return TestClient(app)

    def auth(token):
        return {"Authorization": "Bearer " + token}

    path = "/v1/sharing"
    with gateway(first) as a, gateway(second) as b:
        assert b.get(path).status_code == 401
        base = {
            "title": "Bus count",
            "content": "Three invented stops",
            "source": uuid4().hex,
            "version": 0,
            "revocation_epoch": 0,
        }
        conversation = {**base, "kind": "conversation", "recipients": ["owl-reader@example.test"]}
        response = a.post(path, json=conversation, headers=auth(sender))
        assert response.status_code == 200, response.text
        shared = response.json()["id"]
        assert (
            b.get(path + "/" + shared, headers=auth(reader)).json()["content"]
            == conversation["content"]
        )
        assert b.get(path + "/" + shared, headers=auth(stranger)).status_code == 404
        assert b.get("/published/" + shared).status_code == 404
        assert b.post(
            path + "/" + shared + "/revoke", headers=auth(sender), json={"version": 1}
        ).is_success
        assert a.get(path + "/" + shared, headers=auth(reader)).status_code == 404
        assert a.post(path, headers=auth(sender), json=conversation).status_code == 409
        dashboard = {
            **base,
            "kind": "dashboard",
            "content": "<h1>Invented dashboard</h1>",
            "source": uuid4().hex,
            "revocation_epoch": a.get(path, headers=auth(sender)).json()["revocation_epoch"],
        }
        published = a.post(path, json=dashboard, headers=auth(sender)).json()
        assert published["url"] == "https://reader.example.test/published/" + published["id"]
        page = "/published/" + published["id"]
        assert "Invented dashboard" in b.get(page).text
        assert a.post(
            path + "/" + published["id"] + "/revoke", json={"version": 1}, headers=auth(sender)
        ).is_success
        assert b.get(page).status_code == 404


def test_two_controllers_retry_one_separate_chat_and_reset_a_corrected_section(controllers):
    first, second, owner = controllers
    owl = first.save_bot(owner, BotInput(name="Archivist"))
    default = first.create_chat(owner, owl["id"], "Default")
    gate = Barrier(2)

    def create(store):
        gate.wait(3)
        return store.create_chat(
            owner, owl["id"], "Field notes", thread_request_id="new_field_notes"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = list(pool.map(create, (first, second)))
    assert one["id"] == two["id"] != default["id"]
    assert len(second.list_chats(owner, owl["id"])) == 2
    data = TaskInput(message="Summarize these invented figures.", request_id="same_roomless_task")
    gate = Barrier(2)

    def enqueue(store):
        gate.wait(3)
        return store.enqueue(owner, one["id"], data)

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = list(pool.map(enqueue, (first, second)))
    assert left["id"] == right["id"]
    task = first.claim(owner)
    assert task["id"] == left["id"]
    assert second.conversation(owner, default["id"])["tasks"] == []
    assert [t["id"] for t in second.conversation(owner, two["id"])["tasks"]] == [task["id"]]
    context, _, _ = answer_sections.context_at_start(first, task)
    first.event(
        owner,
        task["id"],
        "reply_section",
        {"number": 1, "text": "Checked opening", "context": context},
    )
    assert answer_sections.context_at_start(second, task) == (context, False, ["Checked opening"])
    second.save_note(
        owner, owl["id"], NoteInput(title="Updated capacity", content="Use the revised count.")
    )
    new_context, changed, parts = answer_sections.context_at_start(first, task)
    assert changed and not parts and new_context != context
    assert not answer_sections.context_at_start(second, task)[1]
    assert (
        len(
            [
                e
                for e in second.task(owner, task["id"])["events"]
                if e["kind"] == "reply_sections_reset"
            ]
        )
        == 1
    )


def test_room_file_on_postgres_reaches_only_its_selected_member(controllers):
    first, second, owner = controllers
    owls = [
        first.save_bot(owner, BotInput(name=name))["id"]
        for name in ("Scout", "Planner", "Outsider")
    ]
    a, b, outside = owls
    rooms = Rooms(first, owner)
    room = rooms.create(RoomCreate(name="Dispatch", bot_ids=[a, b]))
    content = b"session,overflow\nAmber,5\n"
    source = first.save_bytes(owner, a, "invented.csv", content)
    foreign = second.save_bytes(owner, outside, "elsewhere.csv", b"synthetic")
    payload = RoomMessage(
        message="Review this sheet.",
        request_id="pg_room_file",
        expertise=True,
        files=[{"bot_id": a, "file_id": source["id"]}],
    )
    sent = rooms.send(room["id"], payload)["tasks"][0]
    mirror = Rooms(second, owner)
    assert mirror.send(room["id"], payload)["tasks"][0]["id"] == sent["id"]
    assert not second.list_files(owner, b)
    assert first.claim(owner)["id"] == sent["id"]
    mirror.finish_routing(sent["id"], b)
    assert first.claim(owner)["bot_id"] == b
    copied = first.list_files(owner, b)
    assert len(copied) == 1 and copied[0]["id"] != source["id"]
    assert b"".join(second.file_chunks(owner, b, copied[0]["id"])) == content
    assert [f["id"] for f in second.list_files(owner, a)] == [source["id"]]
    assert [f["id"] for f in first.list_files(owner, outside)] == [foreign["id"]]
    with pytest.raises(Conflict):
        mirror.send(
            room["id"],
            RoomMessage(
                message="Different source",
                request_id="pg_room_outside",
                files=[{"bot_id": outside, "file_id": foreign["id"]}],
            ),
        )
    with pytest.raises(NotFound):
        Rooms(second, "account:" + str(uuid4())).send(room["id"], payload)
