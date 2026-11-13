"""Real persistence, authority and worker tests; scripted models are fixtures only."""

import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from bots_task_state import task_state

from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound, Store
from alysis_code.bots.triggers import TriggerDecision, TriggerInput, TriggerStore, file_added
from alysis_code.llm.types import LLMResponse, ToolCall


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Νεφέλη", purpose="Review added files"))
    triggers = TriggerStore(store, "owner")
    draft = triggers.create(
        bot["id"],
        TriggerInput(
            name="Review uploads",
            instructions="Read newly added files, summarize facts and suggest next steps.",
        ),
    )
    return store, bot, triggers, draft


def enable(bot, triggers, rule):
    return triggers.decide(
        bot["id"], rule["id"], TriggerDecision(state="active", expected_revision=rule["revision"])
    )


def upload(store, bot, name="Σημειώσεις-日本語.txt"):
    return store.save_file(
        "owner", bot["id"], FileInput(name=name, content="Synthetic project notes.")
    )


def test_drafts_do_not_activate_or_replay_old_files(setup):
    store, bot, triggers, rule = setup
    upload(store, bot)
    assert triggers.dispatch(max_calls=20, now=time.time() + 10) == 0
    enable(bot, triggers, rule)
    assert triggers.dispatch(max_calls=20, now=time.time() + 10) == 0
    assert store.usage("owner")["calls"] == 0


def test_duplicate_events_concurrent_dispatch_restart_and_batch(setup):
    store, bot, triggers, rule = setup
    enable(bot, triggers, rule)
    files = [upload(store, bot, str(n) + ".txt") for n in range(3)]
    with store.connect(write=True) as db:
        for f in files:
            file_added(db, bot["id"], f["id"], f["name"], time.time())
    assert triggers.dispatch(max_calls=20) == 0  # brief collection window, no provider work
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: TriggerStore(Store(store.path), "owner").dispatch(
                    max_calls=20, now=time.time() + 10
                ),
                range(12),
            )
        )
    assert sum(results) == 1
    task = store.claim("owner")
    assert {e["file_id"] for e in task["config"]["activation"]["events"]} == {
        f["id"] for f in files
    }
    store.finish("owner", task["id"], "completed", content="Review")
    assert (
        TriggerStore(Store(store.path), "owner").dispatch(max_calls=20, now=time.time() + 90) == 0
    )
    assert len({d["task_id"] for d in triggers.list(bot["id"])[0]["recent_deliveries"]}) == 1


def test_upload_and_delivery_rollback_together(setup, monkeypatch):
    store, bot, triggers, rule = setup
    enable(bot, triggers, rule)

    def fail(*args):
        raise RuntimeError("fixture storage failure")

    monkeypatch.setattr("alysis_code.bots.triggers.file_added", fail)
    with pytest.raises(RuntimeError):
        upload(store, bot)
    assert store.list_files("owner", bot["id"]) == []
    assert triggers.list(bot["id"])[0]["recent_deliveries"] == []


def test_busy_and_budget_keep_events_pending_without_model_calls(setup):
    store, bot, triggers, rule = setup
    enable(bot, triggers, rule)
    chat = store.create_chat("owner", bot["id"], "Chat")
    original = store.enqueue(
        "owner", chat["id"], TaskInput(message="Ongoing work", request_id=uuid4().hex)
    )
    upload(store, bot)
    assert triggers.dispatch(max_calls=20, now=time.time() + 10) == 0
    assert "current work" in triggers.list(bot["id"])[0]["recent_deliveries"][0]["reason"]
    store.finish("owner", original["id"], "completed", content="Done")
    assert triggers.dispatch(max_calls=0, now=time.time() + 11) == 0
    assert "allowance" in triggers.list(bot["id"])[0]["recent_deliveries"][0]["reason"]
    assert triggers.dispatch(max_calls=20, now=time.time() + 12) == 1
    assert store.usage("owner")["calls"] == 0


@pytest.mark.parametrize("queued", [False, True])
def test_pause_revokes_pending_and_queued_without_resume_replay(setup, queued):
    store, bot, triggers, rule = setup
    active = enable(bot, triggers, rule)
    upload(store, bot)
    if queued:
        assert triggers.dispatch(max_calls=20, now=time.time() + 10) == 1
    paused = triggers.decide(
        bot["id"], rule["id"], TriggerDecision(state="paused", expected_revision=active["revision"])
    )
    enable(bot, triggers, paused)
    assert triggers.dispatch(max_calls=20, now=time.time() + 90) == 0
    assert store.claim("owner") is None
    assert store.usage("owner")["calls"] == 0


def test_removed_files_skip_but_pending_work_survives_service_downtime(setup):
    store, bot, triggers, rule = setup
    enable(bot, triggers, rule)
    removed = upload(store, bot)
    store.delete_item("owner", bot["id"], removed["id"], kind="files")
    retained = upload(store, bot, "retained.txt")
    later = time.time() + 3 * 86400
    restarted = TriggerStore(Store(store.path), "owner")
    assert restarted.dispatch(max_calls=None, now=later) == 1
    task = store.claim("owner")
    events = task["config"]["activation"]["events"]
    assert [e["file_id"] for e in events] == [retained["id"]]
    assert events[0]["occurred_at"] < later - 2 * 86400
    deliveries = restarted.list(bot["id"])[0]["recent_deliveries"]
    assert (
        next(d for d in deliveries if d["event"]["file_id"] == removed["id"])["reason"]
        == "Source file was removed."
    )
    store.finish("owner", task["id"], "completed", content="Reviewed delayed upload")
    assert restarted.dispatch(max_calls=None, now=later + 61) == 0
    assert store.usage("owner")["calls"] == 0


def test_cooldown_retains_later_events_and_gives_chat_priority(setup):
    store, bot, triggers, rule = setup
    enable(bot, triggers, rule)
    upload(store, bot)
    now = time.time() + 10
    assert triggers.dispatch(max_calls=20, now=now) == 1
    event_task = store.claim("owner")
    store.finish("owner", event_task["id"], "completed", content="Done")
    upload(store, bot, "next.txt")
    assert triggers.dispatch(max_calls=20, now=now + 20) == 0
    assert triggers.dispatch(max_calls=20, now=now + 61) == 1
    chat = store.list_chats("owner", bot["id"])[0]
    human = store.enqueue(
        "owner", chat["id"], TaskInput(message="Answer me first", request_id=uuid4().hex)
    )
    assert store.claim("owner")["id"] == human["id"]


def test_setup_change_and_scope_checks(setup):
    store, bot, triggers, rule = setup
    active = enable(bot, triggers, rule)
    peer = store.save_bot("owner", BotInput(name="Peer", purpose="Separate"))
    with pytest.raises(NotFound):
        triggers.decide(
            peer["id"],
            rule["id"],
            TriggerDecision(state="paused", expected_revision=active["revision"]),
        )
    with pytest.raises(NotFound):
        TriggerStore(store, "other").list(bot["id"])
    upload(store, bot)
    store.save_bot("owner", BotInput(name="Changed", purpose="Different instructions"), bot["id"])
    assert triggers.dispatch(max_calls=20, now=time.time() + 10) == 0
    current = triggers.list(bot["id"])[0]
    assert current["state"] == "paused"
    with pytest.raises(Conflict):
        triggers.decide(
            bot["id"],
            rule["id"],
            TriggerDecision(state="active", expected_revision=active["revision"]),
        )


def test_bot_created_files_never_emit_and_review_has_no_write_tools(setup):
    store, bot, triggers, rule = setup
    enable(bot, triggers, rule)
    upload(store, bot)
    triggers.dispatch(max_calls=20, now=time.time() + 10)
    task = store.claim("owner")

    class Model:
        model = "fixture"
        supports_tool_calling = True
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            tools = {t["function"]["name"] for t in kwargs["tools"]}
            assert not tools & {
                "save_file",
                "coding_task",
                "save_working_note",
                "propose_trigger",
                "propose_schedule",
            }
            if self.calls == 1:
                # Even a malicious out-of-schema tool request cannot write.
                return LLMResponse(
                    "",
                    [ToolCall("write", "save_file", {"name": "bad.txt", "content": "unapproved"})],
                    {},
                )
            assert (
                "Invalid tool arguments"
                in next(m for m in reversed(kwargs["messages"]) if m["role"] == "tool")["content"]
            )
            return LLMResponse("Reviewed", [], {})

    runtime = BotRuntime(store, "owner", lambda _: Model())
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "completed"
    assert len(store.list_files("owner", bot["id"])) == 1
    human = store.enqueue(
        "owner", task["chat_id"], TaskInput(message="Create a result", request_id=uuid4().hex)
    )
    store.claim("owner")
    store.save_file(
        "owner",
        bot["id"],
        FileInput(name="result.txt", content="Result"),
        source_task_id=human["id"],
    )
    store.finish("owner", human["id"], "completed", content="Saved")
    assert triggers.dispatch(max_calls=20, now=time.time() + 100) == 0


def test_pause_cancels_running_review_and_invalidates_its_proposal(setup):
    store, bot, triggers, rule = setup
    active = enable(bot, triggers, rule)
    upload(store, bot)
    triggers.dispatch(max_calls=20, now=time.time() + 10)
    task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    item = proposals.propose(
        task, ActionProposal(question="Prepare a checklist?", instructions="Create a checklist")
    )
    triggers.decide(
        bot["id"], rule["id"], TriggerDecision(state="paused", expected_revision=active["revision"])
    )
    assert store.task("owner", task["id"])["status"] == "cancelling"
    runtime = BotRuntime(store, "owner", lambda _: None)
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "cancelled"
    with pytest.raises(Conflict):
        proposals.decide(
            bot["id"], item["id"], ActionDecision(action="accept", expected_revision=1)
        )


def test_trigger_draft_requires_successful_source_and_no_unknown_event_type(setup):
    from pydantic import ValidationError

    store, bot, triggers, rule = setup
    with pytest.raises(ValidationError):
        TriggerInput(name="Unsupported", kind="github", instructions="Watch repo")
    chat = store.create_chat("owner", bot["id"], "Chat")
    task = store.enqueue(
        "owner", chat["id"], TaskInput(message="Watch uploads", request_id=uuid4().hex)
    )
    store.claim("owner")
    draft = triggers.create(
        bot["id"],
        TriggerInput(name="Draft from model", instructions="Check added files for questions."),
        source_task_id=task["id"],
    )
    assert not triggers.list(bot["id"])[1]["ready"]
    with pytest.raises(Conflict):
        enable(bot, triggers, draft)
    store.finish("owner", task["id"], "completed", content="Review the activation")
    assert enable(bot, triggers, draft)["state"] == "active"


def test_failed_dispatch_rolls_back_task_and_delivery(setup, monkeypatch):
    store, bot, triggers, rule = setup
    enable(bot, triggers, rule)
    upload(store, bot)
    real = store._enqueue

    def fail(*args):
        real(*args)
        raise RuntimeError("fixture crash before delivery commit")

    monkeypatch.setattr(store, "_enqueue", fail)
    with pytest.raises(RuntimeError):
        triggers.dispatch(max_calls=20, now=time.time() + 10)
    assert store.claim("owner") is None
    assert triggers.list(bot["id"])[0]["recent_deliveries"][0]["state"] == "pending"
    assert (
        TriggerStore(Store(store.path), "owner").dispatch(max_calls=20, now=time.time() + 10) == 1
    )


def test_repeated_events_continue_without_daily_development_ceiling(setup):
    store, bot, triggers, rule = setup
    enable(bot, triggers, rule)
    now = time.time() + 10
    for index in range(15):
        f = upload(store, bot, f"{index}.txt")
        assert triggers.dispatch(max_calls=None, now=now + index * 61) == 1
        task = store.claim("owner")
        assert [e["file_id"] for e in task["config"]["activation"]["events"]] == [f["id"]]
        store.finish("owner", task["id"], "completed", content="Review")
    assert triggers.dispatch(max_calls=None, now=now + 15 * 61) == 0
    assert store.usage("owner")["calls"] == 0


def test_many_rules_take_turns_even_with_continuous_uploads(setup):
    store, bot, triggers, first = setup
    rules = [first] + [
        triggers.create(
            bot["id"], TriggerInput(name=f"Review {n}", instructions=f"Review project {n}")
        )
        for n in range(10)
    ]
    for rule in rules:
        enable(bot, triggers, rule)
    expected = {r["id"] for r in rules}
    seen = set()
    now = time.time() + 10
    for n in range(len(rules)):
        upload(store, bot, f"batch-{n}.txt")
        assert triggers.dispatch(max_calls=None, now=now + n * 61) == 1
        task = store.claim("owner")
        trigger_id = task["config"]["activation"]["trigger_id"]
        assert trigger_id not in seen  # A busy older rule must not starve its peers.
        seen.add(trigger_id)
        store.finish("owner", task["id"], "completed", content="Review")
    assert seen == expected
    assert store.usage("owner")["calls"] == 0


def test_api_authority_and_worker_activation_across_restart(tmp_path):
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "api.db", "t" * 40, "fixture-key", "https://example.invalid/v1", "fixture"
    )

    class Model:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            assert "Event activation" in task_state(kwargs["messages"])
            return LLMResponse("Fixture review of the uploaded file", [], {})

    headers = {"Authorization": "Bearer " + settings.token}
    with TestClient(create_app(settings, client_factory=lambda _: Model())) as client:
        assert client.get("/v1/bots/none/triggers").status_code == 401
        client.headers.update(headers)
        bot = client.post("/v1/bots", json={"name": "Events", "purpose": "Review"}).json()
        base = f"/v1/bots/{bot['id']}"
        assert (
            client.post(
                base + "/triggers",
                json={"name": "Bad", "kind": "arbitrary_webhook", "instructions": "Do things"},
            ).status_code
            == 422
        )
        rule = client.post(
            base + "/triggers", json={"name": "Review", "instructions": "Review new files"}
        ).json()
        response = client.patch(
            base + "/triggers/" + rule["id"],
            json={"state": "active", "expected_revision": rule["revision"]},
        )
        assert response.status_code == 200
        assert (
            client.post(
                base + "/files", json={"name": "notes.txt", "content": "Synthetic"}
            ).status_code
            == 201
        )
        # Stop before the collection window elapses. Delivery remains durable.
        assert client.get("/v1/status").json()["usage"]["calls"] == 0
    with TestClient(
        create_app(settings, client_factory=lambda _: Model()), headers=headers
    ) as client:
        until = time.monotonic() + 10
        while time.monotonic() < until:
            deliveries = client.get(base + "/triggers").json()[0]["recent_deliveries"]
            if deliveries[0]["task_status"] == "completed":
                break
            time.sleep(0.05)
        assert deliveries[0]["task_status"] == "completed"
        assert client.get("/v1/status").json()["usage"]["calls"] == 1
    with TestClient(
        create_app(settings, client_factory=lambda _: Model()), headers=headers
    ) as client:
        time.sleep(1.1)
        assert client.get("/v1/status").json()["usage"]["calls"] == 1
        assert len(client.get(base + "/triggers").json()[0]["recent_deliveries"]) == 1
