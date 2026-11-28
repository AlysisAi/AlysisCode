"""Moving an owl between the person's places (owl_move.py), driven as the desktop app drives it."""

from __future__ import annotations

import base64
import copy
import hashlib
import json

import pytest

from alysis_code.bots.action_review import ActionReviews, ReviewSettings, RuleInput
from alysis_code.bots.models import (
    BotInput,
    FileInput,
    MemoryInput,
    NoteConsolidation,
    NoteInput,
    NoteSource,
    RegenerateInput,
    TaskInput,
    TimeZoneSettings,
)
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.owl_move import (
    MAX_COUNTS,
    MOVED_PAUSE_CODE,
    MOVED_PAUSE_REASON,
    InvalidMove,
    MoveBegin,
    MoveExport,
    MovePage,
    OwlMoves,
    export_page,
    require_unchanged,
    summary,
)
from alysis_code.bots.owl_move import fingerprint as owl_fingerprint
from alysis_code.bots.queued_messages import QueuedMessageChange, QueuedMessages
from alysis_code.bots.reactions import ReactionInput, ReplyReactions
from alysis_code.bots.regenerate import regenerates
from alysis_code.bots.routine_access import RoutineAccessDecision
from alysis_code.bots.routine_suggestions import RoutineSuggestionDecision, RoutineSuggestions
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store
from alysis_code.bots.transfers import FileTransfers, UploadChunk, UploadInput
from alysis_code.bots.triggers import TriggerDecision, TriggerInput, TriggerStore

OWNER = "alice"
PHOTO = bytes(range(256)) * 2600  # Over two chunks of the transfer.


@pytest.fixture(params=["rows", "objects"])
def places(tmp_path, request):
    """This computer, and a place that keeps files in object storage (as Cloud does)."""
    source = Store(tmp_path / "here" / "bots.sqlite3")
    objects = LocalObjectStorage(tmp_path / "objects") if request.param == "objects" else None
    target = Store(tmp_path / "there" / "bots.sqlite3", objects=objects)
    return source, target


def turn(store, chat_id, message, request_id, *, reply=None, notes=(), fail=None, files=()):
    store.enqueue(
        OWNER, chat_id, TaskInput(message=message, request_id=request_id, file_ids=list(files))
    )
    task = store.claim(OWNER)
    for note in notes:
        store.event(OWNER, task["id"], "progress_note", {"text": note})
    store.event(OWNER, task["id"], "tool_started", {"name": "web_search", "topic": message})
    if fail:
        store.finish(
            OWNER, task["id"], "failed", error="Η υπηρεσία δεν απάντησε.", failure={"kind": fail}
        )
    else:
        store.finish(OWNER, task["id"], "completed", content=reply)
    return store.task(OWNER, task["id"])


def make_owl(store):
    """An owl with everything a move carries, in Greek and Japanese."""
    bot = store.save_bot(
        OWNER,
        BotInput(
            name="Αθηνά",
            purpose="毎朝のニュースをまとめる",
            instructions="Απάντα σύντομα, σε απλά λόγια.",
            color="violet",
            background="ocean",
        ),
    )
    owl = bot["id"]
    chat = store.create_chat(OWNER, owl, "Συνομιλία")
    notes_file = store.save_file(
        OWNER, owl, FileInput(name="σημειώσεις.md", content="Πρώτη μορφή\n")
    )
    notes_file = store.save_file(
        OWNER,
        owl,
        FileInput(
            name="σημειώσεις.md",
            content="Δεύτερη μορφή — 第二版\n",
            previous_file_id=notes_file["id"],
        ),
    )
    photo = store.save_bytes(OWNER, owl, "写真.png", PHOTO)
    store.save_bytes(OWNER, owl, "空.bin", b"")
    turn(
        store,
        chat["id"],
        "Γεια σου! Δες το αρχείο.",
        "request-greek-1",
        reply="こんにちは。ファイルを読みました。",
        notes=("Διαβάζω το αρχείο…",),
        files=(notes_file["id"], photo["id"]),
    )
    # A file the owl itself saved while it worked stays with that turn.
    store.enqueue(
        OWNER, chat["id"], TaskInput(message="Φτιάξε μια περίληψη", request_id="request-greek-2")
    )
    working = store.claim(OWNER)
    saved = store.save_bytes(
        OWNER, owl, "要約.txt", "要約です".encode(), source_task_id=working["id"]
    )
    store.event(OWNER, working["id"], "tool_completed", {"name": "save_file", "id": saved["id"]})
    store.finish(OWNER, working["id"], "completed", content="Έτοιμη η περίληψη.")
    finished = max(
        event["seq"]
        for event in store.task(OWNER, working["id"])["events"]
        if event["kind"] == "completed"
    )
    store.mark_chat_read(OWNER, chat["id"], finished)
    # The newest turn failed and hasn't been seen yet.
    turn(store, chat["id"], "Και τα νέα;", "request-greek-3", fail="network")
    store.save_memory(OWNER, owl, MemoryInput(title="Γλώσσα", content="Προτιμά ελληνικά"))
    store.save_memory(OWNER, owl, MemoryInput(title="時間", content="朝7時に要約"))
    note = store.save_note(OWNER, owl, NoteInput(title="Σχέδιο", content="Βήμα 1"))
    note = store.save_note(
        OWNER,
        owl,
        NoteInput(title="Σχέδιο", content="Βήμα 1, Βήμα 2", expected_revision=1),
        note_id=note["id"],
    )
    other = store.save_note(OWNER, owl, NoteInput(title="メモ", content="古いメモ"))
    store.consolidate_notes(
        OWNER,
        owl,
        NoteConsolidation(
            title="Όλα μαζί",
            content="Σχέδιο και メモ",
            sources=[
                NoteSource(note_id=note["id"], expected_revision=2),
                NoteSource(note_id=other["id"], expected_revision=1),
            ],
            reason="Ένα σημείωμα αρκεί.",
        ),
    )
    routine = store.save_schedule(
        OWNER,
        owl,
        ScheduleInput.model_validate(
            {
                "name": "Πρωινή σύνοψη",
                "prompt": "Μάζεψε τα νέα της ημέρας.",
                "timing": {"kind": "daily", "time": "07:00", "timezone": "Europe/Athens"},
                "needs": {"sites": ["https://ειδήσεις.example"]},
            }
        ),
    )
    store.allow_routine_access(
        OWNER,
        owl,
        routine["id"],
        RoutineAccessDecision(
            expected_revision=routine["revision"], allow=routine["access"]["to_allow"]
        ),
    )
    store.save_schedule(
        OWNER,
        owl,
        ScheduleInput.model_validate(
            {
                "name": "週次まとめ",
                "prompt": "今週のまとめを書く",
                "timing": {"kind": "interval", "every_minutes": 60},
                "enabled": False,
            }
        ),
    )
    triggers = TriggerStore(store, OWNER)
    trigger = triggers.create(
        owl, TriggerInput(name="Νέο αρχείο", instructions="Διάβασε κάθε νέο αρχείο.")
    )
    triggers.decide(owl, trigger["id"], TriggerDecision(state="active", expected_revision=1))
    reviews = ActionReviews(store, OWNER)
    reviews.settings(owl, ReviewSettings(mode="review"))
    reviews.save_rule(owl, RuleInput(effect="ask", description="Ρώτα πριν στείλεις email"))
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO bot_plugin_grants(bot_id,plugin_id,enabled,revision,updated_at) VALUES(?,?,0,1,1)",
            (owl, "alysis.github"),
        )
    return owl, chat["id"]


def export_all(store, owl, *, threads=False):
    pages, cursor = [], None
    while True:
        page = export_page(store, OWNER, owl, MoveExport(cursor=cursor, threads=threads))
        pages.append(page)
        cursor = page["cursor"]
        if cursor is None:
            return pages


def send_pages(moves, move_id, pages):
    for number, page in enumerate(pages):
        moves.receive(
            move_id, MovePage(page=number, last=page["cursor"] is None, records=page["records"])
        )


def copy_files(source, target, owl, receipt, pages, *, interrupt_after=None):
    """The app's part: each file not there yet, through the ordinary resumable uploads."""
    there = {item["source_id"]: item["file_id"] for item in receipt["files"]}
    files = sorted(
        (record for page in pages for record in page["records"] if record["kind"] == "file"),
        key=lambda record: (record["name"], record["revision"]),
    )
    reading, writing = FileTransfers(source, OWNER), FileTransfers(target, OWNER)
    previous: dict[str, str] = {}
    sent = 0
    for record in files:
        if there.get(record["id"]):
            previous[record["name"]] = there[record["id"]]
            continue
        upload = writing.begin(
            receipt["bot_id"],
            UploadInput(
                name=record["name"],
                size=record["size_bytes"],
                request_id=f"move-{receipt['id']}-{record['id']}",
                previous_file_id=previous.get(record["name"]),
            ),
        )
        offset = upload["received"]
        while offset < record["size_bytes"]:
            chunk = reading.chunk(owl, record["id"], offset)
            assert (
                hashlib.sha256(base64.b64decode(chunk["data_base64"])).hexdigest()
                == chunk["sha256"]
            )
            writing.append(
                receipt["bot_id"],
                upload["id"],
                UploadChunk(offset=offset, data_base64=chunk["data_base64"]),
            )
            offset = chunk["next_offset"]
            sent += 1
            if interrupt_after is not None and sent >= interrupt_after:
                return False
        done = writing.complete(receipt["bot_id"], upload["id"])
        previous[record["name"]] = done["file_id"]
    return True


def move(source, target, owl, request_id="move-request-0001"):
    moves = OwlMoves(target, OWNER)
    started = moves.begin(MoveBegin(request_id=request_id, source_bot_id=owl))
    pages = export_all(source, owl)
    send_pages(moves, started["id"], pages)
    assert copy_files(source, target, owl, moves.receipt(started["id"]), pages)
    done = moves.complete(started["id"])
    source.delete_bot(
        OWNER, owl, check=lambda db: require_unchanged(db, owl, pages[0]["fingerprint"])
    )
    return done, pages


def test_move_keeps_separate_conversations_including_empty_threads(places):
    source, target = places
    owl = source.save_bot(OWNER, BotInput(name="Scout"))["id"]
    default = source.create_chat(OWNER, owl, "Main")
    other = source.create_chat(OWNER, owl, "Field notes", thread_request_id="field_notes")
    source.create_chat(OWNER, owl, "Empty", thread_request_id="empty_thread")
    turn(source, default["id"], "Main question", "main_question", reply="Main answer")
    turn(source, other["id"], "Field question", "field_question", reply="Field answer")
    with pytest.raises(Conflict, match="Update both places"):
        export_all(source, owl)
    pages = export_all(source, owl, threads=True)
    moves = OwlMoves(target, OWNER)
    transfer = moves.begin(MoveBegin(request_id="transfer_threads", source_bot_id=owl))
    send_pages(moves, transfer["id"], pages)
    moved = moves.complete(transfer["id"])["owl"]["id"]
    chats = target.list_chats(OWNER, moved)
    threads = {c["title"]: target.conversation(OWNER, c["id"]) for c in chats}
    assert [m["content"] for m in threads["Field notes"]["messages"]] == [
        "Field question",
        "Field answer",
    ]
    assert threads["Empty"]["messages"] == []
    assert [m["content"] for m in target.conversation(OWNER, chats[0]["id"])["messages"]] == [
        "Main question",
        "Main answer",
    ]


def test_an_owl_moves_with_everything_that_makes_it_that_owl(places):
    source, target = places
    owl, chat_id = make_owl(source)
    original_bot = source.bot(OWNER, owl)
    original = source.conversation(OWNER, chat_id)
    original_files = {(f["name"], f["revision"]): f for f in source.list_files(OWNER, owl)}
    original_bytes = {
        key: b"".join(source.file_chunks(OWNER, owl, file["id"]))
        for key, file in original_files.items()
    }
    original_memories = source.list_memories(OWNER, owl)
    original_notes = source.list_notes(OWNER, owl, include_archived=True)
    original_routines = {r["name"]: r for r in source.list_schedules(OWNER, owl)}

    done, pages = move(source, target, owl)
    copy_id = done["owl"]["id"]

    # Gone here, there with its setup and look; its id is new.
    assert source.list_bots(OWNER) == []
    listed = target.list_bots(OWNER)
    assert [item["id"] for item in listed] == [copy_id] and copy_id != owl
    moved = listed[0]
    for key in (
        "name",
        "purpose",
        "instructions",
        "color",
        "background",
        "memory_enabled",
        "files_enabled",
        "created_at",
    ):
        assert moved[key] == original_bot[key]
    assert moved["latest_activity"]["preview"] == "Και τα νέα;"
    assert moved["unread_chats"] == 1

    # Its chat: every message and reply in order, with the turns' steps, failures and files.
    chat = target.list_chats(OWNER, copy_id)[0]
    conversation = target.conversation(OWNER, chat["id"])
    assert [(m["role"], m["content"]) for m in conversation["messages"]] == [
        (m["role"], m["content"]) for m in original["messages"]
    ]
    assert [t["status"] for t in conversation["tasks"]] == [t["status"] for t in original["tasks"]]
    assert conversation["tasks"][0]["progress_notes"] == ["Διαβάζω το αρχείο…"]
    assert conversation["tasks"][2]["failure"] == {"kind": "network"}
    assert conversation["tasks"][1]["file_count"] == 1
    assert conversation["tasks"][0]["request_id"] == "request-greek-1"
    attached = conversation["messages"][0]["files"]
    copies = {(f["name"], f["revision"]): f for f in target.list_files(OWNER, copy_id)}
    assert [(f["name"], f["available"]) for f in attached] == [
        ("σημειώσεις.md", True),
        ("写真.png", True),
    ]
    assert attached[0]["id"] == copies[("σημειώσεις.md", 2)]["id"]
    saved_step = [
        e
        for e in target.task(OWNER, conversation["tasks"][1]["id"])["events"]
        if e["kind"] == "tool_completed"
    ]
    assert saved_step[0]["data"]["id"] == copies[("要約.txt", 1)]["id"]
    # What the person had read stays read; the failed turn they hadn't seen still waits for them.
    assert conversation["unread"] is True
    assert [item["kind"] for item in target.needs_you(OWNER)["items"]].count("failed") == 1

    # Library: every version, as text or bytes as it was, byte for byte.
    assert set(copies) == set(original_files)
    for key, file in original_files.items():
        copied = copies[key]
        for part in ("sha256", "size_bytes", "binary", "media_type", "created_at"):
            assert copied[part] == file[part], (key, part)
        assert b"".join(target.file_chunks(OWNER, copy_id, copied["id"])) == original_bytes[key]
    assert (
        target.file(OWNER, copy_id, copies[("σημειώσεις.md", 2)]["id"])["content"]
        == "Δεύτερη μορφή — 第二版\n"
    )
    assert copies[("σημειώσεις.md", 2)]["series_id"] == copies[("σημειώσεις.md", 1)]["series_id"]

    # Memories and working notes, with history and what was archived.
    assert [
        (m["title"], m["content"], m["status"]) for m in target.list_memories(OWNER, copy_id)
    ] == [(m["title"], m["content"], m["status"]) for m in original_memories]
    notes = {n["title"]: n for n in target.list_notes(OWNER, copy_id, include_archived=True)}
    assert {n["title"] for n in original_notes} == set(notes)
    assert (
        notes["Σχέδιο"]["revision"] == 2
        and notes["Σχέδιο"]["consolidated_into"] == notes["Όλα μαζί"]["id"]
    )
    history = target.note(OWNER, copy_id, notes["Σχέδιο"]["id"])
    assert [r["content"] for r in history["revisions"]][-1] == "Βήμα 1"

    # Routines and file events arrive off and say so; what they need travels, not what was allowed.
    routines = {r["name"]: r for r in target.list_schedules(OWNER, copy_id)}
    assert routines["Πρωινή σύνοψη"]["state"] == "paused"
    assert routines["Πρωινή σύνοψη"]["pause_code"] == MOVED_PAUSE_CODE
    assert routines["Πρωινή σύνοψη"]["pause_reason"] == MOVED_PAUSE_REASON
    assert routines["Πρωινή σύνοψη"]["needs"] == original_routines["Πρωινή σύνοψη"]["needs"]
    assert routines["Πρωινή σύνοψη"]["needs"]["sites"]
    assert original_routines["Πρωινή σύνοψη"]["access"]["allowed"] is not None
    assert routines["Πρωινή σύνοψη"]["access"]["allowed"] is None
    assert (
        routines["週次まとめ"]["state"] == "paused" and routines["週次まとめ"]["pause_code"] is None
    )
    assert target.enqueue_due_schedules(OWNER, max_calls=None, now=4_000_000_000) == 0
    assert [t["state"] for t in TriggerStore(target, OWNER).list(copy_id)] == ["paused"]
    routine_items = [i for i in target.needs_you(OWNER)["items"] if i["kind"] == "routine"]
    assert [(i["text"], i["pause_code"]) for i in routine_items] == [
        ("Πρωινή σύνοψη", MOVED_PAUSE_CODE)
    ]

    # Its approval mode and rules, and which apps it may use.
    settings = ActionReviews(target, OWNER).settings(copy_id)
    assert settings["mode"] == "review"
    assert [r["description"] for r in settings["rules"]] == ["Ρώτα πριν στείλεις email"]
    with target.connect() as db:
        grants = db.execute(
            "SELECT plugin_id,enabled FROM bot_plugin_grants WHERE bot_id=?", (copy_id,)
        ).fetchall()
    assert [tuple(row) for row in grants] == [("alysis.github", 0)]
    # Files that arrived as uploads were not added by the person there: no file events from them.
    with target.connect() as db:
        assert db.execute("SELECT count(*) FROM activation_events").fetchone()[0] == 0
    assert summary(target, OWNER, copy_id)["files"] == len(original_files)
    assert len(pages) >= 1 and pages[0]["summary"]["file_bytes"] == sum(
        f["size_bytes"] for f in original_files.values()
    )


def test_a_move_retried_with_the_same_request_never_makes_two_copies(places):
    source, target = places
    owl, _chat = make_owl(source)
    moves = OwlMoves(target, OWNER)
    started = moves.begin(MoveBegin(request_id="move-request-0001", source_bot_id=owl))
    assert (
        moves.begin(MoveBegin(request_id="move-request-0001", source_bot_id=owl))["id"]
        == started["id"]
    )
    pages = export_all(source, owl)
    # The first page's answer was lost: sent again, it changes nothing.
    send_pages(moves, started["id"], pages[:1])
    again = moves.receive(
        started["id"],
        MovePage(page=0, last=pages[0]["cursor"] is None, records=pages[0]["records"]),
    )
    assert again["pages"] == 1
    send_pages(moves, started["id"], pages)
    # Hidden while it arrives, from the list and from Needs you.
    assert target.list_bots(OWNER) == [] and target.needs_you(OWNER)["count"] == 0
    # A transfer cut off half way resumes where it stopped.
    receipt = moves.receipt(started["id"])
    assert not copy_files(source, target, owl, receipt, pages, interrupt_after=1)
    with pytest.raises(Conflict):
        moves.complete(started["id"])
    assert copy_files(source, target, owl, moves.receipt(started["id"]), pages)
    assert all(item["file_id"] for item in moves.receipt(started["id"])["files"])
    first = moves.complete(started["id"])
    assert moves.complete(started["id"])["owl"]["id"] == first["owl"]["id"]
    assert len(target.list_bots(OWNER)) == 1
    with target.connect() as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == len(
            source.list_files(OWNER, owl)
        )
    # Another request for the same owl, while the original still stands, makes no second copy.
    with pytest.raises(Conflict):
        moves.begin(MoveBegin(request_id="move-request-0002", source_bot_id=owl))
    assert len(target.list_bots(OWNER)) == 1


def test_an_unfinished_earlier_try_gives_way_and_cancel_leaves_nothing_behind(places):
    source, target = places
    owl, _chat = make_owl(source)
    moves = OwlMoves(target, OWNER)
    first = moves.begin(MoveBegin(request_id="move-request-0001", source_bot_id=owl))
    pages = export_all(source, owl)
    send_pages(moves, first["id"], pages)
    staged = moves.receipt(first["id"])["bot_id"]
    second = moves.begin(MoveBegin(request_id="move-request-0002", source_bot_id=owl))
    assert moves.receipt(first["id"])["state"] == "cancelled"
    with pytest.raises(NotFound):
        target.bot(OWNER, staged)
    send_pages(moves, second["id"], pages)
    assert moves.cancel(second["id"])["state"] == "cancelled"
    assert moves.cancel(second["id"])["state"] == "cancelled"
    with target.connect() as db:
        assert db.execute("SELECT count(*) FROM bots WHERE owner=?", (OWNER,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    # A move nobody finishes is cleared after a day.
    third = moves.begin(MoveBegin(request_id="move-request-0003", source_bot_id=owl))
    send_pages(moves, third["id"], pages)
    assert moves.expire(now=10**10) == 1
    assert moves.receipt(third["id"])["state"] == "cancelled"
    # The original never changed.
    assert len(source.list_files(OWNER, owl)) == 5
    assert summary(source, OWNER, owl)["fingerprint"] == pages[0]["fingerprint"]


def test_a_change_while_moving_leaves_the_owl_where_it_was(places):
    source, target = places
    owl, chat_id = make_owl(source)
    moves = OwlMoves(target, OWNER)
    started = moves.begin(MoveBegin(request_id="move-request-0001", source_bot_id=owl))
    pages = export_all(source, owl)
    send_pages(moves, started["id"], pages)
    copy_files(source, target, owl, moves.receipt(started["id"]), pages)
    copy = moves.complete(started["id"])["owl"]
    # The person told the original something new while its files were moving.
    source.save_memory(OWNER, owl, MemoryInput(title="Νέο", content="Κάτι καινούργιο"))
    with pytest.raises(Conflict, match="changed while it moved"):
        source.delete_bot(
            OWNER, owl, check=lambda db: require_unchanged(db, owl, pages[0]["fingerprint"])
        )
    # The app takes back the copy with the ordinary delete: one owl, where it was, unchanged.
    target.delete_bot(OWNER, copy["id"])
    assert target.list_bots(OWNER) == []
    assert [b["id"] for b in source.list_bots(OWNER)] == [owl]
    assert "Νέο" in [m["title"] for m in source.list_memories(OWNER, owl)]
    assert len(source.conversation(OWNER, chat_id)["messages"]) == 5


def test_a_working_owl_or_one_that_changes_between_pages_is_not_read(places, monkeypatch):
    source, _target = places
    owl, chat_id = make_owl(source)
    source.enqueue(OWNER, chat_id, TaskInput(message="Κάτι ακόμα", request_id="request-greek-9"))
    assert summary(source, OWNER, owl)["working"] is True
    with pytest.raises(Conflict, match="Stop this owl's work"):
        export_page(source, OWNER, owl, MoveExport())
    task = source.claim(OWNER)
    source.finish(OWNER, task["id"], "completed", content="Εντάξει")
    # Small pages, so the owl is read in several.
    monkeypatch.setattr("alysis_code.bots.owl_move.PAGE_BYTES", 600)
    first = export_page(source, OWNER, owl, MoveExport())
    assert first["cursor"] and len(first["records"]) >= 1
    source.save_note(OWNER, owl, NoteInput(title="Νέα σημείωση", content="Μόλις τώρα"))
    with pytest.raises(Conflict, match="changed while it moved"):
        export_page(source, OWNER, owl, MoveExport(cursor=first["cursor"]))
    with pytest.raises(InvalidMove):
        export_page(source, OWNER, owl, MoveExport(cursor="bm90IGEgY3Vyc29y"))
    # A part larger than a move carries is refused, never cut short.
    monkeypatch.setattr("alysis_code.bots.owl_move.MAX_RECORD_BYTES", 100)
    with pytest.raises(LimitReached, match="too large"):
        export_page(source, OWNER, owl, MoveExport())


def test_many_small_pages_carry_the_same_owl(places, monkeypatch):
    source, target = places
    owl, _chat = make_owl(source)
    whole = [record for page in export_all(source, owl) for record in page["records"]]
    monkeypatch.setattr("alysis_code.bots.owl_move.PAGE_BYTES", 300)
    pages = export_all(source, owl)
    assert len(pages) > 5
    assert [record for page in pages for record in page["records"]] == whole
    done, _ = move(source, target, owl)
    assert done["state"] == "complete"


def staged(source, target, owl):
    moves = OwlMoves(target, OWNER)
    started = moves.begin(MoveBegin(request_id="move-request-0001", source_bot_id=owl))
    return moves, started["id"], export_all(source, owl)


def records_of(pages):
    return [record for page in pages for record in page["records"]]


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda r: r[3].update(extra="field"), id="unknown-field"),
        pytest.param(lambda r: r.append({"kind": "secret", "id": "x"}), id="unknown-kind"),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "turn").update(message="λ" * 20001),
            id="oversized-message",
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "turn")["events"].append(
                {"kind": "progress_note", "data": {"text": "あ" * 20000}, "created_at": 1.0}
            ),
            id="oversized-step",
        ),
        pytest.param(
            lambda r: r.insert(0, copy.deepcopy(next(x for x in r if x["kind"] == "turn"))),
            id="turn-before-owl",
        ),
        pytest.param(
            lambda r: r.append(copy.deepcopy(next(x for x in r if x["kind"] == "memory"))),
            id="out-of-order",
        ),
        pytest.param(
            lambda r: r.insert(4, copy.deepcopy(next(x for x in r if x["kind"] == "turn"))),
            id="duplicate-turn",
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "file").update(name="../../etc/passwd"),
            id="path-name",
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "file").update(sha256="00"), id="bad-hash"
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "turn").update(status="running"),
            id="running-turn",
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "owl").update(memory_enabled="yes"),
            id="loose-type",
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "routine").update(timing={"kind": "never"}),
            id="bad-timing",
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "turn")["config"].update(
                activation={"events": "all"}
            ),
            id="bad-activation",
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "app").update(plugin_id="anything at all"),
            id="bad-app",
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "archive").update(target_note_id="f" * 32),
            id="archive-elsewhere",
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "turn")["messages"][0].update(
                role="user",
                reaction={"reaction": "up", "note": "", "created_at": 1.0, "updated_at": 1.0},
            ),
            id="reaction-on-own-message",
        ),
        pytest.param(
            lambda r: next(x for x in r if x["kind"] == "turn").update(
                queued={"revision": 0, "sent_sha256": "0" * 64, "withdrawn_at": 5.0}
            ),
            id="cancelled-message-still-there",
        ),
    ],
)
def test_a_tampered_owl_is_refused_whole(places, tamper):
    source, target = places
    owl, _chat = make_owl(source)
    moves, move_id, pages = staged(source, target, owl)
    records = copy.deepcopy(records_of(pages))
    tamper(records)
    with pytest.raises(InvalidMove):
        moves.receive(move_id, MovePage(page=0, last=True, records=records))
    receipt = moves.receipt(move_id)
    assert receipt["pages"] == 0 and receipt["bot_id"] is None
    with target.connect() as db:
        assert db.execute("SELECT count(*) FROM bots").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    # The real owl still moves afterwards.
    send_pages(moves, move_id, pages)
    assert moves.receipt(move_id)["received_all"] is True


def test_parts_must_come_in_order_and_a_resent_part_must_match(places):
    source, target = places
    owl, _chat = make_owl(source)
    moves, move_id, pages = staged(source, target, owl)
    records = records_of(pages)
    with pytest.raises(Conflict, match="missing"):
        moves.receive(move_id, MovePage(page=1, last=False, records=records[:1]))
    moves.receive(move_id, MovePage(page=0, last=False, records=records[:1]))
    changed = copy.deepcopy(records[:1])
    changed[0]["name"] = "Άλλη"
    with pytest.raises(Conflict, match="other content"):
        moves.receive(move_id, MovePage(page=0, last=False, records=changed))
    with pytest.raises(InvalidMove, match="owl"):
        moves.receive(move_id, MovePage(page=1, last=False, records=records[:1]))
    moves.receive(move_id, MovePage(page=1, last=True, records=records[1:]))
    with pytest.raises(Conflict):
        moves.receive(move_id, MovePage(page=2, last=True, records=[]))


def test_counts_and_owner_are_bounded(places):
    source, target = places
    owl, _chat = make_owl(source)
    moves, move_id, pages = staged(source, target, owl)
    records = records_of(pages)
    routine = next(r for r in records if r["kind"] == "routine")
    many = [
        {**copy.deepcopy(routine), "id": f"{index:032x}"}
        for index in range(MAX_COUNTS["routine"] + 1)
    ]
    owl_record = records[0]
    with pytest.raises(InvalidMove, match="more than"):
        moves.receive(move_id, MovePage(page=0, last=True, records=[owl_record, *many]))
    # Someone else's move is not theirs to read, add to or finish.
    stranger = OwlMoves(target, "bob")
    for action in (
        lambda: stranger.receipt(move_id),
        lambda: stranger.receive(move_id, MovePage(page=0, last=True, records=records)),
        lambda: stranger.complete(move_id),
        lambda: stranger.cancel(move_id),
    ):
        with pytest.raises(NotFound):
            action()
    # A full account takes no more owls, moved ones included.
    for index in range(30):
        target.save_bot(OWNER, BotInput(name=f"Κουκουβάγια {index}"))
    with pytest.raises(LimitReached):
        moves.receive(move_id, MovePage(page=0, last=True, records=records))


def test_a_file_that_differs_from_what_was_described_never_completes(places):
    source, target = places
    owl, _chat = make_owl(source)
    moves, move_id, pages = staged(source, target, owl)
    for record in records_of(pages):
        if record["kind"] == "file" and record["name"] == "写真.png":
            record["sha256"] = hashlib.sha256(b"something else").hexdigest()
    send_pages(moves, move_id, pages)
    # The bytes that arrive are not the ones described.
    copy_files(source, target, owl, moves.receipt(move_id), pages)
    with pytest.raises(Conflict, match="haven't arrived"):
        moves.complete(move_id)
    assert target.list_bots(OWNER) == []
    assert [b["id"] for b in source.list_bots(OWNER)] == [owl]


def test_a_text_file_that_is_not_text_is_refused(places):
    source, target = places
    owl, _chat = make_owl(source)
    moves, move_id, pages = staged(source, target, owl)
    garbled = b"\xff\xfe\xfd"
    for record in records_of(pages):
        if record["kind"] == "file" and record["name"] == "写真.png":
            record.update(
                binary=False, size_bytes=len(garbled), sha256=hashlib.sha256(garbled).hexdigest()
            )
    send_pages(moves, move_id, pages)
    receipt = moves.receipt(move_id)
    # Every other file as usual, and bytes that aren't text under the photo's name.
    others = copy.deepcopy(pages)
    for page in others:
        page["records"] = [r for r in page["records"] if r.get("name") != "写真.png"]
    copy_files(source, target, owl, receipt, others)
    writing = FileTransfers(target, OWNER)
    upload = writing.begin(
        receipt["bot_id"],
        UploadInput(name="写真.png", size=len(garbled), request_id="move-photo-bytes"),
    )
    chunk = UploadChunk(offset=0, data_base64=base64.b64encode(garbled).decode())
    writing.append(receipt["bot_id"], upload["id"], chunk)
    writing.complete(receipt["bot_id"], upload["id"])
    with pytest.raises(InvalidMove, match="isn't text"):
        moves.complete(move_id)
    assert target.list_bots(OWNER) == []


def test_an_owl_moves_back_and_forth(tmp_path):
    here = Store(tmp_path / "here.sqlite3")
    there = Store(tmp_path / "there.sqlite3")
    owl, _chat = make_owl(here)
    done, _ = move(here, there, owl, "move-request-0001")
    back, _ = move(there, here, done["owl"]["id"], "move-request-0002")
    returned = here.list_bots(OWNER)
    assert [b["id"] for b in returned] == [back["owl"]["id"]] and back["owl"]["id"] != owl
    assert returned[0]["name"] == "Αθηνά" and returned[0]["color"] == "violet"
    chat = here.list_chats(OWNER, back["owl"]["id"])[0]
    assert len(here.conversation(OWNER, chat["id"])["messages"]) == 5
    assert (
        json.loads(json.dumps(here.list_memories(OWNER, back["owl"]["id"])))[0]["title"] == "Γλώσσα"
    )


def test_a_pinned_or_archived_owl_keeps_its_place_in_the_list_but_not_its_model(places):
    source, target = places
    owl, _chat_id = make_owl(source)
    with source.connect(write=True) as db:
        db.execute(
            "UPDATE bots SET pinned_at=?,archived_at=?,model=? WHERE id=?",
            (1000.0, 2000.0, "this-place-only", owl),
        )
    done, _pages = move(source, target, owl)
    moved = target.bot(OWNER, done["owl"]["id"])
    assert (moved["pinned_at"], moved["archived_at"]) == (1000.0, 2000.0)
    # Model ids belong to one place: the copy thinks with its new place's default.
    assert moved["model"] is None


def test_a_new_answer_still_replaces_the_same_answer_after_the_move(places):
    source, target = places
    owl = source.save_bot(OWNER, BotInput(name="Ερμής"))["id"]
    chat = source.create_chat(OWNER, owl, "Ερωτήσεις")
    first = turn(source, chat["id"], "Πες μου ένα ποίημα", "request-poem-1", reply="Πρώτο ποίημα")
    asked = source.regenerate(OWNER, first["id"], RegenerateInput(request_id="request-again-1"))
    claimed = source.claim(OWNER)
    assert claimed["id"] == asked["id"]
    source.finish(OWNER, asked["id"], "completed", content="二つ目の詩")

    done, _pages = move(source, target, owl)
    with target.connect() as db:
        ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM tasks WHERE bot_id=? ORDER BY created_at,id", (done["owl"]["id"],)
            )
        ]
    original, again = (target.task(OWNER, task_id) for task_id in ids)
    assert "regenerate" not in original["config"]
    # The links follow the turns to their new ids, so the chat and the owl's history still read
    # the second answer in place of the first.
    assert again["config"]["regenerate"] == {
        "task_id": original["id"],
        "replaces": original["id"],
        "source": original["id"],
    }
    assert regenerates(again) == original["id"]


def test_reactions_and_messages_changed_before_they_started_move_as_they_are(places):
    source, target = places
    owl = source.save_bot(OWNER, BotInput(name="Ήρα"))["id"]
    chat = source.create_chat(OWNER, owl, "Ερωτήσεις")["id"]
    first = source.enqueue(OWNER, chat, TaskInput(message="Πρώτη", request_id="request-first"))
    assert source.claim(OWNER)["id"] == first["id"]
    # Two more wait while the owl works: the person changes one and cancels the other.
    changed = source.enqueue(OWNER, chat, TaskInput(message="Δεύτερη", request_id="request-second"))
    dropped = source.enqueue(OWNER, chat, TaskInput(message="三つ目", request_id="request-third"))
    queue = QueuedMessages(source, OWNER)
    queue.change(
        changed["id"],
        QueuedMessageChange(action="edit", expected_revision=0, message="Δεύτερη, αλλαγμένη"),
    )
    queue.change(dropped["id"], QueuedMessageChange(action="cancel", expected_revision=0))
    source.finish(OWNER, first["id"], "completed", content="Απάντηση στην πρώτη")
    assert source.claim(OWNER)["id"] == changed["id"]
    source.finish(OWNER, changed["id"], "completed", content="二つ目の答え")
    reply = next(
        m["id"]
        for m in source.conversation(OWNER, chat)["messages"]
        if m["role"] == "assistant" and m["task_id"] == first["id"]
    )
    ReplyReactions(source, OWNER).react(
        owl, reply, ReactionInput(reaction="down", note="Πιο σύντομα, παρακαλώ")
    )

    done, _pages = move(source, target, owl)
    moved_chat = target.list_chats(OWNER, done["owl"]["id"])[0]["id"]
    conversation = target.conversation(OWNER, moved_chat)
    # The cancelled message stays gone; the changed one reads as changed.
    assert [m["content"] for m in conversation["messages"] if m["role"] == "user"] == [
        "Πρώτη",
        "Δεύτερη, αλλαγμένη",
    ]
    turns = sorted(conversation["tasks"], key=lambda t: (t["created_at"], t["id"]))
    assert [t["queued_message"]["edited"] for t in turns] == [False, True, False]
    assert [t["queued_message"]["withdrawn"] for t in turns] == [False, False, True]
    assert [(r["reaction"], r["note"]) for r in turns[0]["reactions"]] == [
        ("down", "Πιο σύντομα, παρακαλώ")
    ]
    moved_reply = next(
        m["id"]
        for m in conversation["messages"]
        if m["role"] == "assistant" and m["task_id"] == turns[0]["id"]
    )
    assert turns[0]["reactions"][0]["message_id"] == moved_reply


def suggested_routine(store, chat_id, message, request_id, arguments):
    """The person's message, the owl's suggested routine while it answers, then its reply."""
    store.enqueue(OWNER, chat_id, TaskInput(message=message, request_id=request_id))
    task = store.claim(OWNER)
    BotRuntime(store, OWNER, lambda _: None)._tool(
        task, "propose_schedule", arguments, ["propose_schedule"]
    )
    store.finish(OWNER, task["id"], "completed", content="Να το κάνω ρουτίνα;")
    return next(
        t["routine_suggestion"]
        for t in store.conversation(OWNER, chat_id)["tasks"]
        if t["id"] == task["id"]
    )


def test_suggested_routines_move_with_what_the_person_chose(places):
    source, target = places
    source.time_zone_settings(OWNER, TimeZoneSettings(time_zone="Europe/Athens"))
    target.time_zone_settings(OWNER, TimeZoneSettings(time_zone="Europe/Athens"))
    owl = source.save_bot(OWNER, BotInput(name="Γλαύκα"))["id"]
    chat = source.create_chat(OWNER, owl, "Τιμολόγια")["id"]
    invoices = {
        "name": "Πρωινά τιμολόγια",
        "prompt": "Έλεγξε τα εισερχόμενα για νέα τιμολόγια.",
        "timing": {"kind": "daily", "time": "08:00"},
    }
    report = {
        "name": "週次レポート",
        "prompt": "今週の売上をまとめる。",
        "timing": {"kind": "weekly", "time": "17:00", "weekdays": [4]},
    }
    tidy = {
        "name": "Τακτοποίηση",
        "prompt": "Τακτοποίησε τα αρχεία της εβδομάδας.",
        "timing": {"kind": "weekly", "time": "18:00", "weekdays": [4]},
    }
    suggestions = RoutineSuggestions(source, OWNER)
    declined = suggested_routine(
        source, chat, "Κάθε πρωί τα τιμολόγια.", "request-sugg-1", invoices
    )
    suggestions.decide(
        owl, declined["id"], RoutineSuggestionDecision(action="decline", expected_revision=1)
    )
    on = suggested_routine(source, chat, "毎週金曜にレポート。", "request-sugg-2", report)
    suggestions.decide(
        owl,
        on["id"],
        RoutineSuggestionDecision(action="turn_on", expected_revision=1, request_id="turn-on-0001"),
    )
    suggested_routine(source, chat, "Κάθε Παρασκευή τακτοποιώ.", "request-sugg-3", tidy)
    # Deciding a card is a change a move notices.
    with source.connect() as db:
        before = owl_fingerprint(db, owl)
        db.execute("UPDATE routine_suggestions SET revision=revision+1 WHERE id=?", (on["id"],))
        assert owl_fingerprint(db, owl) != before

    done, pages = move(source, target, owl)
    assert [r["status"] for r in records_of(pages) if r["kind"] == "suggestion"] == [
        "declined",
        "on",
        "open",
    ]
    copy_id = done["owl"]["id"]
    moved_chat = target.list_chats(OWNER, copy_id)[0]["id"]
    turns = sorted(
        target.conversation(OWNER, moved_chat)["tasks"], key=lambda t: (t["created_at"], t["id"])
    )
    cards = [t["routine_suggestion"] for t in turns]
    assert [(c["status"], c["name"]) for c in cards] == [
        ("declined", "Πρωινά τιμολόγια"),
        ("on", "週次レポート"),
        ("open", "Τακτοποίηση"),
    ]
    routines = {r["name"]: r for r in target.list_schedules(OWNER, copy_id)}
    # The routine that was on arrives off, as every routine does; the open card's draft is there.
    assert routines["週次レポート"]["state"] == "paused"
    assert cards[1]["schedule_id"] == routines["週次レポート"]["id"]
    assert routines["Τακτοποίηση"]["state"] == "draft"
    assert cards[2]["schedule_id"] == routines["Τακτοποίηση"]["id"]
    # The open card can be answered in its new place.
    moved = RoutineSuggestions(target, OWNER)
    answered = moved.decide(
        copy_id,
        cards[2]["id"],
        RoutineSuggestionDecision(action="turn_on", expected_revision=1, request_id="turn-on-0002"),
    )
    assert answered["status"] == "on"
    # What the person declined there is still declined here, in another letter case too.
    target.enqueue(OWNER, moved_chat, TaskInput(message="Τιμολόγια;", request_id="request-sugg-4"))
    task = target.claim(OWNER)
    with pytest.raises(Conflict, match="Not now"):
        BotRuntime(target, OWNER, lambda _: None)._tool(
            task,
            "propose_schedule",
            {**invoices, "name": invoices["name"].upper()},
            ["propose_schedule"],
        )
    target.finish(OWNER, task["id"], "completed", content="Εντάξει.")


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda r: r.update(source_task_id="f" * 32), id="unknown-turn"),
        pytest.param(lambda r: r.update(routine_id="f" * 32), id="unknown-routine"),
        pytest.param(lambda r: r.update(status="maybe"), id="bad-status"),
        pytest.param(lambda r: r.update(timing={"kind": "never"}), id="bad-timing"),
        pytest.param(lambda r: r.update(fingerprint="0" * 64), id="fingerprint-from-the-page"),
    ],
)
def test_a_tampered_suggestion_is_refused(places, tamper):
    source, target = places
    source.time_zone_settings(OWNER, TimeZoneSettings(time_zone="Asia/Tokyo"))
    owl = source.save_bot(OWNER, BotInput(name="フクロウ"))["id"]
    chat = source.create_chat(OWNER, owl, "Chat")["id"]
    suggested_routine(
        source,
        chat,
        "毎朝、請求書を確認しています。",
        "request-sugg-1",
        {
            "name": "請求書",
            "prompt": "請求書を確認する。",
            "timing": {"kind": "daily", "time": "08:00"},
        },
    )
    moves, move_id, pages = staged(source, target, owl)
    records = copy.deepcopy(records_of(pages))
    tamper(next(r for r in records if r["kind"] == "suggestion"))
    with pytest.raises(InvalidMove):
        moves.receive(move_id, MovePage(page=0, last=True, records=records))
    assert moves.receipt(move_id)["bot_id"] is None
