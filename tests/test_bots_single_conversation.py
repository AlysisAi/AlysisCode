"""Single bot timeline, including real persisted records from older chat layouts."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import NotFound, Store, encode


def legacy_turn(store, bot, *, status="completed", request="Earlier decision — Ελληνικά 日本語"):
    """Insert an actual old-layout chat/task/message, not a mocked conversation read."""
    chat, task, message = (uuid4().hex for _ in range(3))
    with store.connect(write=True) as db:
        db.execute("INSERT INTO chats VALUES(?,?,?,?)", (chat, bot["id"], "Legacy topic", 2000))
        db.execute(
            """INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (task, chat, bot["id"], "alice", task, request, status, encode(bot), 2000, 2000),
        )
        db.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?)", (message, chat, task, "user", request, 2000)
        )
        store._event(db, task, status, {})
    return chat, task, message


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "single.db")
    bot = store.save_bot("alice", BotInput(name="Atlas", purpose="One ongoing conversation"))
    chat = store.create_chat("alice", bot["id"], "Original")
    # Give the earliest legacy chat a deterministic earlier creation time.
    with store.connect(write=True) as db:
        db.execute("UPDATE chats SET created_at=1000 WHERE id=?", (chat["id"],))
    return store, bot, chat


def test_concurrent_creation_returns_one_conversation_and_preserves_it_on_restart(setup):
    store, bot, original = setup
    with ThreadPoolExecutor(max_workers=6) as pool:
        chats = list(pool.map(lambda n: store.create_chat("alice", bot["id"], str(n)), range(12)))
    assert {chat["id"] for chat in chats} == {original["id"]}
    assert len(Store(store.path).list_chats("alice", bot["id"])) == 1
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM chats").fetchone()[0] == 1


def test_legacy_messages_sources_and_request_identity_survive_without_rewriting(setup):
    store, bot, original = setup
    legacy, task, message = legacy_turn(store, bot)
    with store.connect() as db:
        before = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in ("chats", "tasks", "messages")
        }
    assert store.create_chat("alice", bot["id"], "No side chat")["id"] == original["id"]
    assert len(store.list_chats("alice", bot["id"])) == 1
    canonical = store.conversation("alice", original["id"])
    assert store.conversation("alice", legacy) == canonical
    assert canonical["id"] == original["id"]
    assert canonical["messages"][0]["id"] == message
    assert store.read_message("alice", bot["id"], message, 0, 1000)["task_id"] == task
    assert store.task("alice", task)["chat_id"] == legacy
    assert (
        store.enqueue(
            "alice",
            original["id"],
            TaskInput(
                message="Earlier decision — Ελληνικά 日本語",
                request_id=task,
            ),
        )["id"]
        == task
    )
    with store.connect() as db:
        assert before == {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in before
        }
    new = store.enqueue("alice", legacy, TaskInput(message="Continue", request_id=uuid4().hex))
    assert new["chat_id"] == original["id"]
    context = BotRuntime(store, "alice", lambda _: None)._context(new)
    assert "Earlier decision — Ελληνικά 日本語" in encode(context)
    assert context[-1]["content"] == "Continue"


def test_read_receipt_preserves_unread_legacy_work_and_cannot_cross_bots(setup):
    store, bot, original = setup
    legacy, _, _ = legacy_turn(store, bot)
    older = store.conversation("alice", legacy)["activity_seq"]
    task = store.enqueue("alice", original["id"], TaskInput(message="New", request_id=uuid4().hex))
    store.finish("alice", task["id"], "completed", content="New result")
    newest = store.conversation("alice", original["id"])["activity_seq"]
    # A pre-upgrade receipt for only the newer chat must not hide unread legacy work.
    with store.connect(write=True) as db:
        db.execute("INSERT INTO chat_read_cursors VALUES(?,?)", (original["id"], newest))
    assert store.conversation("alice", original["id"])["unread"]
    store.mark_chat_read("alice", original["id"], older)
    assert not store.conversation("alice", legacy)["unread"]
    store.mark_chat_read("alice", legacy, newest)
    assert store.mark_chat_read("alice", original["id"], older)["read_seq"] == newest
    other = store.save_bot("alice", BotInput(name="Other", purpose="Separate"))
    other_chat = store.create_chat("alice", other["id"], "Other")
    assert store.conversation("alice", other_chat["id"])["messages"] == []
    with pytest.raises(NotFound):
        store.mark_chat_read("alice", other_chat["id"], newest)
    with pytest.raises(NotFound):
        store.conversation("bob", legacy)


def test_legacy_active_work_accepts_new_send_but_serializes_until_recovery(setup):
    store, bot, original = setup
    _, task, _ = legacy_turn(store, bot, status="running")
    new = store.enqueue(
        "alice", original["id"], TaskInput(message="Continue", request_id=uuid4().hex)
    )
    assert store.claim("alice") is None
    assert store.recover("alice") == 1
    assert store.claim("alice")["id"] == new["id"]
    assert store.task("alice", task)["status"] == "interrupted"
    assert "interrupted" in encode(BotRuntime(store, "alice", lambda _: None)._context(new))


def test_scheduled_results_and_file_sources_use_the_same_conversation(setup):
    store, bot, original = setup
    store.save_schedule(
        "alice",
        bot["id"],
        ScheduleInput(
            name="Review",
            prompt="Review our decisions",
            timing={"kind": "interval", "every_minutes": 5},
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules("alice", max_calls=5, now=1300) == 1
    task = store.claim("alice")
    assert task["chat_id"] == original["id"]
    file = store.save_file(
        "alice",
        bot["id"],
        FileInput(name="review.md", content="Saved\n"),
        source_task_id=task["id"],
    )
    store.finish("alice", task["id"], "completed", content="Review ready")
    assert store.conversation("alice", original["id"])["messages"][-1]["content"] == "Review ready"
    assert store.file("alice", bot["id"], file["id"])["source_task_id"] == task["id"]
    assert store.enqueue_due_schedules("alice", max_calls=5, now=1600) == 1
    assert store.claim("alice")["chat_id"] == original["id"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM chats").fetchone()[0] == 1
