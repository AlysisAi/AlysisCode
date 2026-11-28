import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from alysis_code.bots.files import FileQuery
from alysis_code.bots.models import (
    AssignmentInput,
    AssistantOwlCreateInput,
    AssistantOwlDraftInput,
    BotInput,
    ConversationPageQuery,
    IntroInput,
    NoteInput,
    OwlAssignmentProposal,
    TaskInput,
)
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.store import (
    ASSIGNMENT_PAGE_BYTES,
    ASSIGNMENT_PAGE_SIZE,
    ASSIGNMENT_RESULT_BYTES,
    Conflict,
    LibraryCapacityExceeded,
    LimitReached,
    NotFound,
    Store,
    encode,
)

OWNER = "owner"


def reviewed_setup(*, start_task=True):
    return AssistantOwlCreateInput(
        request_id=uuid4().hex,
        name="Έλεγχος βαρδιών",
        purpose="Find uncovered shifts.",
        instructions="Give a short list with supporting reasons.",
        brief="Η πρωινή βάρδια έχει ένα από τα δύο άτομα. Επισήμανε το κενό.",
        expected_result="A short Greek note saying that one more person is needed.",
        start_task=start_task,
    )


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_assistant_owl_draft_is_retry_safe_and_available_only_after_completion(tmp_path, status):
    store = Store(tmp_path / "owl-drafts.db")
    assistant = store.set_assistant(OWNER, True)["bot"]
    chat = store.create_chat(OWNER, assistant["id"], "Shift review")
    task = store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(message="Βρες ποια βάρδια χρειάζεται βοήθεια.", request_id=uuid4().hex),
    )
    draft = AssistantOwlDraftInput.model_validate(
        reviewed_setup().model_dump(exclude={"request_id", "start_task", "playbook"})
    )
    assert store.claim(OWNER)["id"] == task["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, retry = pool.map(
            lambda _: store.propose_assistant_owl(OWNER, task["id"], draft), range(2)
        )
    assert first == retry == draft.model_dump(exclude_defaults=True)
    with pytest.raises(Conflict, match="different owl draft"):
        store.propose_assistant_owl(
            OWNER, task["id"], draft.model_copy(update={"name": "Different owl"})
        )
    assert "assistant_owl_draft" not in store.conversation(OWNER, chat["id"])["tasks"][0]
    assert len(store.list_bots(OWNER)) == 1
    assert store.list_assignments(OWNER, assistant["id"])["items"] == []

    store.finish(OWNER, task["id"], status, content="Έτοιμο για έλεγχο.")
    reopened = Store(store.path)
    (shown_task,) = reopened.conversation_page(OWNER, chat["id"], ConversationPageQuery())["tasks"]
    if status == "completed":
        assert shown_task["assistant_owl_draft"] == draft.model_dump(exclude_defaults=True)
    else:
        assert "assistant_owl_draft" not in shown_task


@pytest.mark.parametrize("first", ["question", "assignment", "draft"])
def test_assistant_can_only_choose_one_way_to_handle_the_turn(tmp_path, first):
    from alysis_code.bots.clarifications import AskQuestion, ClarificationStore

    store = Store(tmp_path / "owl-draft-conflicts.db")
    assistant = store.set_assistant(OWNER, True)["bot"]
    recipient = store.save_bot(OWNER, BotInput(name="Planner"))
    task, assignment = propose(store, assistant, recipient)
    draft = AssistantOwlDraftInput.model_validate(
        reviewed_setup().model_dump(exclude={"request_id", "start_task", "playbook"})
    )
    questions = ClarificationStore(store, OWNER)
    if first == "question":
        questions.ask(task, AskQuestion(question="どのチームが対象ですか？"))
        with pytest.raises(Conflict, match="clarification"):
            store.propose_assistant_owl(OWNER, task["id"], draft)
        with pytest.raises(Conflict, match="clarification"):
            store.propose_assignment(OWNER, task["id"], assignment)
    elif first == "assignment":
        store.propose_assignment(OWNER, task["id"], assignment)
        with pytest.raises(Conflict, match="already has an assignment"):
            store.propose_assistant_owl(OWNER, task["id"], draft)
    else:
        store.propose_assistant_owl(OWNER, task["id"], draft)
        with pytest.raises(Conflict, match="already has a new owl draft"):
            store.propose_assignment(OWNER, task["id"], assignment)


def test_only_managed_assistant_uses_working_notes_with_memory_off(tmp_path):
    store = Store(tmp_path / "owl-working-notes.db")
    assistant = store.set_assistant(OWNER, True)["bot"]
    worker = store.save_bot(OWNER, BotInput(name="Planner", memory_enabled=False))
    note = NoteInput(title="確認した回答", content="対象は東京チームです。")
    assert not assistant["memory_enabled"]
    task, _ = propose(store, assistant, worker)
    assert (
        store.save_note(OWNER, assistant["id"], note, source_task_id=task["id"])["content"]
        == note.content
    )
    store.finish(OWNER, task["id"], "completed", content="回答を保存しました。")
    other_task, _ = propose(store, worker, assistant)
    with pytest.raises(Conflict, match="working notes"):
        store.save_note(OWNER, worker["id"], note, source_task_id=other_task["id"])


def test_assistant_owl_approval_is_owner_scoped_and_retry_safe(tmp_path):
    store = Store(tmp_path / "approved-owl.db")
    assistant = store.set_assistant(OWNER, True)["bot"]
    setup = reviewed_setup()
    assert len(store.list_bots(OWNER)) == 1
    assert store.claim(OWNER) is None
    with pytest.raises(NotFound):
        store.approve_assistant_owl("other owner", assistant["id"], setup)
    with ThreadPoolExecutor(max_workers=2) as users:
        first, retry = users.map(
            lambda _: store.approve_assistant_owl(OWNER, assistant["id"], setup), range(2)
        )
    assert first == retry
    assert len(store.list_bots(OWNER)) == 2
    child = store.claim(OWNER)
    assert child["id"] == first["assignment"]["child_task_id"]
    assert child["bot_id"] == first["bot"]["id"]
    assert store.claim(OWNER) is None
    assert store.assignment_context(OWNER, child["id"])["brief"] == setup.brief
    with pytest.raises(Conflict):
        store.approve_assistant_owl(
            OWNER, assistant["id"], setup.model_copy(update={"start_task": False})
        )
    store.set_assistant(OWNER, False)
    with pytest.raises(Conflict):
        store.approve_assistant_owl(OWNER, assistant["id"], reviewed_setup())


def test_assistant_owl_can_be_created_without_starting_and_rollback_is_atomic(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "approved-owl.db")
    assistant = store.set_assistant(OWNER, True)["bot"]
    setup = reviewed_setup(start_task=False)
    created = store.approve_assistant_owl(OWNER, assistant["id"], setup)
    assert created["assignment"] is None
    assert store.approve_assistant_owl(OWNER, assistant["id"], setup) == created
    assert store.claim(OWNER) is None

    def refuse(*_, **_kwargs):
        raise LimitReached("No assignment can be queued.")

    monkeypatch.setattr(store, "_enqueue_assignment_child", refuse)
    with pytest.raises(LimitReached):
        store.approve_assistant_owl(OWNER, assistant["id"], reviewed_setup())
    assert len(store.list_bots(OWNER)) == 2
    assert store.list_assignments(OWNER, assistant["id"])["items"] == []


def test_assistant_owl_creation_api_publishes_result_in_recipient_chat(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    store = Store(tmp_path / "approved-owl.db")
    assistant = store.set_assistant(OWNER, True)["bot"]
    settings = Settings(
        store.path, "t" * 40, "fixture", "https://example.invalid", "fixture", owner=OWNER
    )
    app = create_app(settings, task_store=store)
    monkeypatch.setattr(type(app.state.runtime), "is_running", property(lambda _: True))
    http = TestClient(app)
    setup = reviewed_setup().model_dump()
    path = f"/v1/assistant/{assistant['id']}/create-owl"
    assert http.post(path, json=setup).status_code == 401
    assert len(store.list_bots(OWNER)) == 1

    http.headers["Authorization"] = "Bearer " + settings.token
    response = http.post(path, json=setup)
    assert response.status_code == 201
    receipt = response.json()
    assert receipt["assignment"]["recipient_bot_id"] == receipt["bot"]["id"]
    assert receipt["assignment"]["from_assist_offer"] is True
    child = store.claim(OWNER)
    result = "Χρειάζεται ακόμη ένα άτομο στην πρωινή βάρδια."
    verdict = {"status": "met", "detail": "Identifies the uncovered shift."}
    store.finish(
        OWNER,
        child["id"],
        "completed",
        content=result,
        publish_reply=False,
        assignment_verification=verdict,
    )
    saved = http.get(f"/v1/bots/{assistant['id']}/assignments/{receipt['assignment']['id']}")
    assert saved.status_code == 200
    assert saved.json()["result"] == result
    bot_id = receipt["bot"]["id"]
    (chat,) = http.get(f"/v1/bots/{bot_id}/chats").json()
    path = f"/v1/chats/{chat['id']}"
    conversation = http.get(path).json()
    (message,) = conversation["messages"]
    assert (message["role"], message["content"], message["task_id"]) == (
        "assistant",
        result,
        child["id"],
    )
    (task,) = conversation["tasks"]
    assert task["assistant_handoff"] == {
        "brief": setup["brief"],
        "expected_result": setup["expected_result"],
        "verification": verdict,
    }
    worker = next(bot for bot in http.get("/v1/bots").json() if bot["id"] == bot_id)
    assert worker["unread_chats"] == 1
    assert worker["latest_activity"]["assistant_handoff"] is True
    assert worker["latest_activity"]["task_id"] == child["id"]
    assert http.post(path + "/read", json={"activity_seq": conversation["activity_seq"]}).is_success
    assert http.get(path).json()["unread"] is False


@pytest.mark.parametrize(
    "lineage,version", [("release", 52), ("assignments", 52), ("assignments", 59)]
)
def test_divergent_owl_schemas_upgrade_and_reopen(tmp_path, lineage, version):
    path = tmp_path / "upgrade.db"
    store = Store(path)
    bot = store.save_bot(OWNER, BotInput(name="Καταγραφή", model="chosen", reasoning_effort="high"))
    bot_id = bot["id"]
    chat = store.create_chat(OWNER, bot_id, "Dispatch")
    task = store.enqueue(OWNER, chat["id"], TaskInput(message="保留", request_id=uuid4().hex))
    assert store.claim(OWNER)["id"] == task["id"]
    asset = store.save_bytes(OWNER, bot_id, "notes.txt", b"confirmed", source_task_id=task["id"])
    store.finish(OWNER, task["id"], "completed", content="Checked.")
    if lineage == "assignments":
        store.set_coordinator(OWNER, bot_id, True)
    # Version 52 could have effort or assignments. Later assignment previews still lacked effort.
    with store.connect(write=True) as db:
        if lineage == "release":
            db.execute("DROP TABLE owl_coordinators")
            db.execute("ALTER TABLE tasks DROP COLUMN conversation_visible")
        else:
            db.execute("ALTER TABLE bots DROP COLUMN reasoning_effort")
        db.execute(f"PRAGMA user_version={version}")
    for _ in range(2):
        store = Store(path)
        saved = store.bot(OWNER, bot_id)
        assert saved["model"] == "chosen"
        assert saved["reasoning_effort"] == ("high" if lineage == "release" else None)
        assert store.is_coordinator(OWNER, bot_id) == (lineage == "assignments")
        assert store.conversation(OWNER, chat["id"])["tasks"][0]["id"] == task["id"]
        assert store.file_data(OWNER, bot_id, asset["id"]) == b"confirmed"
        with store.connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION > 59
            assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_existing_assistant_handoff_is_backfilled_once(tmp_path):
    store = Store(tmp_path / "existing-handoff.db")
    assistant = store.set_assistant(OWNER, True)["bot"]
    receipt = store.approve_assistant_owl(OWNER, assistant["id"], reviewed_setup())
    child = store.claim(OWNER)
    assert child["id"] == receipt["assignment"]["child_task_id"]
    store.finish(OWNER, child["id"], "completed", content="Saved handoff.", publish_reply=False)
    # Model the prior schema, where the result was stored only on the assignment task.
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET conversation_visible=0 WHERE id=?", (child["id"],))
        db.execute("DELETE FROM messages WHERE task_id=?", (child["id"],))
        db.execute("PRAGMA user_version=45")
    for _ in range(2):
        store = Store(store.path)
        conversation = store.conversation(OWNER, child["chat_id"])
        assert conversation["unread"]
        (message,) = conversation["messages"]
        assert message["content"] == "Saved handoff."
        assert message["task_id"] == child["id"]


@pytest.fixture
def owls(tmp_path):
    store = Store(tmp_path / "assignments.db")
    source = store.save_bot(OWNER, BotInput(name="Coordinator"))
    worker = store.save_bot(OWNER, BotInput(name="Νεφέλη", instructions="Use the agreed format."))
    outsider = store.save_bot("another owner", BotInput(name="Unrelated"))
    return store, source, worker, outsider


def propose(store, source, worker):
    chat = store.create_chat(OWNER, source["id"], "Assign work")
    task = store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(message="Σύγκρινε τις δύο περιγραφές.", request_id=uuid4().hex),
    )
    assert store.claim(OWNER)["id"] == task["id"]
    data = AssignmentInput(
        recipient_bot_id=worker["id"],
        brief="Σύγκρινε τις δύο περιγραφές.",
        expected_result="A summary in Greek with both names preserved.",
    )
    return task, data


@pytest.mark.parametrize("object_backed", [False, True], ids=["database", "objects"])
def test_file_handoff_pins_revisions_and_returns_reviewed_bytes_once(tmp_path, object_backed):
    from alysis_code.bots.object_storage import LocalObjectStorage
    from alysis_code.bots.retention import ObjectRetention

    objects = LocalObjectStorage(tmp_path / "objects") if object_backed else None
    store = Store(tmp_path / "handoff.db", objects=objects)
    lead = store.save_bot(OWNER, BotInput(name="Coordinator"))
    worker = store.save_bot(OWNER, BotInput(name="Reviewer"))
    store.set_coordinator(OWNER, lead["id"], True)
    ledger = b"id,amount\nA,42\n"
    first = store.save_bytes(OWNER, lead["id"], "Ledger.csv", ledger)
    latest = store.save_bytes(
        OWNER, lead["id"], "Ledger.csv", b"A,999", previous_file_id=first["id"]
    )
    collision = store.save_bytes(OWNER, worker["id"], "LEDGER.CSV", b"worker's own file")
    previous_report = store.save_bytes(OWNER, lead["id"], "report.xlsx", b"older report")
    store.save_bytes(OWNER, lead["id"], "Report (2).xlsx", b"another existing report")
    parent, proposal = propose(store, lead, worker)
    proposal = proposal.model_copy(
        update={"work_scope": "workbook", "input_file_ids": [first["id"]]}
    )
    assignment = store.propose_assignment(OWNER, parent["id"], proposal)
    assert [f["id"] for f in assignment["input_files"]] == [first["id"]]
    assert store.list_files(OWNER, worker["id"])[0]["id"] == collision["id"]
    assert len(store.list_files(OWNER, worker["id"])) == 1
    store.finish(OWNER, parent["id"], "completed", content="Ready for your approval.")

    def approve(ids):
        return store.decide_assignment(
            OWNER, lead["id"], assignment["id"], "approve", "workbook", ids
        )

    with pytest.raises(Conflict, match="Review the selected"):
        approve(None)
    before = store.library_usage(OWNER)
    store.set_library_limit(OWNER, before["used_bytes"] + len(ledger) - 1)
    with pytest.raises(LibraryCapacityExceeded):
        approve([first["id"]])
    assert store.read_assignment(OWNER, lead["id"], assignment["id"])["child_task_id"] is None
    assert store.library_usage(OWNER)["file_count"] == before["file_count"]
    store.set_library_limit(OWNER, None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        approved, retried = pool.map(approve, [[first["id"]], [first["id"]]])
    assert approved == retried
    assert approved["input_files"] == assignment["input_files"]
    with pytest.raises(Conflict, match="different file revisions"):
        approve([latest["id"]])
    assert store.library_usage(OWNER)["used_bytes"] == before["used_bytes"] + len(ledger)
    child = store.claim(OWNER)
    assert child["id"] == approved["child_task_id"]
    store = Store(store.path, objects=objects)
    (shared,) = store.assignment_context(OWNER, child["id"])["input_files"]
    assert shared["id"] != first["id"] and shared["name"] == "Ledger (2).csv"
    assert shared["source_file_id"] == first["id"] and shared["source_revision"] == 1
    assert (
        shared["source_bot_id"] == lead["id"]
        and shared["sha256"] == hashlib.sha256(ledger).hexdigest()
    )
    store.delete_item(OWNER, lead["id"], latest["id"], kind="files")
    if object_backed:
        retention = ObjectRetention(store, OWNER)
        retention.sweep()
        retention.sweep(now=time.time() + 100000)
    assert store.assignment_input_data(OWNER, child["id"], shared["id"]) == ledger
    assert (
        b"".join(store.assignment_input_chunks(OWNER, child["id"], shared["id"], offset=4))
        == ledger[4:]
    )
    for unrelated in (collision["id"], first["id"], previous_report["id"]):
        with pytest.raises(NotFound):
            store.assignment_input_data(OWNER, child["id"], unrelated)
    with pytest.raises(NotFound):
        store.assignment_input_data("different owner", child["id"], shared["id"])

    draft = store.save_bytes(
        OWNER, worker["id"], "draft.xlsx", b"unapproved", source_task_id=child["id"]
    )
    contents = b"PK\x03\x04reviewed version"
    result = store.save_bytes(
        OWNER, worker["id"], "Report.xlsx", contents, source_task_id=child["id"]
    )
    for unrelated in (shared["id"], collision["id"], previous_report["id"]):
        with pytest.raises(Conflict):
            store.checkpoint_assignment_result(
                OWNER, child["id"], "Wrong file", output_file_ids=[unrelated]
            )
    store.checkpoint_assignment_result(
        OWNER, child["id"], "Reviewed", output_file_ids=[result["id"]]
    )
    reviewed = Store(store.path, objects=objects).assignment_context(OWNER, child["id"])
    assert (
        reviewed["checkpoint_file_ids"] == [result["id"]]
        and reviewed["checkpoint_result"] == "Reviewed"
    )
    verdict = {"status": "met", "detail": "Reviewed the selected workbook."}

    def finish(ids):
        store.finish(
            OWNER,
            child["id"],
            "completed",
            assignment_verification=verdict,
            assignment_output_file_ids=ids,
        )

    with pytest.raises(Conflict, match="differ"):
        finish([draft["id"]])
    before = store.library_usage(OWNER)
    store.set_library_limit(OWNER, before["used_bytes"] + len(contents) - 1)
    with pytest.raises(LibraryCapacityExceeded):
        finish([result["id"]])
    assert store.task(OWNER, child["id"])["status"] == "running"
    assert store.claim(OWNER) is None
    assert store.library_usage(OWNER)["file_count"] == before["file_count"]
    store.set_library_limit(OWNER, None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish, [[result["id"]], [result["id"]]]))
    assert store.library_usage(OWNER)["file_count"] == before["file_count"] + 1
    resume = store.claim(OWNER)
    assert resume["request_id"] == "assignment-return-" + assignment["id"]
    assert resume["bot_id"] == lead["id"] and resume["chat_id"] == parent["chat_id"]
    assert store.claim(OWNER) is None
    delivery = resume["config"]["assignment_return"]
    assert delivery["verification"] == verdict and delivery["child_task_id"] == child["id"]
    (received,) = delivery["returned_files"]
    persisted = store.read_assignment(OWNER, lead["id"], assignment["id"])
    assert persisted["returned_files"] == [received] and persisted["artifact_id"] == result["id"]
    assert received["source_file_id"] == result["id"] and received["source_bot_id"] == worker["id"]
    assert received["id"] != result["id"] and received["name"] == "Report (3).xlsx"
    assert store.file_data(OWNER, lead["id"], previous_report["id"]) == b"older report"
    store.delete_item(OWNER, worker["id"], result["id"], kind="files")
    if object_backed:
        retention.sweep()
        retention.sweep(now=time.time() + 100000)
    assert store.file_data(OWNER, lead["id"], received["id"]) == contents
    cards = store.files_page(OWNER, lead["id"], FileQuery(source_task_id=resume["id"]))
    assert [f["id"] for f in cards["items"]] == [received["id"]]
    store.finish(OWNER, resume["id"], "completed", content="The reviewed workbook is attached.")
    page = store.conversation_page(OWNER, parent["chat_id"], ConversationPageQuery())
    assert next(t for t in page["tasks"] if t["id"] == resume["id"])["file_count"] == 1
    assert [m["role"] for m in page["messages"]].count("user") == 1


@pytest.mark.parametrize(
    "outcome",
    [
        "decline",
        "cancelled_proposal",
        "deleted",
        "failed",
        "cancelled",
        "needs_review",
        "unavailable",
    ],
)
def test_rejected_sources_and_unsuccessful_reviews_never_deliver(owls, outcome):
    store, lead, worker, outsider = owls
    store.set_coordinator(OWNER, lead["id"], True)
    source = store.save_bytes(OWNER, lead["id"], "data.csv", b"A,1")
    local = store.save_bytes(OWNER, worker["id"], "mine.csv", b"A,2")
    foreign = store.save_bytes("another owner", outsider["id"], "private.csv", b"A,3")
    parent, proposal = propose(store, lead, worker)
    proposal = proposal.model_copy(
        update={"work_scope": "workbook", "input_file_ids": [source["id"]]}
    )
    assignment = store.propose_assignment(OWNER, parent["id"], proposal)
    store.finish(
        OWNER, parent["id"], "cancelled" if outcome == "cancelled_proposal" else "completed"
    )
    for unowned in (local["id"], foreign["id"]):
        with pytest.raises(Conflict):
            store.decide_assignment(
                OWNER, lead["id"], assignment["id"], "approve", "workbook", [unowned]
            )
    if outcome == "deleted":
        store.delete_item(OWNER, lead["id"], source["id"], kind="files")
    if outcome in {"decline", "cancelled_proposal", "deleted"}:
        if outcome == "decline":
            store.decide_assignment(OWNER, lead["id"], assignment["id"], "decline")
        with pytest.raises(Conflict):
            store.decide_assignment(
                OWNER, lead["id"], assignment["id"], "approve", "workbook", [source["id"]]
            )
        assert [f["id"] for f in store.list_files(OWNER, worker["id"])] == [local["id"]]
    else:
        store.decide_assignment(
            OWNER, lead["id"], assignment["id"], "approve", "workbook", [source["id"]]
        )
        child = store.claim(OWNER)
        selected = store.save_bytes(
            OWNER, worker["id"], "Draft.xlsx", b"draft", source_task_id=child["id"]
        )["id"]
        store.checkpoint_assignment_result(
            OWNER, child["id"], "Candidate", output_file_ids=[selected]
        )
        if outcome == "cancelled":
            store.cancel(OWNER, child["id"])
        before = store.conversation_page(OWNER, parent["chat_id"], ConversationPageQuery())
        status = "failed" if outcome == "failed" else "completed"
        verdict = {"status": outcome if outcome in {"needs_review", "unavailable"} else "met"}
        store.finish(
            OWNER,
            child["id"],
            status,
            assignment_verification=verdict,
            assignment_output_file_ids=[selected],
        )
        assert [f["id"] for f in store.list_files(OWNER, lead["id"])] == [source["id"]]
        if outcome in {"needs_review", "unavailable"}:
            after = store.conversation_page(
                OWNER,
                parent["chat_id"],
                ConversationPageQuery(
                    version=before["version"], after=before["messages"][-1]["id"]
                ),
            )
            assert after["activity_seq"] > before["activity_seq"] and not after["unchanged"]
            (changed,) = after["tasks"]
            assert changed["id"] == parent["id"] and changed["assignment_reviews"] == [
                {
                    "id": assignment["id"],
                    "recipient_name": worker["name"],
                    "recipient_bot_id": worker["id"],
                    "child_task_id": child["id"],
                    "status": outcome,
                    "detail": None,
                }
            ]
            store.mark_chat_read(OWNER, parent["chat_id"], after["activity_seq"])
            recipient_chat = store.conversation(OWNER, child["chat_id"])
            assert (
                recipient_chat["tasks"][-1]["assistant_handoff"]["verification"]["status"]
                == outcome
            )
    item = store.read_assignment(OWNER, lead["id"], assignment["id"])
    assert "returned_files" not in item and "artifact_id" not in item
    assert store.claim(OWNER) is None


def test_workbook_repair_is_claimed_once_and_recovers_only_its_own_successor(owls):
    store, lead, worker, _ = owls
    store.set_coordinator(OWNER, lead["id"], True)
    parent, data = propose(store, lead, worker)
    assignment = store.propose_assignment(
        OWNER, parent["id"], data.model_copy(update={"work_scope": "workbook"})
    )
    store.finish(OWNER, parent["id"], "completed")
    store.decide_assignment(OWNER, lead["id"], assignment["id"], "approve", "workbook")
    child = store.claim(OWNER)["id"]
    base = store.save_bytes(OWNER, worker["id"], "Report.xlsx", b"before", source_task_id=child)[
        "id"
    ]
    different = store.save_bytes(OWNER, worker["id"], "Draft.xlsx", b"draft", source_task_id=child)[
        "id"
    ]
    verdict = {"status": "met"}

    def finish(file_id):
        store.finish(
            OWNER,
            child,
            "completed",
            assignment_output_file_ids=[file_id],
            assignment_verification=verdict,
        )

    with pytest.raises(Conflict, match="for review"):
        finish(base)
    store.checkpoint_assignment_result(OWNER, child, "No files", output_file_ids=[])
    with pytest.raises(Conflict, match="differ"):
        finish(base)
    store.checkpoint_assignment_result(OWNER, child, "Needs work", output_file_ids=[base])
    with pytest.raises(Conflict, match="exact workbook"):
        store.begin_assignment_repair(OWNER, child, different, "Replace the total.")

    def claim_repair(_):
        return store.begin_assignment_repair(OWNER, child, base, "Replace the total.")

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim_repair, range(2))) == [False, True]
    assert Store(store.path).assignment_context(OWNER, child)["repair"] == {
        "file_id": base,
        "reason": "Replace the total.",
    }
    with pytest.raises(Conflict, match="repaired workbook"):
        finish(base)
    successor = store.save_bytes(
        OWNER, worker["id"], "Report.xlsx", b"repaired", previous_file_id=base, source_task_id=child
    )["id"]
    recovered = Store(store.path).assignment_context(OWNER, child)
    assert recovered["repair"]["saved_file_id"] == successor and recovered[
        "checkpoint_file_ids"
    ] == [base]
    store.checkpoint_assignment_result(OWNER, child, "Unrelated", output_file_ids=[different])
    with pytest.raises(Conflict, match="repaired workbook"):
        finish(different)
    store.checkpoint_assignment_result(OWNER, child, "Repaired", output_file_ids=[successor])
    finish(successor)
    (released,) = store.read_assignment(OWNER, lead["id"], assignment["id"])["returned_files"]
    assert released["source_file_id"] == successor and released["source_revision"] == 2
    assert store.file_data(OWNER, lead["id"], released["id"]) == b"repaired"


def test_computer_preserves_recipient_local_files_and_explicit_coordinator_selection(owls):
    from alysis_code.bots.models import AssignmentAccess

    store, lead, worker, _ = owls
    store.set_coordinator(OWNER, lead["id"], True)
    selected = store.save_bytes(OWNER, lead["id"], "selected.csv", b"coordinator")["id"]
    other = store.save_bytes(OWNER, lead["id"], "other.csv", b"not selected")["id"]
    local = store.save_bytes(OWNER, worker["id"], "existing.csv", b"recipient")["id"]
    parent, data = propose(store, lead, worker)
    proposal = data.model_copy(
        update={
            "work_scope": "computer",
            "access": AssignmentAccess(offline_computer=True, file_ids=[local]),
            "input_file_ids": [other],
        }
    )
    assignment = store.propose_assignment(OWNER, parent["id"], proposal)
    store.finish(OWNER, parent["id"], "completed")
    # The reviewing person can replace the suggestion with another specific coordinator file.
    approved = store.decide_assignment(
        OWNER, lead["id"], assignment["id"], "approve", "computer", [selected]
    )
    child = store.claim(OWNER)
    assert child["id"] == approved["child_task_id"]
    (shared,) = store.assignment_context(OWNER, child["id"])["input_files"]
    assert child["config"]["assignment_access"]["file_ids"] == [local, shared["id"]]
    assert [f["source_file_id"] for f in approved["input_files"]] == [selected]
    assert store.assignment_input_data(OWNER, child["id"], shared["id"]) == b"coordinator"
    for file_id in (other, local):
        with pytest.raises(NotFound):
            store.assignment_input_data(OWNER, child["id"], file_id)


@pytest.mark.parametrize("decision", ["approve", "decline", "coordinator_changed"])
def test_pending_review_stays_on_its_original_turn_and_clears_in_paging_deltas(owls, decision):
    store, lead, worker, _ = owls
    store.set_coordinator(OWNER, lead["id"], True)
    parent, data = propose(store, lead, worker)
    assignment = store.propose_assignment(OWNER, parent["id"], data)
    chat = parent["chat_id"]

    def page(**query):
        return store.conversation_page(OWNER, chat, ConversationPageQuery(limit=2, **query))

    before = page()
    assert before["tasks"][0]["pending_assignments"] == []
    store.finish(OWNER, parent["id"], "completed", content="Please review the assignment.")
    ready = page(version=before["version"], after=before["messages"][-1]["id"])
    receipt = [{"id": assignment["id"], "recipient_name": worker["name"]}]
    assert (
        ready["tasks"][0]["id"] == parent["id"]
        and ready["tasks"][0]["pending_assignments"] == receipt
    )
    assert set(receipt[0]) == {"id", "recipient_name"}
    newer = store.enqueue(
        OWNER, chat, TaskInput(message="A separate request", request_id=uuid4().hex)
    )
    assert store.claim(OWNER)["id"] == newer["id"]
    store.finish(OWNER, newer["id"], "completed", content="That separate request is done.")
    moved = page(version=ready["version"], after=ready["messages"][-1]["id"])
    assert parent["id"] in moved["version"]
    assert next(t for t in moved["tasks"] if t["id"] == newer["id"])["pending_assignments"] == []
    store = Store(store.path)
    earlier = page(before=page()["earlier"])
    assert (
        next(t for t in earlier["tasks"] if t["id"] == parent["id"])["pending_assignments"]
        == receipt
    )
    if decision == "coordinator_changed":
        store.set_coordinator(OWNER, worker["id"], True)
    else:
        store.decide_assignment(OWNER, lead["id"], assignment["id"], decision)
    closed = page(version=moved["version"], after=moved["messages"][-1]["id"])
    assert closed["continues"] and not closed["unchanged"] and not closed["messages"]
    assert [(t["id"], t["pending_assignments"]) for t in closed["tasks"]] == [(parent["id"], [])]
    assert parent["id"] not in closed["version"]
    assert all(t["pending_assignments"] == [] for t in store.conversation(OWNER, chat)["tasks"])


def test_assignment_capability_and_approval_api(owls, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    store, source, worker, outsider = owls
    settings = Settings(
        store.path, "t" * 40, "fixture", "https://example.invalid", "fixture", owner=OWNER
    )
    app = create_app(settings)
    runtime = app.state.runtime
    monkeypatch.setattr(type(runtime), "is_running", property(lambda _: True))
    client = TestClient(app)
    path = f"/v1/bots/{source['id']}"
    assert client.get(path + "/assignments").status_code == 401
    assert client.put(path + "/coordinator", json={"enabled": True}).status_code == 401
    client.headers["Authorization"] = "Bearer " + settings.token
    capabilities = client.get("/v1/status").json()["capabilities"]
    assert "owl_assignments" in capabilities and "owl_taught_steps" in capabilities
    assert client.put(path + "/coordinator", json={"enabled": 1}).status_code == 422
    assert client.put(path + "/coordinator", json={"enabled": True}).json() == {"coordinator": True}
    task, data = propose(store, source, worker)
    assignment = store.propose_assignment(OWNER, task["id"], data)
    assert client.get(path + "/assignments").json() == {
        "coordinator": True,
        "items": [assignment],
        "next_before": None,
    }
    approval = path + f"/assignments/{assignment['id']}"
    assert client.patch(approval, json={"decision": "approve"}).status_code == 409
    assert client.get(f"/v1/bots/{outsider['id']}/assignments").status_code == 404
    assert client.post(path + "/assignments/page", json={"before": assignment["id"]}).json() == {
        "coordinator": True,
        "items": [],
        "next_before": None,
    }
    assert (
        client.post(
            f"/v1/bots/{worker['id']}/assignments/page", json={"before": assignment["id"]}
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"/v1/bots/{worker['id']}/assignments/{assignment['id']}",
            json={"decision": "approve"},
        ).status_code
        == 404
    )
    store.finish(OWNER, task["id"], "completed", content="Η ανάθεση είναι έτοιμη.")
    assert client.get(path + "/assignments").json()["items"][0]["ready"] is True
    queued = client.patch(approval, json={"decision": "approve"}).json()
    assert queued["status"] == "queued"
    assert queued["ready"] is False
    assert client.patch(approval, json={"decision": "approve"}).json() == queued
    assert client.patch(approval, json={"decision": "decline"}).status_code == 409
    reclaimed = []
    runtime.desktop_computers = SimpleNamespace(
        factory=SimpleNamespace(
            spec=SimpleNamespace(suspend_mode="Memory"),
            recover=lambda bot_id, **_: reclaimed.append(bot_id),
        ),
    )
    assert client.delete(path).status_code == 409
    assert reclaimed == []
    assert (
        client.post(f"/v1/tasks/{queued['child_task_id']}/cancel").json()["status"] == "cancelled"
    )
    assert client.get(path + "/assignments").json()["items"][0]["status"] == "cancelled"


def test_coordinator_switch_cancels_only_proposals_and_rejects_outside_scope(owls):
    store, source, worker, outsider = owls
    task, data = propose(store, source, worker)
    with pytest.raises(Conflict, match="coordinating owl"):
        store.propose_assignment(OWNER, task["id"], data)
    store.set_coordinator(OWNER, source["id"], True)
    with pytest.raises(NotFound):
        store.propose_assignment(
            OWNER, task["id"], data.model_copy(update={"recipient_bot_id": outsider["id"]})
        )
    with pytest.raises(Conflict, match="another owl"):
        store.propose_assignment(
            OWNER, task["id"], data.model_copy(update={"recipient_bot_id": source["id"]})
        )
    first = store.propose_assignment(OWNER, task["id"], data)
    assert store.propose_assignment(OWNER, task["id"], data) == first
    with pytest.raises(Conflict, match="different assignment"):
        store.propose_assignment(OWNER, task["id"], data.model_copy(update={"brief": "Changed"}))
    store.finish(OWNER, task["id"], "completed")
    approved = store.decide_assignment(OWNER, source["id"], first["id"], "approve")
    assert store.claim(OWNER)["id"] == approved["child_task_id"]
    later_task, later_data = propose(store, source, worker)
    pending = store.propose_assignment(OWNER, later_task["id"], later_data)
    store.finish(OWNER, later_task["id"], "completed")
    store.set_coordinator(OWNER, worker["id"], True)
    view = store.list_assignments(OWNER, source["id"])
    assert view["coordinator"] is False
    assert [(item["id"], item["status"]) for item in view["items"]] == [
        (pending["id"], "cancelled"),
        (first["id"], "running"),
    ]
    store.set_coordinator(OWNER, source["id"], True)
    with pytest.raises(Conflict, match="already been decided"):
        store.decide_assignment(OWNER, source["id"], pending["id"], "approve")


@pytest.mark.parametrize("stop", ["restart", "cancel"])
def test_assignment_approval_retries_share_one_child_and_preserve_unverified_work(owls, stop):
    store, source, worker, _ = owls
    store.set_coordinator(OWNER, source["id"], True)
    task, data = propose(store, source, worker)
    assignment = store.propose_assignment(OWNER, task["id"], data)
    store.finish(OWNER, task["id"], "completed")

    def approve():
        return store.decide_assignment(OWNER, source["id"], assignment["id"], "approve")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(approve)
        b = pool.submit(approve)
        first, retried = a.result(), b.result()
    assert first == retried
    child_id = first["child_task_id"]
    store = Store(store.path)
    child = store.claim(OWNER)
    assert child["id"] == child_id and child["config"]["assignment_id"] == assignment["id"]
    assert store.claim(OWNER) is None
    assert store.conversation(OWNER, child["chat_id"])["messages"] == []
    assert store.conversation_page(OWNER, child["chat_id"], ConversationPageQuery())["tasks"] == []
    with store.connect() as db, pytest.raises(Conflict):
        store.direct_request(db, OWNER, worker["id"], child_id)
    assert store.assignment_context(OWNER, child_id) == {
        "coordinator_bot_id": source["id"],
        "input_files": [],
        "checkpoint_result": "",
        "repair": None,
        "checkpoint_file_ids": [],
        "selected_output_file_ids": [],
        "recipient_name": worker["name"],
        "brief": data.brief,
        "expected_result": data.expected_result,
    }
    with pytest.raises(LimitReached, match="size limit"):
        store.checkpoint_assignment_result(OWNER, child_id, "🦉" * (ASSIGNMENT_RESULT_BYTES // 4))
    store.checkpoint_assignment_result(OWNER, child_id, "Το αποτέλεσμα.")
    if stop == "cancel":
        assert store.cancel(OWNER, child_id)["status"] == "cancelling"
        store.finish(OWNER, child_id, "completed")
    restarted = Store(store.path)
    assert restarted.recover(OWNER) == (1 if stop == "restart" else 0)
    if stop == "restart":
        # Nothing it did reached outside the owl, so it starts again once from the approved
        # brief (Store._cut_by_restart); a second restart leaves it interrupted.
        again = restarted.claim(OWNER)
        assert again["id"] == child_id and again["restarts"] == 1
        restarted = Store(store.path)
        assert restarted.recover(OWNER) == 1
    assert restarted.claim(OWNER) is None
    item = restarted.list_assignments(OWNER, source["id"])["items"][0]
    assert item["status"] == ("interrupted" if stop == "restart" else "cancelled")
    assert item["result"] == "Το αποτέλεσμα."
    assert item["verification"] is None
    assert restarted.needs_you(OWNER)["items"] == []
    assert restarted.conversation(OWNER, child["chat_id"])["tasks"] == []
    assert (
        restarted.conversation_page(OWNER, child["chat_id"], ConversationPageQuery())["tasks"] == []
    )


def test_assignment_completion_keeps_result_and_verdict_out_of_recipient_chat(owls):
    store, source, worker, _ = owls
    worker_chat = store.create_chat(OWNER, worker["id"], "Earlier request")["id"]
    earlier = store.enqueue(
        OWNER, worker_chat, TaskInput(message="Check this.", request_id=uuid4().hex)
    )
    assert store.claim(OWNER)["id"] == earlier["id"]
    store.finish(OWNER, earlier["id"], "failed", error="The check stopped.")
    store.set_coordinator(OWNER, source["id"], True)
    task, data = propose(store, source, worker)
    assignment = store.propose_assignment(OWNER, task["id"], data)
    store.finish(OWNER, task["id"], "completed", content="Ready")
    child_id = store.decide_assignment(OWNER, source["id"], assignment["id"], "approve")[
        "child_task_id"
    ]
    assert store.claim(OWNER)["id"] == child_id
    with pytest.raises(Conflict, match="active assignments"):
        store.delete_bot(OWNER, source["id"])
    store.checkpoint_assignment_result(OWNER, child_id, "Both names checked.")
    verdict = {"status": "met", "detail": "Both names are mentioned."}
    store.finish(OWNER, child_id, "completed", assignment_verification=verdict)
    restored = Store(store.path)
    item = restored.list_assignments(OWNER, source["id"])["items"][0]
    assert (item["status"], item["result"], item["verification"]) == (
        "completed",
        "Both names checked.",
        verdict,
    )
    assert restored.task(OWNER, child_id)["chat_id"] == worker_chat
    conversation = restored.conversation(OWNER, worker_chat)
    assert [message["content"] for message in conversation["messages"]] == ["Check this."]
    assert [entry["id"] for entry in conversation["tasks"]] == [earlier["id"]]
    worker_row = next(bot for bot in restored.list_bots(OWNER) if bot["id"] == worker["id"])
    assert worker_row["latest_activity"]["task_id"] == earlier["id"]
    assert [item["task_id"] for item in restored.needs_you(OWNER)["items"]] == [earlier["id"]]
    restored.delete_bot(OWNER, source["id"])
    assert restored.task(OWNER, child_id)["output"] == ""
    assert restored.task(OWNER, child_id)["events"] == []
    for page in (
        restored.conversation(OWNER, worker_chat),
        restored.conversation_page(OWNER, worker_chat, ConversationPageQuery()),
    ):
        assert [entry["id"] for entry in page["tasks"]] == [earlier["id"]]
    assert [item["task_id"] for item in restored.needs_you(OWNER)["items"]] == [earlier["id"]]
    assert restored.list_assignments(OWNER, worker["id"]) == {
        "coordinator": False,
        "items": [],
        "next_before": None,
    }


def finished_assignment(store, source, worker, **proposal):
    task, data = propose(store, source, worker)
    assignment = store.propose_assignment(
        OWNER, task["id"], OwlAssignmentProposal(**data.model_dump(), **proposal)
    )
    store.finish(OWNER, task["id"], "completed", content="Ready")
    child_id = store.decide_assignment(OWNER, source["id"], assignment["id"], "approve")[
        "child_task_id"
    ]
    assert store.claim(OWNER)["id"] == child_id
    store.checkpoint_assignment_result(OWNER, child_id, "Done.")
    store.finish(OWNER, child_id, "completed")
    return assignment


def test_a_proposal_says_which_finished_assignment_it_repeats(owls):
    store, source, worker, _ = owls
    other = store.save_bot(OWNER, BotInput(name="Άλλη"))
    store.set_coordinator(OWNER, source["id"], True)
    earlier = finished_assignment(store, source, worker)
    assert "repeats" not in earlier
    # The same job with new data: the coordinator names the earlier one, and the item says so.
    again = finished_assignment(store, source, worker, repeats=earlier["id"])
    assert again["repeats"] == earlier["id"]
    listed = {item["id"]: item for item in store.list_assignments(OWNER, source["id"])["items"]}
    assert listed[again["id"]]["repeats"] == earlier["id"]
    assert "repeats" not in listed[earlier["id"]]
    assert store.read_assignment(OWNER, source["id"], again["id"])["repeats"] == earlier["id"]

    # Only a finished assignment this coordinator gave the same owl counts; anything else is
    # dropped and the handoff is still proposed.
    task, data = propose(store, source, worker)
    waiting = store.propose_assignment(
        OWNER, task["id"], OwlAssignmentProposal(**data.model_dump())
    )
    store.finish(OWNER, task["id"], "completed", content="Ready")
    for recipient, repeats in (
        (worker, waiting["id"]),
        (worker, "0" * 32),
        (worker, "not an id"),
        (other, earlier["id"]),
    ):
        task, data = propose(store, source, recipient)
        item = store.propose_assignment(
            OWNER, task["id"], OwlAssignmentProposal(**data.model_dump(), repeats=repeats)
        )
        assert item["status"] == "proposed"
        assert "repeats" not in item
        store.finish(OWNER, task["id"], "completed", content="Ready")


def test_only_current_authenticated_requests_can_create_proposals(owls):
    store, source, worker, _ = owls
    store.set_coordinator(OWNER, source["id"], True)
    intro = store.start_intro(OWNER, source["id"], IntroInput(request_id=uuid4().hex))
    assert store.claim(OWNER)["id"] == intro["id"]
    assignment = AssignmentInput(
        recipient_bot_id=worker["id"], brief="Prepare a draft", expected_result="A draft"
    )
    with pytest.raises(Conflict):
        store.propose_assignment(OWNER, intro["id"], assignment)
    store.finish(OWNER, intro["id"], "cancelled")
    task, data = propose(store, source, worker)
    item = store.propose_assignment(OWNER, task["id"], data)
    store.finish(OWNER, task["id"], "failed")
    assert store.list_assignments(OWNER, source["id"])["items"][0]["status"] == "cancelled"
    with pytest.raises(Conflict):
        store.decide_assignment(OWNER, source["id"], item["id"], "approve")


@pytest.mark.parametrize("large_results", [False, True])
def test_assignment_pages_use_stable_scoped_cursors_and_bounded_response(owls, large_results):
    store, source, worker, outsider = owls
    store.set_coordinator(OWNER, source["id"], True)
    decisions = {}
    for _ in range(ASSIGNMENT_PAGE_SIZE + 2):
        task, data = propose(store, source, worker)
        item = store.propose_assignment(OWNER, task["id"], data)
        store.finish(OWNER, task["id"], "completed")
        decision = store.decide_assignment(
            OWNER, source["id"], item["id"], "approve" if large_results else "decline"
        )
        if large_results:
            child = store.claim(OWNER)
            store.finish(OWNER, child["id"], "completed", content="x" * 127_000)
        decisions[item["id"]] = decision

    with store.connect(write=True) as db:
        db.execute("UPDATE owl_assignments SET created_at=1.0")
    first = store.list_assignments(OWNER, source["id"])
    assert first["next_before"] == first["items"][-1]["id"]
    if large_results:
        assert len(first["items"]) < ASSIGNMENT_PAGE_SIZE
    else:
        assert len(first["items"]) == ASSIGNMENT_PAGE_SIZE
    assert len(encode(first).encode("utf-8")) < ASSIGNMENT_PAGE_BYTES + 100
    second = store.list_assignments(OWNER, source["id"], before=first["next_before"])
    assert second["next_before"] is None
    listed = [item["id"] for page in (first, second) for item in page["items"]]
    assert listed == sorted(decisions, reverse=True)
    oldest = store.read_assignment(OWNER, source["id"], listed[-1])
    assert oldest == second["items"][-1]
    with pytest.raises(NotFound):
        store.list_assignments("another owner", outsider["id"], before=first["next_before"])
    with pytest.raises(NotFound):
        store.read_assignment(OWNER, worker["id"], oldest["id"])


TAUGHT = ["Άνοιξε το email της παραγγελίας.", "Πέρασε τον κωδικό στο φύλλο.", "Αποθήκευσε."]


def test_taught_steps_travel_numbered_at_the_end_of_the_brief(tmp_path):
    store = Store(tmp_path / "taught-brief.db")
    assistant = store.set_assistant(OWNER, True)["bot"]
    setup = reviewed_setup().model_copy(update={"steps": TAUGHT})
    child = store.approve_assistant_owl(OWNER, assistant["id"], setup)["assignment"]
    brief = store.assignment_context(OWNER, child["child_task_id"])["brief"]
    assert brief == (
        setup.brief
        + "\n\nSteps the person showed or described, in order:\n"
        + "1. Άνοιξε το email της παραγγελίας.\n2. Πέρασε τον κωδικό στο φύλλο.\n3. Αποθήκευσε."
    )
    # A handoff without steps reads exactly as before.
    plain = store.approve_assistant_owl(OWNER, assistant["id"], reviewed_setup())["assignment"]
    assert store.assignment_context(OWNER, plain["child_task_id"])["brief"] == (
        reviewed_setup().brief
    )


def test_a_proposed_assignment_keeps_its_steps_and_a_retry_matches_it(owls):
    store, source, worker, _ = owls
    store.set_coordinator(OWNER, source["id"], True)
    chat = store.create_chat(OWNER, source["id"], "Assign work")
    task = store.enqueue(
        OWNER, chat["id"], TaskInput(message="Πέρασε τις παραγγελίες.", request_id=uuid4().hex)
    )
    assert store.claim(OWNER)["id"] == task["id"]
    data = AssignmentInput(
        recipient_bot_id=worker["id"],
        brief="Πέρασε τις παραγγελίες της ημέρας.",
        expected_result="Ένα ενημερωμένο φύλλο.",
        steps=TAUGHT,
    )
    first = store.propose_assignment(OWNER, task["id"], data)
    assert store.propose_assignment(OWNER, task["id"], data)["id"] == first["id"]
    assert first["brief"].endswith("3. Αποθήκευσε.")
    with pytest.raises(Conflict):
        store.propose_assignment(OWNER, task["id"], data.model_copy(update={"steps": TAUGHT[:2]}))


def test_confirmed_steps_are_kept_with_the_owl_as_a_playbook(tmp_path):
    from alysis_code.bots.models import AssistantAssignmentApproval, TaughtPlaybook
    from alysis_code.bots.playbooks import Playbooks

    store = Store(tmp_path / "taught-playbook.db")
    assistant = store.set_assistant(OWNER, True)["bot"]
    playbook = TaughtPlaybook(
        name="Παραγγελίες ημέρας",
        when_to_use="Όταν έρχονται νέες παραγγελίες με email.",
        done_check="Το φύλλο έχει μία γραμμή για κάθε παραγγελία.",
    )
    created = store.approve_assistant_owl(
        OWNER,
        assistant["id"],
        reviewed_setup().model_copy(update={"steps": TAUGHT, "playbook": playbook}),
    )
    owl = created["bot"]
    assert created["playbook"] == {
        "saved": True,
        "id": created["playbook"]["id"],
        "name": playbook.name,
        "version": 1,
    }
    playbooks = Playbooks(store, OWNER)
    kept = playbooks.read(owl["id"], created["playbook"]["id"])
    assert kept["steps"] == TAUGHT
    assert kept["pitfalls"] == [] and kept["tools"] == []
    assert kept["done_check"] == playbook.done_check

    # Teaching the same job again to the same owl makes a new version, which Undo takes back.
    again = AssistantAssignmentApproval(
        request_id=uuid4().hex,
        recipient_bot_id=owl["id"],
        brief="Πέρασε τις σημερινές παραγγελίες.",
        expected_result="Ένα ενημερωμένο φύλλο.",
        steps=TAUGHT[:2],
        playbook=playbook.model_copy(update={"name": "παραγγελιες ημερας"}),
    )
    second = store.approve_assistant_owl(OWNER, assistant["id"], again)
    assert second["playbook"]["id"] == created["playbook"]["id"]
    assert second["playbook"]["version"] == 2
    # A retried approval answers with the same receipt, playbook included, and keeps no more.
    assert (
        store.approve_assistant_owl(OWNER, assistant["id"], again)["playbook"]
        == (second["playbook"])
    )
    assert [item["version"] for item in playbooks.list(owl["id"])] == [2]
    assert playbooks.undo(owl["id"], second["playbook"]["id"])["restored_version"] == 1

    # With memory off the owl keeps nothing, and the handoff still goes ahead.
    quiet = store.save_bot(OWNER, BotInput(name="Σιωπηλή", memory_enabled=False))
    off = store.approve_assistant_owl(
        OWNER,
        assistant["id"],
        again.model_copy(update={"request_id": uuid4().hex, "recipient_bot_id": quiet["id"]}),
    )
    assert off["playbook"] == {"saved": False, "reason": "memory_off"}
    assert off["assignment"] is not None


def test_only_taught_steps_can_be_kept_as_a_playbook():
    from pydantic import ValidationError

    from alysis_code.bots import playbooks
    from alysis_code.bots.models import TAUGHT_STEPS, TaughtPlaybook

    with pytest.raises(ValidationError):
        reviewed_setup().model_copy(update={"steps": []}).model_validate(
            {
                **reviewed_setup().model_dump(exclude={"playbook"}),
                "playbook": {"name": "Ν", "when_to_use": "Π", "done_check": "Ε"},
            }
        )
    # A taught list always fits a playbook.
    assert TAUGHT_STEPS == playbooks.MAX_STEPS
    fields = TaughtPlaybook.model_fields
    assert fields["name"].metadata[-1].max_length == playbooks.NAME_CHARACTERS
    assert fields["when_to_use"].metadata[-1].max_length == playbooks.WHEN_CHARACTERS
    assert fields["done_check"].metadata[-1].max_length == playbooks.LINE_CHARACTERS
