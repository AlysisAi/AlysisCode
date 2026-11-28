import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from alysis_code.bots import store as store_module
from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import AssignmentInput, BotInput, ConversationPageQuery, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import AssignmentScheduleInput, ScheduleInput
from alysis_code.bots.store import Conflict, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse

OWNER = "owner"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 11, 20, 7, tzinfo=UTC).timestamp())
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: clock.now))
    store = Store(tmp_path / "owls.db")
    coordinator = store.save_bot(OWNER, BotInput(name="Συντονισμός"))
    worker = store.save_bot(OWNER, BotInput(name="Δελτίο"))
    store.set_coordinator(OWNER, coordinator["id"], True)
    chat = store.create_chat(OWNER, coordinator["id"], "Ενημέρωση")
    parent = store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(message="Ζήτησε εβδομαδιαία ενημέρωση.", request_id=uuid4().hex),
    )
    store.claim(OWNER)
    proposal = store.propose_assignment(
        OWNER,
        parent["id"],
        AssignmentInput(
            recipient_bot_id=worker["id"],
            brief="Γράψε μια σύντομη υπενθύμιση για την παράδοση αυτής της εβδομάδας.",
            expected_result="Δώσε μία πρόταση στα ελληνικά με τη σημερινή ημερομηνία.",
        ),
    )
    store.finish(OWNER, parent["id"], "completed", content="Έτοιμο για έγκριση.")
    spec = AssignmentScheduleInput.model_validate(
        {
            "name": "Εβδομαδιαία ενημέρωση",
            "timing": {
                "kind": "weekly",
                "weekday": 4,
                "time": "10:00",
                "timezone": "Europe/Athens",
            },
            "request_id": uuid4().hex,
        }
    )
    return store, coordinator, worker, chat, proposal, spec, clock


def complete_original(store, coordinator, proposal):
    original = store.decide_assignment(OWNER, coordinator["id"], proposal["id"], "approve")
    child = store.claim(OWNER)
    assert child["id"] == original["child_task_id"]
    store.finish(
        OWNER,
        child["id"],
        "completed",
        content="Το αρχικό κείμενο.",
        assignment_verification={"status": "met", "detail": "Ελεγμένο."},
    )


def test_schedule_endpoint_accepts_only_completed_owned_assignments_and_is_idempotent(setup):
    store, coordinator, worker, _, proposal, spec, _ = setup
    settings = Settings(
        store.path, "fixture" * 8, "fixture", "https://example.invalid", "fixture", owner=OWNER
    )
    client = TestClient(create_app(settings))
    path = f"/v1/bots/{coordinator['id']}/assignments/{proposal['id']}/schedule"
    assert client.post(path, json=spec.model_dump(mode="json")).status_code == 401
    client.headers["Authorization"] = "Bearer " + settings.token
    assert "owl_assignment_routines" in client.get("/v1/status").json()["capabilities"]
    assert client.post(path, json=spec.model_dump(mode="json")).status_code == 409
    complete_original(store, coordinator, proposal)
    wrong_owl_path = path.replace(coordinator["id"], worker["id"])
    assert client.post(wrong_owl_path, json=spec.model_dump(mode="json")).status_code == 404
    response = client.post(path, json=spec.model_dump(mode="json"))
    assert response.status_code == 201
    schedule = response.json()
    assert schedule["state"] == "paused"
    assert schedule["next_run_at"] is None
    assert schedule["assignment"] == {
        "source_assignment_id": proposal["id"],
        "recipient_name": worker["name"],
    }
    assert client.post(path, json=spec.model_dump(mode="json")).json() == schedule
    assert (
        client.post(
            path, json={**spec.model_dump(mode="json"), "name": "Different schedule"}
        ).status_code
        == 409
    )
    assert store.claim(OWNER) is None


def test_active_routine_survives_restart_without_replay_and_keeps_the_approved_scope(setup):
    store, coordinator, worker, chat, proposal, spec, clock = setup
    bot_id = coordinator["id"]
    complete_original(store, coordinator, proposal)
    schedule = store.schedule_assignment(OWNER, bot_id, proposal["id"], spec)
    with pytest.raises(Conflict, match="approved assignment"):
        store.save_schedule(
            OWNER,
            bot_id,
            ScheduleInput(
                name="Changed",
                prompt="A new task that was never approved.",
                timing=spec.timing,
                expected_revision=schedule["revision"],
            ),
            schedule_id=schedule["id"],
        )
    active = store.decide_schedule(OWNER, bot_id, schedule["id"], "active", schedule["revision"])
    clock.now = active["next_run_at"]
    assert store.enqueue_due_schedules(OWNER, max_calls=None) == 1
    child = store.claim(OWNER)
    assert child["bot_id"] == worker["id"]
    assert store.enqueue_due_schedules(OWNER, max_calls=None) == 0
    store.checkpoint_assignment_result(OWNER, child["id"], "Το κείμενο σώθηκε.")
    store = Store(store.path)
    assert store.recover(OWNER) == 1
    # Nothing it did reached outside the owl, so it starts again once from the approved brief
    # (Store._cut_by_restart); a second restart leaves it interrupted, without a replay.
    again = store.claim(OWNER)
    assert again["id"] == child["id"] and again["restarts"] == 1
    store = Store(store.path)
    assert store.recover(OWNER) == 1
    assert store.enqueue_due_schedules(OWNER, max_calls=None) == 0
    assert store.claim(OWNER) is None
    entries = {
        item["child_task_id"]: item for item in store.list_assignments(OWNER, bot_id)["items"]
    }
    interrupted = entries[child["id"]]
    assert interrupted["result"] == "Το κείμενο σώθηκε."
    assert interrupted["status"] == "interrupted"
    assert interrupted["verification"] is None
    assert interrupted["routine_id"] == schedule["id"]
    assert interrupted["source_task_id"] == proposal["source_task_id"]

    # Jump ahead three months. Old occurrences are missed and only the current
    # occurrence runs. The current date is supplied beside the unchanged brief.
    clock.now += timedelta(weeks=13).total_seconds()
    assert store.enqueue_due_schedules(OWNER, max_calls=None) == 1
    current = store.claim(OWNER)
    assert current["id"] != child["id"]
    assert current["bot_id"] == worker["id"]
    seen = []

    class TextModel:
        model = "text-only-fixture"
        supports_tool_calling = False

        def chat(self, *, tools, messages, **kwargs):
            assert tools is None
            payload = json.loads(messages[-1]["content"])
            assert payload["brief"] == proposal["brief"]
            assert payload["expected_result"] == proposal["expected_result"]
            assert (
                payload["scheduled_for_utc"] == datetime.fromtimestamp(clock.now, UTC).isoformat()
            )
            assert "Europe/Athens" in payload["run_time"]
            seen.append(payload)
            return LLMResponse(
                '{"decision":"ready","reason":"Υπάρχει ημερομηνία."}'
                if "worker_result" in payload
                else "Παρασκευή 19 Φεβρουαρίου 2027: ώρα για την ενημέρωση.",
                [],
                {},
            )

    runtime = BotRuntime(store, OWNER, lambda _: TextModel())
    runtime.run(current, EventCancellationToken(threading.Event()))
    assert len(seen) == 2
    assert store.enqueue_due_schedules(OWNER, max_calls=None) == 0
    scheduled = next(
        item
        for item in store.list_assignments(OWNER, bot_id)["items"]
        if item["child_task_id"] == current["id"]
    )
    assert scheduled["verification"]["status"] == "met"
    runs = store.schedule_runs(OWNER, bot_id, schedule["id"])["runs"]
    assert [run["current_status"] for run in runs] == ["completed", "missed", "interrupted"]
    assert [item["id"] for item in store.conversation(OWNER, chat["id"])["tasks"]] == [
        proposal["source_task_id"]
    ]
    assert (
        store.conversation_page(OWNER, current["chat_id"], ConversationPageQuery())["tasks"] == []
    )
    with pytest.raises(Conflict, match="original assignment"):
        store.schedule_assignment(OWNER, bot_id, scheduled["id"], spec)


def test_manual_run_is_idempotent_and_does_not_enable_the_routine(setup):
    store, coordinator, worker, _, proposal, spec, clock = setup
    bot_id = coordinator["id"]
    complete_original(store, coordinator, proposal)
    schedule = store.schedule_assignment(OWNER, bot_id, proposal["id"], spec)
    request_id = uuid4().hex
    first = store.run_schedule_now(OWNER, bot_id, schedule["id"], request_id, max_calls=None)
    clock.now += 1
    assert (
        store.run_schedule_now(OWNER, bot_id, schedule["id"], request_id, max_calls=None) == first
    )
    with pytest.raises(Conflict, match="already running"):
        store.run_schedule_now(OWNER, bot_id, schedule["id"], uuid4().hex, max_calls=None)
    task = store.claim(OWNER)
    assert task["id"] == first["task_id"] and task["bot_id"] == worker["id"]
    assert store.schedule_runs(OWNER, bot_id, schedule["id"])["runs"][0]["origin"] == "manual"
    assert store.list_schedules(OWNER, bot_id)[0]["state"] == "paused"


@pytest.mark.parametrize("change", ["coordinator", "recipient", "review"])
def test_affected_assignment_routine_pauses_instead_of_dispatching_more_work(setup, change):
    store, coordinator, worker, _, proposal, spec, clock = setup
    bot_id = coordinator["id"]
    complete_original(store, coordinator, proposal)
    paused = store.schedule_assignment(OWNER, bot_id, proposal["id"], spec)
    active = store.decide_schedule(OWNER, bot_id, paused["id"], "active", paused["revision"])
    if change == "coordinator":
        store.set_coordinator(OWNER, worker["id"], True)
    elif change == "recipient":
        store.delete_bot(OWNER, worker["id"])
    else:
        clock.now = active["next_run_at"]
        assert store.enqueue_due_schedules(OWNER, max_calls=None) == 1
        child = store.claim(OWNER)
        store.finish(
            OWNER,
            child["id"],
            "completed",
            content="Ένα κείμενο που χρειάζεται έλεγχο.",
            assignment_verification={"status": "needs_review", "detail": "Λείπει η ημερομηνία."},
        )
    schedule = store.list_schedules(OWNER, bot_id)[0]
    assert schedule["state"] == "paused"
    assert schedule["pause_code"] == "assignment_" + (
        "missing" if change == "recipient" else change
    )
    assert (
        store.enqueue_due_schedules(
            OWNER, max_calls=None, now=active["next_run_at"] + timedelta(days=7).total_seconds()
        )
        == 0
    )
    assert store.claim(OWNER) is None


def computer_task(store, coordinator, worker, chat, access):
    """A finished computer task on the worker's own computer, approved with its card."""
    parent = store.enqueue(
        OWNER, chat["id"], TaskInput(message="Πέρασε τις παραγγελίες.", request_id=uuid4().hex)
    )
    assert store.claim(OWNER)["id"] == parent["id"]
    proposal = store.propose_assignment(
        OWNER,
        parent["id"],
        AssignmentInput(
            recipient_bot_id=worker["id"],
            brief="Πέρασε τις σημερινές παραγγελίες στο φύλλο του καταστήματος.",
            expected_result="Ένα ενημερωμένο φύλλο.",
            work_scope="computer",
            access=access,
        ),
    )
    store.finish(OWNER, parent["id"], "completed", content="Έτοιμο για έγκριση.")
    approved = store.decide_assignment(
        OWNER, coordinator["id"], proposal["id"], "approve", "computer"
    )
    child = store.claim(OWNER)
    assert child["id"] == approved["child_task_id"]
    store.finish(OWNER, child["id"], "completed", content="Πέρασα τις παραγγελίες.")
    return proposal


def test_a_computer_task_repeats_with_its_card_once_the_person_allows_it_for_the_routine(setup):
    from alysis_code.bots.models import AssignmentAccess
    from alysis_code.bots.routine_access import RoutineAccessDecision

    store, coordinator, worker, chat, _, spec, clock = setup
    bot_id = coordinator["id"]
    access = AssignmentAccess(sites=["https://shop.example"], apps=["alysis.gmail"])
    proposal = computer_task(store, coordinator, worker, chat, access)
    schedule = store.schedule_assignment(OWNER, bot_id, proposal["id"], spec)
    # The routine shows the card its runs use, and asks for its browser and website first.
    assert schedule["state"] == "paused"
    assert schedule["assignment"]["access"] == access.model_dump()
    assert schedule["assignment"]["recipient_bot_id"] == worker["id"]
    to_allow = schedule["access"]["to_allow"]
    assert to_allow == {
        "offline_computer": False,
        "online_browser": True,
        "sites": ["https://shop.example"],
    }
    with pytest.raises(Conflict, match="Allow what this routine uses"):
        store.decide_schedule(OWNER, bot_id, schedule["id"], "active", schedule["revision"])
    active = store.allow_routine_access(
        OWNER,
        bot_id,
        schedule["id"],
        RoutineAccessDecision(expected_revision=schedule["revision"], allow=to_allow, turn_on=True),
    )
    assert active["state"] == "active" and active["access"]["to_allow"] is None
    clock.now = active["next_run_at"]
    assert store.enqueue_due_schedules(OWNER, max_calls=None) == 1
    run = store.claim(OWNER)
    # Each run is the same task on the worker's computer, with exactly the card's access.
    assert run["bot_id"] == worker["id"]
    assert run["config"]["assignment_access"] == access.model_dump()
    assert run["config"]["assignment_brief"]["brief"] == proposal["brief"]
    # Nothing beyond the card can be allowed during a run: no card, no routine access request.
    runtime = BotRuntime(store, OWNER, lambda _: None)
    asked = runtime._routine_needs_more(
        run, SimpleNamespace(offline_computer=True, browser_origin=None)
    )
    assert asked["state"] == "not_on_card"
    assert not [
        e for e in store.task(OWNER, run["id"])["events"] if e["kind"] == "routine_access_needed"
    ]
    assert runtime._routine_access_state(run) == ""
    store.finish(
        OWNER,
        run["id"],
        "completed",
        content="Πέρασα τις νέες παραγγελίες.",
        assignment_verification={"status": "met", "detail": "Ταιριάζει."},
    )
    assert store.list_schedules(OWNER, bot_id)[0]["state"] == "active"
    item = next(
        entry
        for entry in store.list_assignments(OWNER, bot_id)["items"]
        if entry["child_task_id"] == run["id"]
    )
    assert item["routine_id"] == schedule["id"] and item["access"] == access.model_dump()
    # Taken back, the next run does not start: the routine waits for the person to allow it.
    store.revoke_routine_access(OWNER, bot_id, schedule["id"])
    clock.now += timedelta(days=7).total_seconds()
    assert store.enqueue_due_schedules(OWNER, max_calls=None) == 0
    assert store.claim(OWNER) is None
    paused = store.list_schedules(OWNER, bot_id)[0]
    assert paused["state"] == "paused" and paused["pause_code"] == "access"
    assert paused["access"]["to_allow"] == to_allow


def test_a_computer_task_with_only_apps_needs_nothing_more_to_turn_on(setup):
    from alysis_code.bots.models import AssignmentAccess

    store, coordinator, worker, chat, _, spec, clock = setup
    bot_id = coordinator["id"]
    access = AssignmentAccess(apps=["alysis.gmail"])
    proposal = computer_task(store, coordinator, worker, chat, access)
    schedule = store.schedule_assignment(OWNER, bot_id, proposal["id"], spec)
    assert schedule["access"]["to_allow"] is None
    active = store.decide_schedule(OWNER, bot_id, schedule["id"], "active", schedule["revision"])
    clock.now = active["next_run_at"]
    assert store.enqueue_due_schedules(OWNER, max_calls=None) == 1
    assert store.claim(OWNER)["config"]["assignment_access"] == access.model_dump()
