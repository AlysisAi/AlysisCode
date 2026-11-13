"""Provider-free handoff and accounting checks; fixtures are not live-model evidence."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.coding import (
    CodingModelRequest,
    CodingResult,
    CodingStore,
    WorkspaceRegistration,
)
from alysis_code.bots.coding_worker import review_changes, snapshot
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store


@pytest.fixture
def handoff(tmp_path):
    store = Store(tmp_path / "bots.db")
    coding = CodingStore(store, "owner")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Maintain this project"))
    workspace, worker = uuid4().hex, uuid4().hex
    coding.register(WorkspaceRegistration(id=workspace, name="Project", worker_id=worker))
    coding.bind(bot["id"], workspace)
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue(
        "owner", chat["id"], TaskInput(message="Change the actual code", request_id=uuid4().hex)
    )
    task = store.claim("owner")
    return store, coding, bot, workspace, worker, task


def submit(handoff):
    store, coding, bot, workspace, worker, task = handoff
    return coding.submit(
        task, "Implement and check", [{"role": "user", "content": "日本語 context"}]
    )


def test_once_only_under_concurrent_submission_and_claim(handoff):
    store, coding, bot, workspace, worker, task = handoff
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(lambda _: submit(handoff), range(10)))
    assert len({j["id"] for j in jobs}) == 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda _: coding.claim(workspace, worker), range(10)))
    assert len([v for v in claims if v]) == 1
    assert jobs[0]["context"]["original_request"] == task["message"]
    assert jobs[0]["context"]["conversation"][0]["content"] == "日本語 context"
    assert (
        store.conversation("owner", task["chat_id"])["tasks"][0]["coding_job_id"] == jobs[0]["id"]
    )


@pytest.mark.parametrize("first_lane", ["computer", "workspace"])
def test_computer_and_workspace_jobs_cannot_write_for_same_bot_together(handoff, first_lane):
    store, coding, bot, workspace, worker, task = handoff
    background = BackgroundStore(store, "owner")
    if first_lane == "computer":
        background.submit(task, "Create the report", [])
    else:
        coding.submit(task, "Change the project", [], continuation={"messages": []})
    store.enqueue(
        "owner", task["chat_id"], TaskInput(message="Another write", request_id=uuid4().hex)
    )
    next_task = store.claim("owner")
    with pytest.raises(Conflict):
        if first_lane == "computer":
            coding.submit(next_task, "Concurrent write", [])
        else:
            background.submit(next_task, "Concurrent write", [])


def test_binding_and_worker_ownership(handoff):
    store, coding, bot, workspace, worker, task = handoff
    with pytest.raises(Conflict):
        coding.bind(bot["id"], None)
    with pytest.raises(NotFound):
        CodingStore(store, "other-owner").bind(bot["id"], workspace)
    with pytest.raises(Conflict):
        coding.register(WorkspaceRegistration(id=workspace, name="Imposter", worker_id=uuid4().hex))
    submit(handoff)
    job = coding.claim(workspace, worker)
    with pytest.raises(Conflict):
        coding.require_worker(job["id"], uuid4().hex)


def test_stopped_parent_cannot_be_upgraded_by_late_success(handoff):
    store, coding, bot, workspace, worker, task = handoff
    submit(handoff)
    job = coding.claim(workspace, worker)
    store.cancel("owner", task["id"])
    assert coding.get(job["id"])["status"] == "cancelled"
    late = coding.finish(
        job["id"],
        CodingResult(
            worker_id=worker,
            status="completed",
            summary="Looks good",
            outcome={"outcome": "verified_success", "verified_success": True},
        ),
    )
    assert late["status"] == "cancelled"
    assert not late["result"]["outcome"]["verified_success"]
    assert not coding.claim(workspace, worker)


def test_replacement_worker_does_not_replay_old_job(handoff):
    store, coding, bot, workspace, worker, task = handoff
    submit(handoff)
    job = coding.claim(workspace, worker)
    with store.connect(write=True) as db:
        db.execute("UPDATE coding_workspaces SET heartbeat=0")
    replacement = uuid4().hex
    coding.register(WorkspaceRegistration(id=workspace, name="Project", worker_id=replacement))
    assert coding.get(job["id"])["status"] == "interrupted"
    assert coding.claim(workspace, replacement) is None
    with pytest.raises(Conflict):
        coding.heartbeat(workspace, worker)


def test_model_attempt_dedup_and_shared_budget(handoff):
    store, coding, bot, workspace, worker, task = handoff
    submit(handoff)
    job = coding.claim(workspace, worker)
    req = CodingModelRequest(
        worker_id=worker,
        request_id=uuid4().hex,
        phase="execute",
        messages=[{"role": "user", "content": "Change it"}],
    )
    call, saved = coding.reserve_model(job["id"], req, "fixture", 2)
    assert saved is None
    with pytest.raises(Conflict, match="not be replayed"):
        coding.reserve_model(job["id"], req, "fixture", 2)
    store.finish_call(call, "completed", None)
    coding.finish_model(call, {"content": "Receipt"})
    assert coding.reserve_model(job["id"], req, "fixture", 2) == (call, {"content": "Receipt"})
    with pytest.raises(Conflict, match="different input"):
        coding.reserve_model(job["id"], req.model_copy(update={"messages": []}), "fixture", 2)
    # The parent's own call consumes the same ledger; no separate child allowance.
    store.reserve_call("owner", task["id"], "fixture", 2)
    with pytest.raises(LimitReached):
        coding.reserve_model(
            job["id"], req.model_copy(update={"request_id": uuid4().hex}), "fixture", 2
        )
    assert store.task("owner", task["id"])["calls"] == 2


def test_same_session_context_on_explicit_followup(handoff):
    store, coding, bot, workspace, worker, task = handoff
    first = submit(handoff)
    coding.claim(workspace, worker)
    coding.finish(
        first["id"],
        CodingResult(
            worker_id=worker, status="failed", summary="Partial", outcome={"outcome": "incomplete"}
        ),
    )
    store.finish("owner", task["id"], "completed", content="Some work remains")
    store.enqueue("owner", task["chat_id"], TaskInput(message="Correct it", request_id=uuid4().hex))
    followup = store.claim("owner")
    second = coding.submit(followup, "Apply correction", [])
    assert first["id"] != second["id"]
    assert first["session_id"] == second["session_id"]
    assert (
        CodingStore(Store(store.path), "owner").get(first["id"])["result"]["summary"] == "Partial"
    )


def test_job_budget_survives_reconfiguration_and_reserves_report(handoff):
    store, coding, bot, workspace, worker, task = handoff
    coding = CodingStore(store, "owner", execution_calls=2)
    job = coding.submit(task, "Bounded assignment", [])
    coding.claim(workspace, worker)
    # A later configuration must not expand an already accepted assignment.
    coding = CodingStore(Store(store.path), "owner", execution_calls=30)
    assert coding.get(job["id"])["context"]["execution_calls"] == 2

    def reserve(phase):
        request = CodingModelRequest(
            worker_id=worker, request_id=uuid4().hex, phase=phase, messages=[]
        )
        call, _ = coding.reserve_model(job["id"], request, "fixture", 100)
        store.finish_call(call, "completed", None)
        coding.finish_model(call, {"content": "Fixture response"})

    reserve("execute")
    reserve("execute")
    with pytest.raises(LimitReached, match="allowance"):
        reserve("execute")
    reserve("report")
    with pytest.raises(LimitReached, match="allowance"):
        reserve("report")
    with pytest.raises(Conflict, match="ended"):
        reserve("execute")


def test_review_reports_new_files_deletions_binary_and_unicode(tmp_path):
    (tmp_path / "old.txt").write_text("before\n")
    before = snapshot(tmp_path)
    (tmp_path / "old.txt").unlink()
    (tmp_path / "νέο.txt").write_text("日本語\n")
    (tmp_path / "binary.bin").write_bytes(b"\xff\0")
    files, diff = review_changes(before, snapshot(tmp_path))
    assert files == ["binary.bin", "old.txt", "νέο.txt"]
    assert "-before" in diff and "+日本語" in diff and "Binary change" in diff


def test_snapshot_does_not_follow_external_links(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "private").write_text("private fixture")
    (root / "link").symlink_to(tmp_path / "private")
    assert snapshot(root) == {}


def test_worker_transport_is_separate_from_desktop_capability(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    user_token, worker_token = "u" * 40, "w" * 40
    app = create_app(
        Settings(
            database=tmp_path / "auth.db",
            token=user_token,
            coding_token=worker_token,
            api_key="fixture-key",
            base_url="https://example.invalid/v1",
            model="fixture",
            max_calls=0,
        )
    )
    with TestClient(app) as client:
        route = "/internal/coding/workspaces"
        body = {"id": uuid4().hex, "worker_id": uuid4().hex, "name": "Scoped project"}
        assert (
            client.post(
                route, json=body, headers={"Authorization": "Bearer " + user_token}
            ).status_code
            == 401
        )
        assert (
            client.post(
                route, json=body, headers={"Authorization": "Bearer " + worker_token}
            ).status_code
            == 200
        )
        assert (
            client.get("/v1/bots", headers={"Authorization": "Bearer " + worker_token}).status_code
            == 401
        )
        workspaces = client.get(
            "/v1/coding/workspaces", headers={"Authorization": "Bearer " + user_token}
        ).json()
        assert set(workspaces[0]) == {"id", "name", "bot_id", "online"}


def test_schema_four_migration_preserves_user_workspace(tmp_path):
    import sqlite3

    path = tmp_path / "migrate.db"
    store = Store(path)
    bot = store.save_bot("owner", BotInput(name="Existing", purpose="Preserve"))
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE coding_calls")
        db.execute("DROP TABLE coding_jobs")
        db.execute("DROP TABLE coding_workspaces")
        db.execute("PRAGMA user_version=4")
    migrated = Store(path)
    assert migrated.list_bots("owner")[0]["id"] == bot["id"]
    with migrated.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "request_hash" in [r[1] for r in db.execute("PRAGMA table_info(coding_calls)")]


def test_parent_reports_actual_child_receipt_without_more_tools(handoff):
    import threading
    import time

    from alysis_code.bots.runtime import BotRuntime
    from alysis_code.cancellation import EventCancellationToken
    from alysis_code.llm.types import LLMResponse, ToolCall

    store, coding, bot, workspace, worker, task = handoff

    class FixtureClient:
        model = "fixture"
        supports_tool_calling = True
        count = 0

        def chat(self, **kw):
            self.count += 1
            if self.count == 1:
                return LLMResponse(
                    "",
                    [ToolCall("handoff", "coding_task", {"brief": "Implement current request"})],
                    {},
                )
            assert [t["function"]["name"] for t in kw["tools"]] == [
                "propose_action",
                "withdraw_action",
            ]
            assert "incomplete" in str(kw["messages"])
            return LLMResponse("Partial changes are saved; work remains.", [], {})

    client = FixtureClient()
    runtime = BotRuntime(store, "owner", lambda _: client, coding_enabled=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(runtime.run, task, EventCancellationToken(threading.Event()))
        end = time.monotonic() + 5
        job = None
        while time.monotonic() < end:
            job = coding.claim(workspace, worker)
            if job:
                break
            time.sleep(0.01)
        assert job
        coding.finish(
            job["id"],
            CodingResult(
                worker_id=worker,
                status="failed",
                summary="Tests failed",
                outcome={"outcome": "incomplete", "verified_success": False},
                files=["labels.py"],
                changes="+partial",
            ),
        )
        running.result(timeout=5)
    resumed = store.claim("owner")
    assert resumed["id"] == task["id"]
    runtime.run(resumed, EventCancellationToken(threading.Event()))
    final = store.task("owner", task["id"])
    assert final["status"] == "completed"  # Report completed; coding remains incomplete.
    assert "incomplete" in final["output"]
    assert final["output"].startswith("Work status: incomplete.")
    assert "AlysisCode" not in final["output"]
    assert coding.get(job["id"])["status"] == "failed"


def test_registered_subdirectory_never_expands_to_parent_git_repo(tmp_path):
    from alysis_code.bots.coding_worker import ScopedRunner, workspace_binding

    (tmp_path / ".git").mkdir()
    child = tmp_path / "selected"
    child.mkdir()
    binding = workspace_binding(child)
    assert binding.workspace_context.workspace_root == child
    assert binding.workspace_context.git_root is None
    runner = ScopedRunner(workspace=child)
    with pytest.raises(ValueError, match="leave the registered"):
        runner.run(root=tmp_path, cwd=child, cmd="true", timeout_s=1)


def test_receipt_retrieval_is_scoped_to_bot(handoff):
    store, coding, bot, workspace, worker, task = handoff
    submit(handoff)
    job = coding.claim(workspace, worker)
    coding.finish(
        job["id"],
        CodingResult(
            worker_id=worker,
            status="failed",
            summary="Failed check",
            outcome={"outcome": "incomplete"},
            verification={"passed": False},
        ),
    )
    assert coding.recent(bot["id"])[0]["task_id"] == task["id"]
    other = store.save_bot("owner", BotInput(name="Peer", purpose="Separate context"))
    with pytest.raises(NotFound):
        coding.read_result(other["id"], task["id"])


def test_host_session_uses_exact_workspace_and_never_loads_keys_or_hooks(tmp_path, monkeypatch):
    from alysis_code.agent import session as session_module
    from alysis_code.bots.coding_worker import Broker, ScopedRunner, make_session

    root = (tmp_path / "repo").resolve()
    root.mkdir()
    (root / ".git").mkdir()
    selected = root / "selected"
    selected.mkdir()
    state = tmp_path / "private-state"
    state.mkdir()

    def forbidden(*args, **kwargs):
        raise AssertionError("Host credentials/hooks must not be resolved")

    monkeypatch.setattr(session_module, "resolve_api_key", forbidden)
    monkeypatch.setattr(session_module, "load_resolved_hooks_config", forbidden)
    broker = Broker("http://127.0.0.1:1", "fixture", uuid4().hex)
    session = make_session(
        selected,
        state,
        {"id": uuid4().hex, "session_id": uuid4().hex},
        broker,
        {
            "base_url": "https://api.z.ai/api/coding/paas/v4",
            "model": "glm-5.3-flash",
            "image": "alysis-bots-dev:local",
            "verify": [],
        },
        ScopedRunner(workspace=selected),
    )
    try:
        assert session.root == selected
        assert session.client.broker is broker
        assert session.hook_dispatcher is None
        assert session.verification_enabled
        assert session.effective_verification_commands == []
        assert not session.verification_authoritative
    finally:
        session.close()
        broker.client.close()


@pytest.mark.parametrize("read_before_result", [False, True])
def test_late_partial_diff_preserves_stopped_verdict(handoff, read_before_result):
    store, coding, bot, workspace, worker, task = handoff
    submit(handoff)
    job = coding.claim(workspace, worker)
    store.cancel("owner", task["id"])
    if read_before_result:
        coding.get(job["id"])
    stopped = coding.finish(
        job["id"],
        CodingResult(
            worker_id=worker,
            status="completed",
            summary="Done",
            outcome={"outcome": "verified_success"},
            changes="\n+partial\n",
            files=["file.txt"],
        ),
    )
    assert stopped["status"] == "cancelled"
    assert stopped["result"]["outcome"]["verified_success"] is False
    assert stopped["result"]["changes"] == "\n+partial\n"
    assert stopped["result"]["files"] == ["file.txt"]


def test_current_request_and_historical_context_have_separate_channels(tmp_path, monkeypatch):
    import threading

    from alysis_code.bots import coding_worker

    root = tmp_path / "project"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    captured = {}

    class SessionFixture:
        from types import SimpleNamespace

        client = SimpleNamespace(instruction_revision=0)
        messages = []
        last_turn_outcome = {"outcome": "completed_unverified", "verified_success": False}

        def run_turn(self, instruction, **kwargs):
            captured.update(instruction=instruction, **kwargs)
            return 0

        def close(self):
            pass

    monkeypatch.setattr(coding_worker, "make_session", lambda *args: SessionFixture())
    job = {
        "id": uuid4().hex,
        "workspace_id": uuid4().hex,
        "session_id": uuid4().hex,
        "context": {
            "original_request": "Only correct the current test.",
            "brief": "Correct the test",
            "conversation": [
                {"role": "user", "content": "Historical request to deploy a website."}
            ],
        },
    }
    result = coding_worker.execute(
        job,
        workspace=root,
        state=state,
        broker=None,
        settings={"image": "unused-fixture", "verify": []},
        stop=threading.Event(),
    )
    assert captured["instruction"] == "Only correct the current test."
    assert "Historical request to deploy a website." in captured["ephemeral_system_messages"][0]
    assert result["status"] == "completed"
    assert not result["outcome"]["verified_success"]


def test_fast_restart_waits_for_old_lease_without_claiming_or_model_calls():
    import threading

    from alysis_code.bots.coding_worker import Broker

    calls = []

    class ResponseFixture:
        def __init__(self, status):
            self.status_code = status
            self.is_success = status == 200

        def json(self):
            return {"registered": True}

    class TransportFixture:
        def post(self, path, **kwargs):
            calls.append(path)
            return ResponseFixture(409 if len(calls) == 1 else 200)

    broker = Broker("http://127.0.0.1:1", "fixture", uuid4().hex)
    broker.client.close()
    broker.client = TransportFixture()
    assert broker.register_workspace(uuid4().hex, "Synthetic", threading.Event()) == {
        "registered": True
    }
    assert calls == ["/internal/coding/workspaces"] * 2
