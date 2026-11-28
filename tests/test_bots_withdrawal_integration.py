"""An Owl can withdraw its own result from a reply without losing the person's file."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
import test_bots_owl_move as move_protocol

from alysis_code.bots.delivery_series import previous_delivery
from alysis_code.bots.files import FileQuery
from alysis_code.bots.interactive_revision import InteractiveRevision
from alysis_code.bots.models import (
    AssignmentAccess,
    AssignmentInput,
    BotInput,
    ConversationPageQuery,
    TaskInput,
)
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.owl_move import MoveBegin, MoveExport, OwlMoves, export_page
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.rooms import RoomCreate, RoomMessage, Rooms
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound, Store
from alysis_code.bots.task_events import read as read_events


@pytest.fixture(params=["sqlite", "postgres"])
def places(tmp_path, request):
    owner = "account:" + str(uuid4())
    if request.param == "sqlite":
        path = tmp_path / "owls.sqlite"
        yield Store(path), Store(path), owner
        return
    dsn = os.environ.get("ALYSIS_BOTS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set ALYSIS_BOTS_TEST_POSTGRES_DSN to use disposable PostgreSQL")
    objects = LocalObjectStorage(tmp_path / "objects")
    first, second = (
        PostgresStore(dsn, objects=objects, state_directory=tmp_path / name)
        for name in ("first", "second")
    )
    try:
        with first.connect() as a, second.connect() as b:
            assert (
                a.execute("SELECT pg_backend_pid()").fetchone()[0]
                != b.execute("SELECT pg_backend_pid()").fetchone()[0]
            )
        yield first, second, owner
    finally:
        first.close()
        second.close()


def start(store, owner, chat, message, *, files=()):
    queued = store.enqueue(
        owner, chat, TaskInput(message=message, request_id=uuid4().hex, file_ids=list(files))
    )
    task = store.claim(owner)
    assert task["id"] == queued["id"]
    return task


def turn_files(store, owner, owl, task):
    page = store.files_page(owner, owl, FileQuery(source_task_id=task, include_versions=True))
    assert page["total_count"] == page["matched_count"] == len(page["items"])
    return {f["id"] for f in page["items"]}


def assert_count(store, owner, chat, task, count):
    for conversation in (
        store.conversation(owner, chat),
        store.conversation_page(owner, chat, ConversationPageQuery()),
    ):
        summary = next(t for t in conversation["tasks"] if t["id"] == task)
        assert summary["file_count"] == count


def test_withdrawal_is_scoped_idempotent_and_replays_only_the_exact_revision(places):
    first, second, owner = places
    owl = first.save_bot(owner, BotInput(name="Editor"))["id"]
    chat = first.create_chat(owner, owl, "Drafts")["id"]
    source = first.save_bytes(owner, owl, "Source.txt", b"Granted source")
    earlier = start(first, owner, chat, "Write the first edition.")
    original = first.save_bytes(
        owner, owl, "Report.txt", b"Edition one", source_task_id=earlier["id"]
    )
    first.finish(owner, earlier["id"], "completed", content="First edition")
    active = start(first, owner, chat, "Revise the report.", files=[source["id"]])
    task = active["id"]
    rejected = first.save_bytes(
        owner, owl, "Report.txt", b"Extra", previous_file_id=original["id"], source_task_id=task
    )
    kept = first.save_bytes(owner, owl, "Reading.txt", b"Final", source_task_id=task)
    cursor = read_events(second, owner, task, 0, 100)["after"]

    with pytest.raises(NotFound):
        second.withdraw_task_result("account:" + str(uuid4()), task, rejected["id"])
    for forbidden in (original, source):
        with pytest.raises((Conflict, NotFound)):
            second.withdraw_task_result(owner, task, forbidden["id"])

    gate = Barrier(2)

    def withdraw(store):
        gate.wait(5)
        return store.withdraw_task_result(owner, task, rejected["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(withdraw, (first, second)))
    assert all(r["withdrawn"] and r["file_id"] == rejected["id"] for r in receipts)
    events = read_events(second, owner, task, cursor, 100)["events"]
    withdrawals = [e for e in events if e["kind"] == "result_withdrawn"]
    (event,) = withdrawals
    assert event["task_id"] == task
    assert event["data"] == {
        "file_id": rejected["id"],
        "series_id": original["series_id"],
        "revision": 2,
    }
    assert read_events(first, owner, task, event["seq"], 100)["events"] == []

    first.finish(owner, task, "completed", content="Only the reading accompanies this reply.")
    with pytest.raises((Conflict, NotFound)):
        second.withdraw_task_result(owner, task, kept["id"])
    assert turn_files(second, owner, owl, task) == {kept["id"]}
    assert_count(second, owner, chat, task, 1)
    series = second.files_page(owner, owl, FileQuery(series_id=original["series_id"]))["items"]
    assert [(f["revision"], f.get("withdrawn_from_reply", False)) for f in series] == [
        (2, True),
        (1, False),
    ]
    assert second.file_data(owner, owl, original["id"]) == b"Edition one"
    assert second.file_data(owner, owl, rejected["id"]) == b"Extra"
    assert second.file_data(owner, owl, source["id"]) == b"Granted source"
    follow_up = start(second, owner, chat, "Use the result.", files=[rejected["id"]])
    assert any(
        any(f["id"] == rejected["id"] for f in m.get("files", []))
        for m in first.conversation(owner, chat)["messages"]
    )
    assert [f["previous_file_id"] for f in previous_delivery(first, owner, follow_up)] == [
        kept["id"]
    ]
    first.finish(owner, follow_up["id"], "completed", content="Done.")


def test_room_reply_omits_withdrawn_file_and_keeps_explicit_input(places):
    first, second, owner = places
    writer, reader = [first.save_bot(owner, BotInput(name=n))["id"] for n in ("Writer", "Reader")]
    room = Rooms(first, owner).create(RoomCreate(name="Preparation", bot_ids=[writer, reader]))[
        "id"
    ]
    source = first.save_bytes(owner, reader, "Notes.txt", b"User picked this file")
    request = RoomMessage(
        message="Summarize these notes.",
        request_id=uuid4().hex,
        to=writer,
        files=[{"bot_id": reader, "file_id": source["id"]}],
    )
    queued = Rooms(first, owner).send(room, request)["tasks"][0]
    active = first.claim(owner)
    task = active["id"]
    assert task == queued["id"] and active["bot_id"] == writer
    (granted,) = first.list_files(owner, writer)
    with pytest.raises((Conflict, NotFound)):
        second.withdraw_task_result(owner, task, granted["id"])
    with pytest.raises((Conflict, NotFound)):
        second.withdraw_task_result(owner, task, source["id"])
    extra = first.save_bytes(owner, writer, "Extra.txt", b"Unrequested", source_task_id=task)
    final = first.save_bytes(owner, writer, "Summary.txt", b"Three items", source_task_id=task)
    first.withdraw_task_result(owner, task, extra["id"])
    first.finish(owner, task, "completed", content="The summary is attached.")
    assert Rooms(second, owner).send(room, request)["tasks"][0]["id"] == task
    assert_count(second, owner, room, task, 1)
    assert turn_files(second, owner, writer, task) == {final["id"]}
    assert second.file_data(owner, reader, source["id"]) == second.file_data(
        owner, writer, granted["id"]
    )
    assert second.file(owner, writer, extra["id"])["withdrawn_from_reply"]


def test_visible_html_can_be_selected_and_revised_after_a_withdrawn_successor(places):
    first, second, owner = places
    owl = first.save_bot(owner, BotInput(name="Charts"))["id"]
    chat = first.create_chat(owner, owl, "Dashboard")["id"]
    original_task = start(first, owner, chat, "Make a dashboard.")
    original = first.save_bytes(
        owner, owl, "Board.html", b"<h1>Original</h1>", source_task_id=original_task["id"]
    )
    first.finish(owner, original_task["id"], "completed", content="Dashboard attached.")
    extra_task = start(first, owner, chat, "Read this dashboard.")
    extra = first.save_bytes(
        owner,
        owl,
        "Board.html",
        b"<h1>Abandoned rewrite</h1>",
        previous_file_id=original["id"],
        source_task_id=extra_task["id"],
    )
    first.withdraw_task_result(owner, extra_task["id"], extra["id"])
    first.finish(owner, extra_task["id"], "completed", content="Read.")

    region = {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.3}
    target = {
        "file_id": original["id"],
        "revision": original["revision"],
        "sha256": original["sha256"],
        "state_version": 0,
        "region": region,
        "element": {"label": "Original", "path": "h1", "region": region},
        "viewport": {"width": 640, "height": 400},
    }

    def selection_request():
        return TaskInput(
            message="Update the heading.",
            request_id=uuid4().hex,
            file_ids=[original["id"]],
            interactive_target=target,
        )

    queued = second.enqueue(owner, chat, selection_request())
    task = first.claim(owner)
    assert task["id"] == queued["id"]
    runtime = BotRuntime(first, owner, lambda _: None)
    revision = InteractiveRevision(first, owner, task)

    def tool(name, args):
        return runtime._tool(
            task, name, args, [name], token=runtime.task_token(task["id"]), revision=revision
        )

    assert "<h1>Original</h1>" in tool("read_file", {"file_id": original["id"]})["content"]
    revised = tool(
        "save_file",
        {
            "name": "Board.html",
            "previous_file_id": original["id"],
            "replacements": [{"old": "Original", "new": "Revised"}],
        },
    )
    assert revised["revision"] == 3 and revised["series_id"] == original["series_id"]
    assert second.file_data(owner, owl, revised["id"]) == b"<h1>Revised</h1>"
    assert second.file_data(owner, owl, extra["id"]) == b"<h1>Abandoned rewrite</h1>"
    assert (
        InteractiveRevision(second, owner, task).selection()["already_saved"]["id"] == revised["id"]
    )
    first.finish(owner, task["id"], "completed", content="Heading updated.")
    with pytest.raises(Conflict):
        second.enqueue(owner, chat, selection_request())


def test_assignment_selection_and_review_cannot_return_a_withdrawn_file(places):
    first, second, owner = places
    worker, lead = [first.save_bot(owner, BotInput(name=n))["id"] for n in ("Researcher", "Lead")]
    granted = first.save_bytes(owner, worker, "Approved.txt", b"Approved synthetic input")
    first.set_coordinator(owner, lead, True)
    parent = start(
        first, owner, first.create_chat(owner, lead, "Research")["id"], "Write a report."
    )
    card = first.propose_assignment(
        owner,
        parent["id"],
        AssignmentInput(
            recipient_bot_id=worker,
            brief="Count the entries.",
            expected_result="A text report.",
            work_scope="computer",
            access=AssignmentAccess(offline_computer=True, file_ids=[granted["id"]]),
        ),
    )
    first.finish(owner, parent["id"], "completed", content="Approval requested.")
    approved = second.decide_assignment(owner, lead, card["id"], "approve", "computer")
    child = first.claim(owner)
    task = child["id"]
    assert task == approved["child_task_id"]
    runtime = BotRuntime(first, owner, lambda _: None)

    def tool(tool_name, **args):
        return runtime._tool(child, tool_name, args, [tool_name], token=runtime.task_token(task))

    extra = tool("save_file", name="Extra.txt", content="Unexpected")
    final = tool("save_file", name="Final.txt", content="Six entries")
    tool("select_assignment_outputs", file_ids=[extra["id"], final["id"]])
    tool("withdraw_action", file_id=extra["id"])
    assert second.assignment_context(owner, task)["selected_output_file_ids"] == [final["id"]]
    with pytest.raises((Conflict, NotFound)):
        tool("withdraw_action", file_id=granted["id"])
    assert tool("read_file", file_id=granted["id"])["content"] == "Approved synthetic input"
    with pytest.raises((Conflict, NotFound)):
        tool("select_assignment_outputs", file_ids=[extra["id"]])
    with pytest.raises((Conflict, NotFound)):
        second.checkpoint_assignment_result(owner, task, "Stale", output_file_ids=[extra["id"]])
    tool("select_assignment_outputs", file_ids=[final["id"]])
    second.checkpoint_assignment_result(owner, task, "Six entries", output_file_ids=[final["id"]])
    first.finish(
        owner,
        task,
        "completed",
        content="Six entries",
        assignment_verification={"status": "met"},
        assignment_output_file_ids=[final["id"]],
    )
    returned = second.list_files(owner, lead)
    assert [f["name"] for f in returned] == ["Final.txt"]
    assert second.file_data(owner, lead, returned[0]["id"]) == b"Six entries"
    assert second.file_data(owner, worker, extra["id"]) == b"Unexpected"


def test_portable_move_preserves_withdrawal_on_its_remapped_revision(places, tmp_path, monkeypatch):
    source, _, owner = places
    monkeypatch.setattr(move_protocol, "OWNER", owner)
    owl = source.save_bot(owner, BotInput(name="Traveller"))["id"]
    chat = source.create_chat(owner, owl, "Travel")["id"]
    initial = start(source, owner, chat, "Create the itinerary.")
    one = source.save_bytes(owner, owl, "Stops.txt", b"Original trip", source_task_id=initial["id"])
    source.finish(owner, initial["id"], "completed", content="Itinerary attached.")
    latest = start(source, owner, chat, "Review the itinerary.")
    two = source.save_bytes(
        owner,
        owl,
        "Stops.txt",
        b"Unwanted edit",
        previous_file_id=one["id"],
        source_task_id=latest["id"],
    )
    source.withdraw_task_result(owner, latest["id"], two["id"])
    source.finish(owner, latest["id"], "completed", content="Use the original.")

    destination_path = tmp_path / "new-place" / "owls.sqlite"
    destination = Store(destination_path)
    moves = OwlMoves(destination, owner)
    move_id = moves.begin(MoveBegin(request_id=uuid4().hex, source_bot_id=owl))["id"]
    pages, cursor = [], None
    while True:
        page = export_page(source, owner, owl, MoveExport(cursor=cursor))
        pages.append(page)
        cursor = page["cursor"]
        if cursor is None:
            break
    move_protocol.send_pages(moves, move_id, pages)
    assert move_protocol.copy_files(source, destination, owl, moves.receipt(move_id), pages)
    new_owl = moves.complete(move_id)["owl"]["id"]
    reopened = Store(destination_path)
    (new_chat,) = reopened.list_chats(owner, new_owl)
    earlier, current = reopened.conversation(owner, new_chat["id"])["tasks"]
    assert_count(reopened, owner, new_chat["id"], current["id"], 0)
    assert turn_files(reopened, owner, new_owl, current["id"]) == set()
    (new_one,) = turn_files(reopened, owner, new_owl, earlier["id"])
    new_two = reopened.files_page(owner, new_owl, FileQuery())["items"][0]
    assert new_one != one["id"] and new_two["id"] != two["id"]
    assert new_two["withdrawn_from_reply"] and new_two["revision"] == 2
    assert reopened.file_data(owner, new_owl, new_one) == b"Original trip"
    assert reopened.file_data(owner, new_owl, new_two["id"]) == b"Unwanted edit"
    follow_up = start(reopened, owner, new_chat["id"], "Which itinerary did you send?")
    assert [f["previous_file_id"] for f in previous_delivery(reopened, owner, follow_up)] == [
        new_one
    ]
    runtime = BotRuntime(reopened, owner, lambda _: None)
    revised = runtime._tool(
        follow_up,
        "save_file",
        {
            "name": "Stops.txt",
            "previous_file_id": new_one,
            "content": "Original trip with a return leg",
        },
        ["save_file"],
        token=runtime.task_token(follow_up["id"]),
    )
    assert revised["series_id"] == new_two["series_id"] and revised["revision"] == 3
    assert reopened.file_data(owner, new_owl, new_two["id"]) == b"Unwanted edit"
    with pytest.raises(Conflict):
        runtime._tool(
            follow_up,
            "save_file",
            {
                "name": "Stops.txt",
                "previous_file_id": new_one,
                "content": "Overwrite a newer delivery",
            },
            ["save_file"],
            token=runtime.task_token(follow_up["id"]),
        )
