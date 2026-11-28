"""Opt-in real PostgreSQL tests; LLM/compute fixtures are explicitly synthetic."""

import base64
import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_bots_runtime import ScriptedClient, response

from alysis_code.bots.allocation_ledger import AllocationLedger
from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.cooperation import PeerRequest, PeerStore
from alysis_code.bots.models import BotInput, HistoryQuery, MemoryInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore, parameters
from alysis_code.bots.retention import ObjectRetention, RetentionPolicy
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.transfers import FileTransfers, UploadChunk, UploadInput
from alysis_code.bots.worker_lease import WorkerLease, WorkerLeaseLost


def test_sql_bindings_preserve_unicode_literals_comments_and_percent():
    assert parameters("SELECT ?, '?', 'it''s ?', \"?\", 5%2 -- ?\n/* ? /* ? */ */ ?") == (
        "SELECT %s, '?', 'it''s ?', \"?\", 5%%2 -- ?\n/* ? /* ? */ */ %s"
    )


@pytest.fixture
def shared(tmp_path):
    dsn = os.environ.get("ALYSIS_BOTS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set ALYSIS_BOTS_TEST_POSTGRES_DSN for disposable PostgreSQL 18 tests")
    objects = LocalObjectStorage(tmp_path / "objects")
    store = PostgresStore(dsn, objects=objects, state_directory=tmp_path / "one")
    other = PostgresStore(dsn, objects=objects, state_directory=tmp_path / "two")
    try:
        yield store, other, uuid4().hex
    finally:
        store.close()
        other.close()


def bot_task(store, owner):
    bot = store.save_bot(owner, BotInput(name="Shared", purpose="Verify durable execution"))
    chat = store.create_chat(owner, bot["id"], "Continuous")
    task = store.enqueue(
        owner, chat["id"], TaskInput(message="Straße Αθήνα 東京", request_id=uuid4().hex)
    )
    return bot, chat, task


def test_interactive_state_and_selected_publication_across_replicas(shared):
    from alysis_code.bots.models import ConversationPageQuery, InteractiveStateWrite
    from alysis_code.bots.store import Conflict

    first, second, owner = shared
    bot, chat, original = bot_task(first, owner)
    owl = bot["id"]
    first.claim(owner)
    file = first.save_bytes(
        owner, owl, "Meeting.html", b"<h1>Meeting</h1>", source_task_id=original["id"]
    )
    first.finish(owner, original["id"], "completed")

    def write(replica):
        body = InteractiveStateWrite(
            revision=1, sha256=file["sha256"], expected_version=0, data={"filter": "Delta"}
        )
        try:
            return replica.interactive_state(owner, owl, file["id"], body)["version"]
        except Conflict:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(write, (first, second))) == {1, "stale"}
    message = TaskInput(
        message="Make this heading larger.",
        request_id=uuid4().hex,
        file_ids=[file["id"]],
        interactive_target={
            "file_id": file["id"],
            "revision": 1,
            "sha256": file["sha256"],
            "state_version": 1,
            "region": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1},
        },
    )
    task = first.enqueue(owner, chat["id"], message)
    page = second.conversation_page(owner, chat["id"], ConversationPageQuery(limit=1))
    assert page["messages"][0]["interactive_selection"] == {
        "file_id": file["id"],
        "revision": 1,
        "label": "",
    }
    assert "interactive_target" not in page["tasks"][-1]["config"]
    assert second.claim(owner)["id"] == task["id"]
    assert second.interactive_revision_context(owner, task["id"])["state"] == {"filter": "Delta"}
    next_file = second.save_bytes(
        owner,
        owl,
        file["name"],
        b"<h2>Meeting</h2>",
        previous_file_id=file["id"],
        source_task_id=task["id"],
    )
    assert first.enqueue(owner, chat["id"], message)["id"] == task["id"]
    assert first.interactive_state(owner, owl, file["id"])["is_latest"] is False
    assert first.interactive_state(owner, owl, next_file["id"])["version"] == 1
    assert first.file_data(owner, owl, file["id"]) == b"<h1>Meeting</h1>"
    assert first.file_data(owner, owl, next_file["id"]) == b"<h2>Meeting</h2>"
    with pytest.raises(Conflict):
        first.interactive_revision_context(owner, task["id"])


def test_assignment_files_are_durable_across_shared_workers(shared):
    from alysis_code.bots.models import AssignmentInput

    store, other, owner = shared
    lead, _, parent = bot_task(store, owner)
    worker = store.save_bot(owner, BotInput(name="File reviewer"))
    store.set_coordinator(owner, lead["id"], True)
    assert other.claim(owner)["id"] == parent["id"]
    source = store.save_bytes(owner, lead["id"], "Ledger.csv", b"A,12")
    assignment = other.propose_assignment(
        owner,
        parent["id"],
        AssignmentInput(
            recipient_bot_id=worker["id"],
            brief="Review the selected ledger.",
            expected_result="Reviewed workbook",
            work_scope="workbook",
            input_file_ids=[source["id"]],
        ),
    )
    other.finish(owner, parent["id"], "completed")

    def approve(replica):
        return replica.decide_assignment(
            owner, lead["id"], assignment["id"], "approve", "workbook", [source["id"]]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(approve, (store, other))
    assert first == second
    child = other.claim(owner)
    shared_input = store.assignment_context(owner, child["id"])["input_files"][0]
    store.delete_item(owner, lead["id"], source["id"], kind="files")
    assert other.assignment_input_data(owner, child["id"], shared_input["id"]) == b"A,12"
    saved = other.save_bytes(
        owner, worker["id"], "Reviewed.xlsx", b"exact bytes", source_task_id=child["id"]
    )
    store.checkpoint_assignment_result(
        owner, child["id"], "Reviewed.", output_file_ids=[saved["id"]]
    )

    def finish(replica):
        replica.finish(
            owner,
            child["id"],
            "completed",
            assignment_verification={"status": "met"},
            assignment_output_file_ids=[saved["id"]],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish, (store, other)))
    (returned,) = other.read_assignment(owner, lead["id"], assignment["id"])["returned_files"]
    other.delete_item(owner, worker["id"], saved["id"], kind="files")
    assert store.file_data(owner, lead["id"], returned["id"]) == b"exact bytes"
    assert other.claim(owner)["config"]["assignment_return"]["returned_files"] == [returned]
    assert store.claim(owner) is None


def test_answer_review_preference_is_shared_and_new_tasks_capture_it(shared):
    from alysis_code.bots.models import ReplyReviewSettings

    store, other, owner = shared
    assert store.reply_review_settings(owner) == {"enabled": True}
    store.reply_review_settings(owner, ReplyReviewSettings(enabled=False))
    assert other.reply_review_settings(owner) == {"enabled": False}
    assert other.reply_review_settings(uuid4().hex) == {"enabled": True}
    _, _, task = bot_task(other, owner)
    assert task["config"]["reply_review_enabled"] is False
    other.reply_review_settings(owner, ReplyReviewSettings(enabled=True))
    assert store.reply_review_settings(owner) == {"enabled": True}
    assert store.task(owner, task["id"])["config"]["reply_review_enabled"] is False


def test_the_persons_time_zone_is_shared_per_owner(shared):
    from alysis_code.bots.models import TimeZoneSettings

    store, other, owner = shared
    assert store.time_zone_settings(owner) == {"time_zone": None}
    store.time_zone_settings(owner, TimeZoneSettings(time_zone="Pacific/Auckland"))
    other.time_zone_settings(owner, TimeZoneSettings(time_zone="America/New_York"))
    assert store.time_zone(owner) == "America/New_York"
    assert other.time_zone(uuid4().hex) is None


def test_action_rules_and_human_approval_survive_shared_store_reconnect(shared):
    from alysis_code.bots.action_review import (
        ActionReviews,
        ApprovalDecision,
        ApprovalRequired,
        RuleInput,
        RuleRevision,
        RuleUpdate,
    )
    from alysis_code.bots.background import BackgroundStore
    from alysis_code.bots.store import Conflict

    store, other, owner = shared
    bot, _, task = bot_task(store, owner)
    task = store.claim(owner)
    original, reconnected = ActionReviews(store, owner), ActionReviews(other, owner)
    custom = original.save_rule(
        bot["id"], RuleInput(effect="ask", description="Πριν δημοσιεύσεις αρχεία")
    )
    assert reconnected.rules(bot["id"])[0]["description"] == custom["description"]
    updated = reconnected.save_rule(
        bot["id"],
        RuleUpdate(effect="ask", description="Before publishing any report", expected_revision=1),
        custom["id"],
    )
    assert updated["revision"] == 2
    with pytest.raises(Conflict):
        original.remove_rule(bot["id"], custom["id"], RuleRevision(expected_revision=1))
    reconnected.remove_rule(bot["id"], custom["id"], RuleRevision(expected_revision=2))
    action = {
        "tool": "computer_run",
        "arguments": {"command": "touch report.txt"},
        "observed_context": {},
    }
    with pytest.raises(ApprovalRequired):
        original.request(task, action, "Confirm")
    original.pause(task, [])
    (pending,) = reconnected.list(bot["id"])
    decision = ApprovalDecision(action="always_allow", expected_revision=1)
    reconnected.decide(bot["id"], pending["id"], decision)
    assert other.recover(owner) == 0
    BackgroundStore(other, owner).claim()
    assert reconnected.consume(task, action)
    assert not original.consume(task, action)
    (exact,) = original.rules(bot["id"])
    assert exact["action"] == action
    original.remove_rule(bot["id"], exact["id"], RuleRevision(expected_revision=1))
    reconnected.decide(bot["id"], pending["id"], decision)
    assert original.rules(bot["id"]) == []


def test_shared_pool_bounds_connections_and_does_not_replay_failed_writes(shared):
    store, other, owner = shared
    bot, _, _ = bot_task(store, owner)

    def query(_):
        with store.connect() as db:
            db.execute("SELECT pg_sleep(0.02)")
            return db.execute("SELECT pg_backend_pid()").fetchone()[0]

    with ThreadPoolExecutor(max_workers=24) as pool:
        pids = set(pool.map(query, range(48)))
    assert len(pids) <= 8
    assert store._pool.get_stats()["pool_size"] <= 8
    with pytest.raises(RuntimeError, match="no success was confirmed"):
        with store.connect(write=True) as db:
            db.execute("UPDATE bots SET name=? WHERE id=?", ("Must roll back", bot["id"]))
            pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
            with other.connect() as admin:
                assert admin.execute("SELECT pg_terminate_backend(?)", (pid,)).fetchone()[0]
    # The interrupted transaction was not published or automatically replayed;
    # the pool replaces broken connections before later independent operations.
    assert store.bot(owner, bot["id"])["name"] == "Shared"


def expire(store, owner):
    # Fault injection; separate subprocess qualification waits for real expiry.
    with store.connect(write=True) as db:
        db.execute("UPDATE worker_leases SET expires=0 WHERE owner=?", (owner,))


def test_shared_history_memory_files_uploads_and_retention(shared):
    store, other, owner = shared
    bot, chat, task = bot_task(store, owner)
    store.save_memory(owner, bot["id"], MemoryInput(title="Language", content="日本語 Ελληνικά"))
    assert other.list_memories(owner, bot["id"])[0]["content"] == "日本語 Ελληνικά"
    store.claim(owner)
    store.finish(owner, task["id"], "completed", content="verified")
    assert other.search_history(owner, bot["id"], HistoryQuery(query="STRASSE"))["messages"]
    store.mark_chat_read(owner, chat["id"], store.task(owner, task["id"])["events"][-1]["seq"])
    a, b = FileTransfers(store, owner), FileTransfers(other, owner)
    data = os.urandom(800001)
    up = a.begin(bot["id"], UploadInput(name="Δ 東京.bin", size=len(data), request_id=uuid4().hex))
    # Uneven boundaries, retries and completion on a second controller.
    for i in range(0, len(data), 180007):
        chunk = UploadChunk(offset=i, data_base64=base64.b64encode(data[i : i + 180007]).decode())
        a.append(bot["id"], up["id"], chunk)
        b.append(bot["id"], up["id"], chunk)
    done = b.complete(bot["id"], up["id"])
    assert a.complete(bot["id"], up["id"]) == done
    assert b"".join(store.file_chunks(owner, bot["id"], done["file_id"])) == data
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM upload_parts WHERE upload_id=?", (up["id"],)
            ).fetchone()[0]
            == 0
        )
    store.delete_item(owner, bot["id"], done["file_id"], kind="files")
    gc = ObjectRetention(other, owner, RetentionPolicy(300, 300, 2, 300))
    now = time.time()
    gc.sweep(now=now)
    assert gc.sweep(now=now + 301)["deleted"] == 1


def test_competing_workers_expired_writer_and_uncertain_task_recovery(shared):
    store, other, owner = shared
    bot, chat, task = bot_task(store, owner)
    leases = [WorkerLease(store, owner), WorkerLease(other, owner)]

    def claim(lease):
        try:
            return lease, lease.acquire()
        except WorkerLeaseLost:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, leases))
    winners = [r for r in results if r]
    assert len(winners) == 1
    leader, worker = winners[0]
    assert worker.claim(owner)["id"] == task["id"]
    expire(store, owner)
    replacement = WorkerLease(other, owner)
    next_worker = replacement.acquire()
    with pytest.raises(WorkerLeaseLost):
        worker.finish(owner, task["id"], "completed", content="late success")
    with pytest.raises(WorkerLeaseLost):
        leader.renew()
    leader.close()  # A delayed release cannot release the replacement epoch.
    next_worker.recover(owner)
    assert next_worker.task(owner, task["id"])["status"] == "interrupted"
    assert next_worker.claim(owner) is None  # Never automatically replay uncertain work.
    replacement.close()


def test_write_transaction_rolls_back_when_lease_expires_before_commit(shared):
    store, _, owner = shared
    bot, _, _ = bot_task(store, owner)
    lease = WorkerLease(store, owner)
    worker = lease.acquire()
    with pytest.raises(WorkerLeaseLost), worker.connect(write=True) as db:
        db.execute("UPDATE bots SET name='must roll back' WHERE id=?", (bot["id"],))
        db.execute("UPDATE worker_leases SET expires=0 WHERE owner=?", (owner,))
    assert store.bot(owner, bot["id"])["name"] == "Shared"
    lease.close()


def test_workspace_recovery_uses_shared_allocation_and_fences_stale_computer(shared, tmp_path):
    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import ComputerSpec

    store, other, owner = shared
    bot, _, task = bot_task(store, owner)
    one = WorkerLease(store, owner)
    worker = one.acquire()
    worker.claim(owner)
    provider = Provider()
    # This fixture has its own provider inventory. Uncertain allocations left
    # by independent tests must retain their separate scopes and capacity.
    provider.scope = "recovery-fixture-" + uuid4().hex
    spec = ComputerSpec("synthetic-image")
    factory = CloudDesktopFactory(owner, provider, spec, tmp_path / "old-ledger", store=worker)
    computer = factory(bot["id"], None, cancel=lambda: False)
    old = computer.ref
    expire(store, owner)
    two = WorkerLease(other, owner)
    replacement = two.acquire()
    fresh = CloudDesktopFactory(owner, provider, spec, tmp_path / "fresh-ledger", store=replacement)
    fresh.reconcile()
    assert provider.status(old).state == "missing"
    with pytest.raises(WorkerLeaseLost):
        computer._request("command", {}, cancel=lambda: False)
    replacement.recover(owner)
    assert replacement.task(owner, task["id"])["status"] == "interrupted"
    assert not AllocationLedger(replacement, owner).pending()
    # Fixture's old process owns a local lock only; release its scratch handle.
    computer._computer._lease.cleanup(clean=False)
    two.close()


def test_schedules_and_peer_transactions(shared):
    store, _, owner = shared
    bot = store.save_bot(owner, BotInput(name="Routines", purpose="Shared scheduling"))
    s = store.save_schedule(
        owner,
        bot["id"],
        ScheduleInput(
            name="Check",
            prompt="Read notes",
            timing={"kind": "interval", "every_minutes": 5},
            request_id=uuid4().hex,
        ),
        now=1000,
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(
            pool.map(lambda _: store.enqueue_due_schedules(owner, max_calls=20, now=1301), range(8))
        )
    assert sum(counts) == 1
    assert len(store.schedule_runs(owner, bot["id"], s["id"])["runs"]) == 1
    scheduled = store.claim(owner)
    store.finish(owner, scheduled["id"], "completed", content="checked")
    _, _, request = bot_task(store, owner)
    task = store.claim(owner)
    ps = PeerStore(store, owner)
    req = PeerRequest(recipient_bot_id=bot["id"], brief="Review constraints")
    receipt = ps.submit(task, req, {"role": "assistant", "content": "Request review"}, 8)
    assert receipt
    assert ps.claim() is not None


def test_service_runtime_and_api_share_database_without_native_preview(shared, tmp_path):
    store, other, owner = shared
    bot, chat, task = bot_task(store, owner)
    settings = Settings(
        database=tmp_path / "unused.db",
        token="synthetic-test-token-for-disposable-tests",
        api_key="unused",
        base_url="https://fixture.invalid",
        model="fixture",
        owner=owner,
    )
    client = ScriptedClient(
        [
            response("Durable result"),
            response(
                '{"decision":"ready","reason":"The simple reply makes no unsupported action claims."}'
            ),
        ]
    )
    app = create_app(settings, task_store=store, client_factory=lambda _: client)
    with TestClient(app) as api:
        deadline = time.monotonic() + 8
        while other.task(owner, task["id"])["status"] not in ("completed", "failed"):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert other.task(owner, task["id"])["status"] == "completed", other.task(
            owner, task["id"]
        ).get("error")
        assert other.conversation(owner, chat["id"])["messages"][-1]["content"] == "Durable result"
        assert (
            api.get(
                "/v1/bots",
                headers={"Authorization": "Bearer synthetic-test-token-for-disposable-tests"},
            ).status_code
            == 200
        )
        second = BotRuntime(other, owner, lambda _: client)
        with pytest.raises(WorkerLeaseLost):
            second.start()
    assert not settings.database.exists()


def test_uncertain_allocation_blocks_runtime_recovery(shared, tmp_path):
    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import ComputerSpec
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.desktop_linux import DesktopError

    store, other, owner = shared
    bot, _, task = bot_task(store, owner)
    initial = WorkerLease(store, owner)
    worker = initial.acquire()
    worker.claim(owner)
    AllocationLedger(worker, owner).save(
        bot["id"],
        {
            "scope": "synthetic-scope",
            "allocation_id": uuid4().hex,
            "state": "creating",
        },
    )
    initial.close()
    factory = CloudDesktopFactory(
        owner, Provider(), ComputerSpec("fixture-image"), tmp_path / "unresolved", store=other
    )
    desktop = DesktopComputers(owner, ComputerViews(), factory=factory, store=other)
    runtime = BotRuntime(
        other, owner, lambda _: pytest.fail("No model should be called"), desktop_computers=desktop
    )
    with pytest.raises(DesktopError, match="uncertain"):
        runtime.start()
    assert not runtime.is_running
    assert store.task(owner, task["id"])["status"] == "running"  # not falsely recovered


def test_shared_runtime_stops_all_lanes_on_fence_loss(shared):
    store, _, owner = shared
    runtime = BotRuntime(store, owner, lambda _: pytest.fail("No queued model work"))
    runtime.start()
    expire(store, owner)
    deadline = time.monotonic() + 3
    while runtime.is_running:
        assert time.monotonic() < deadline
        time.sleep(0.025)
    assert runtime._stop.is_set()
    runtime.stop()


def test_shared_notes_proposals_background_and_file_events(shared):
    from alysis_code.bots.background import BackgroundStore
    from alysis_code.bots.models import FileInput, NoteInput
    from alysis_code.bots.proposals import ActionProposal, ProposalStore
    from alysis_code.bots.triggers import TriggerDecision, TriggerInput, TriggerStore

    store, other, owner = shared
    bot, chat, _ = bot_task(store, owner)
    task = store.claim(owner)
    note = store.save_note(
        owner, bot["id"], NoteInput(title="Context", content="東京 remains exact")
    )
    store.save_note(
        owner,
        bot["id"],
        NoteInput(title="Context", content="Later correction", expected_revision=1),
        note_id=note["id"],
    )
    assert len(other.note(owner, bot["id"], note["id"], include_history=True)["revisions"]) == 2
    proposed = ProposalStore(store, owner).propose(
        task,
        ActionProposal(
            question="Create a file?",
            instructions="Create the requested file",
            offline_computer=True,
        ),
    )
    assert proposed
    bg = BackgroundStore(store, owner)
    bg.submit(task, "Keep working", [{"role": "user", "content": task["message"]}])
    assert bg.claim()["id"] == task["id"]
    store.finish(owner, task["id"], "completed", content="done")
    triggers = TriggerStore(other, owner)
    rule = triggers.create(
        bot["id"],
        TriggerInput(name="Review uploads", kind="file_added", instructions="Review new files"),
    )
    rule = triggers.decide(
        bot["id"], rule["id"], TriggerDecision(state="active", expected_revision=rule["revision"])
    )
    assert rule["state"] == "active"
    store.save_file(owner, bot["id"], FileInput(name="event.txt", content="Synthetic event"))
    assert triggers.dispatch(max_calls=20, now=time.time() + 10) == 1
    review = other.claim(owner)
    assert review["config"]["activation"]


def test_partial_upload_cancel_and_expiry_free_chunk_rows(shared):
    store, _, owner = shared
    bot, _, _ = bot_task(store, owner)
    transfers = FileTransfers(store, owner)
    for cancel in (True, False):
        upload = transfers.begin(
            bot["id"], UploadInput(name=uuid4().hex, size=100, request_id=uuid4().hex)
        )
        transfers.append(
            bot["id"],
            upload["id"],
            UploadChunk(offset=0, data_base64=base64.b64encode(b"partial").decode()),
        )
        if cancel:
            transfers.cancel(bot["id"], upload["id"])
        else:
            with store.connect(write=True) as db:
                db.execute("UPDATE file_uploads SET expires=0 WHERE id=?", (upload["id"],))
            transfers.expire()
        with store.connect() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM upload_parts WHERE upload_id=?", (upload["id"],)
                ).fetchone()[0]
                == 0
            )


def test_delete_after_shared_computer_cleanup_uses_the_named_allocation_record(shared):
    import json

    from alysis_code.bots.store import Conflict

    store, other, owner = shared
    bot = store.save_bot(owner, BotInput(name="Retired", purpose="Remove after confirmed cleanup"))
    kept = store.save_bot(owner, BotInput(name="Kept", purpose="Preserve other work"))
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO computer_allocations VALUES(?,?,?,?)",
            (owner, bot["id"], json.dumps({"state": "sleeping"}), 0),
        )
    with pytest.raises(Conflict):
        other.delete_bot(owner, bot["id"])
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE computer_allocations SET record=? WHERE bot_id=?",
            (json.dumps({"state": "deleted"}), bot["id"]),
        )
    other.delete_bot(owner, bot["id"])
    assert [item["id"] for item in store.list_bots(owner)] == [kept["id"]]
