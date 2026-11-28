from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from alysis_code.bots.assistant_profile import ASSISTANT_PROFILE
from alysis_code.bots.models import (
    AssignmentInput,
    AssistantAssignmentApproval,
    BotInput,
    IntroInput,
    TaskInput,
)
from alysis_code.bots.scheduling import AssignmentScheduleInput
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store

OWNER = "owner"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "owls.db")


def test_assistant_api_is_opt_in_and_does_not_start_work(store, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    def unexpected_work(*_args):
        pytest.fail("Changing Assist must not wake the runtime or open a model client.")

    settings = Settings(
        store.path, "fixture" * 8, "fixture", "https://example.invalid", "fixture", owner=OWNER
    )
    app = create_app(settings, client_factory=unexpected_work)
    monkeypatch.setattr(app.state.runtime, "wake", unexpected_work)
    # Treat the worker as ready without starting it so the no-intro and enqueue guards run too.
    monkeypatch.setattr(type(app.state.runtime), "is_running", property(lambda _: True))
    regular = store.save_bot(OWNER, BotInput(name=ASSISTANT_PROFILE["name"]))
    client = TestClient(app)
    assert client.get("/v1/assistant").status_code == 401
    assert client.put("/v1/assistant", json={"enabled": True}).status_code == 401
    client.headers["Authorization"] = f"Bearer {settings.token}"
    assert "owl_assistant" in client.get("/v1/status").json()["capabilities"]
    for bad in (1, "true", None):
        assert client.put("/v1/assistant", json={"enabled": bad}).status_code == 422
    assert client.get("/v1/assistant").json() == {"enabled": False, "bot": None}
    assert client.put("/v1/assistant", json={"enabled": False}).json() == {
        "enabled": False,
        "bot": None,
    }
    assert (
        client.post("/v1/bots", json={"name": "Impostor", "managed_assistant": True}).status_code
        == 422
    )

    enabled = client.put("/v1/assistant", json={"enabled": True}).json()
    assert enabled == client.get("/v1/assistant").json()
    bot = enabled["bot"]
    assert enabled["enabled"] and bot["assistant_enabled"] and bot["managed_assistant"]
    assert {field: bot[field] for field in ASSISTANT_PROFILE} == ASSISTANT_PROFILE
    # Results stay with the owl that did the work; the Assistant points there instead of
    # promising the result in its own chat.
    assert "never say the result will appear in this chat" in bot["instructions"]
    assert bot["id"] != regular["id"]
    by_id = {item["id"]: item for item in client.get("/v1/bots").json()}
    assert by_id[bot["id"]]["managed_assistant"] is True
    assert by_id[bot["id"]]["assistant_enabled"] is True
    assert "managed_assistant" not in by_id[regular["id"]]
    assert "assistant_enabled" not in by_id[regular["id"]]
    assert store.list_chats(OWNER, bot["id"]) == []
    assert store.claim(OWNER) is None
    assert client.post(f"/v1/bots/{bot['id']}/intro", json={"request_id": uuid4().hex}).json() == {
        "task": None
    }

    for field, change in (
        ("name", "Another name"),
        ("purpose", "Different work"),
        ("instructions", "Different instructions"),
    ):
        data = {**ASSISTANT_PROFILE, field: change, "color": "red"}
        assert client.put(f"/v1/bots/{bot['id']}", json=data).status_code == 409
    assert store.bot(OWNER, bot["id"])["color"] is None
    data = {
        "name": ASSISTANT_PROFILE["name"],
        "color": "teal",
        "background": "stars",
        "memory_enabled": True,
    }
    edited = client.put(f"/v1/bots/{bot['id']}", json=data).json()
    assert (edited["color"], edited["background"], edited["memory_enabled"]) == (
        "teal",
        "stars",
        True,
    )
    assert edited["files_enabled"] == bot["files_enabled"]
    assert {field: edited[field] for field in ASSISTANT_PROFILE} == ASSISTANT_PROFILE
    regular_path = f"/v1/bots/{regular['id']}"
    assert client.put(regular_path + "/coordinator", json={"enabled": True}).status_code == 409
    view = client.get(regular_path + "/assignments").json()
    assert view["assistant_coordinator"] is True and view["coordinator"] is False
    assert (
        client.put(f"/v1/bots/{bot['id']}/coordinator", json={"enabled": False}).status_code == 409
    )

    off = client.put("/v1/assistant", json={"enabled": False}).json()
    assert off == {"enabled": False, "bot": {**edited, "assistant_enabled": False}}
    assert "assistant_coordinator" not in client.get(regular_path + "/assignments").json()
    chat = store.create_chat(OWNER, bot["id"], "Assistant history")
    denied = client.post(
        f"/v1/chats/{chat['id']}/tasks",
        json={"message": "Plan a review.", "request_id": uuid4().hex},
    )
    assert denied.status_code == 409
    assert store.claim(OWNER) is None
    assert client.delete(f"/v1/bots/{bot['id']}").status_code == 204
    assert client.get("/v1/assistant").json() == {"enabled": False, "bot": None}
    assert client.put("/v1/assistant", json={"enabled": True}).json()["bot"]["id"] != bot["id"]


def test_concurrent_first_enable_creates_one_owl_per_owner_and_reuses_history(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda _: store.set_assistant(OWNER, True), range(16)))
    (assistant,) = {state["bot"]["id"] for state in states}
    assert all(state["enabled"] for state in states)
    assert [bot["id"] for bot in store.list_bots(OWNER)] == [assistant]
    assert store.assistant("another owner") == {"enabled": False, "bot": None}
    other = store.set_assistant("another owner", True)["bot"]["id"]
    assert other != assistant
    with pytest.raises(NotFound):
        store.bot("another owner", assistant)

    chat = store.create_chat(OWNER, assistant, "Own history")
    request = TaskInput(message="Οργάνωσε μια ανασκόπηση για 東京.", request_id=uuid4().hex)
    task = store.enqueue(OWNER, chat["id"], request)
    assert task["config"]["managed_assistant"] is True
    assert task["config"]["assistant_enabled"] is True
    assert task["config"]["instructions"] == ASSISTANT_PROFILE["instructions"]
    assert store.claim(OWNER)["id"] == task["id"]
    store.finish(OWNER, task["id"], "completed", content="Αποθηκευμένη απάντηση.")
    assert store.set_assistant(OWNER, False)["bot"]["id"] == assistant
    assert store.enqueue(OWNER, chat["id"], request)["id"] == task["id"]
    with pytest.raises(Conflict, match="Turn on Assist"):
        store.enqueue(OWNER, chat["id"], request.model_copy(update={"request_id": uuid4().hex}))
    assert store.start_intro(OWNER, assistant, IntroInput(request_id=uuid4().hex)) is None

    reopened = Store(store.path)
    assert reopened.assistant(OWNER)["enabled"] is False
    assert reopened.set_assistant(OWNER, True)["bot"]["id"] == assistant
    messages = reopened.conversation(OWNER, chat["id"])["messages"]
    assert [row["content"] for row in messages] == [request.message, "Αποθηκευμένη απάντηση."]
    assert reopened.claim(OWNER) is None
    assert reopened.assistant("another owner")["bot"]["id"] == other


def test_existing_handoff_reuses_the_owl_and_queues_only_one_scoped_task(store):
    assistant = store.set_assistant(OWNER, True)["bot"]
    recipient = store.save_bot(OWNER, BotInput(name="仕入れ確認", purpose="入荷の状況を整理する"))
    other = store.save_bot("someone else", BotInput(name="Another owl"))
    approval = AssistantAssignmentApproval(
        recipient_bot_id=recipient["id"],
        request_id=uuid4().hex,
        brief="入荷記録の不明点を整理する",
        expected_result="確認項目の一覧",
    )
    with pytest.raises(NotFound):
        store.approve_assistant_owl(
            OWNER, assistant["id"], approval.model_copy(update={"recipient_bot_id": other["id"]})
        )
    with pytest.raises(Conflict, match="another owl"):
        store.approve_assistant_owl(
            OWNER,
            assistant["id"],
            approval.model_copy(update={"recipient_bot_id": assistant["id"]}),
        )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: store.approve_assistant_owl(OWNER, assistant["id"], approval), range(6)
            )
        )
    assert len({result["assignment"]["child_task_id"] for result in results}) == 1
    assert {bot["id"] for bot in store.list_bots(OWNER)} == {assistant["id"], recipient["id"]}
    result = results[0]
    assert result["bot"]["id"] == recipient["id"]
    assert result["assignment"]["brief"] == approval.brief
    assert result["assignment"]["expected_result"] == approval.expected_result
    with pytest.raises(Conflict, match="different owl handoff"):
        store.approve_assistant_owl(
            OWNER, assistant["id"], approval.model_copy(update={"brief": "Different instruction"})
        )


@pytest.mark.parametrize("enable_first", [False, True])
def test_assistant_does_not_use_an_ordinary_owl_slot(store, enable_first):
    assistant = store.set_assistant(OWNER, True)["bot"] if enable_first else None
    ordinary = [store.save_bot(OWNER, BotInput(name=f"Owl {n}")) for n in range(30)]
    if assistant is None:
        assistant = store.set_assistant(OWNER, True)["bot"]
    assert {bot["id"] for bot in store.list_bots(OWNER)} == {
        assistant["id"],
        *(bot["id"] for bot in ordinary),
    }
    for assist_enabled in (True, False):
        assert store.set_assistant(OWNER, assist_enabled)["bot"]["id"] == assistant["id"]
        with pytest.raises(LimitReached, match="up to 30"):
            store.save_bot(OWNER, BotInput(name="Extra owl"))
    assert store.claim(OWNER) is None


def proposal(store, coordinator, recipient):
    chat = store.create_chat(OWNER, coordinator, "Assignments")
    task = store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(message="Σύγκρινε τις περιγραφές.", request_id=uuid4().hex),
    )
    assert store.claim(OWNER)["id"] == task["id"]
    data = AssignmentInput(
        recipient_bot_id=recipient,
        brief="Σύγκρινε τις περιγραφές.",
        expected_result="Μία σύντομη σύνοψη.",
    )
    return task, data


def active_routine_and_pending_draft(store, coordinator, recipient):
    task, data = proposal(store, coordinator, recipient)
    accepted = store.propose_assignment(OWNER, task["id"], data)
    store.finish(OWNER, task["id"], "completed", content="Έτοιμο για έγκριση.")
    child = store.decide_assignment(OWNER, coordinator, accepted["id"], "approve")
    assert store.claim(OWNER)["id"] == child["child_task_id"]
    store.finish(
        OWNER,
        child["child_task_id"],
        "completed",
        content="Δύο διαφορετικές περιγραφές.",
        assignment_verification={"status": "met"},
    )
    routine = store.schedule_assignment(
        OWNER,
        coordinator,
        accepted["id"],
        AssignmentScheduleInput.model_validate(
            {
                "name": "Review",
                "timing": {"kind": "interval", "every_minutes": 60},
                "request_id": uuid4().hex,
            }
        ),
    )
    routine = store.decide_schedule(
        OWNER, coordinator, routine["id"], "active", routine["revision"]
    )
    task, data = proposal(store, coordinator, recipient)
    pending = store.propose_assignment(OWNER, task["id"], data)
    store.finish(OWNER, task["id"], "completed", content="Περιμένει έγκριση.")
    return accepted["id"], pending["id"], routine["id"]


def assert_role_ended(store, coordinator, accepted, pending, routine):
    view = store.list_assignments(OWNER, coordinator)
    items = {item["id"]: item for item in view["items"]}
    assert view["coordinator"] is False
    assert items[pending]["status"] == "cancelled"
    assert items[accepted]["result"] == "Δύο διαφορετικές περιγραφές."
    (schedule,) = store.list_schedules(OWNER, coordinator)
    assert schedule["id"] == routine
    assert (schedule["state"], schedule["pause_code"], schedule["next_run_at"]) == (
        "paused",
        "assignment_coordinator",
        None,
    )


def test_toggling_assist_transfers_authority_without_losing_approved_work(store):
    manual = store.save_bot(OWNER, BotInput(name="Manual coordinator"))["id"]
    recipient = store.save_bot(OWNER, BotInput(name="Νεφέλη"))["id"]
    store.set_coordinator(OWNER, manual, True)
    manual_work = active_routine_and_pending_draft(store, manual, recipient)

    assistant = store.set_assistant(OWNER, True)["bot"]["id"]
    assert store.set_assistant(OWNER, True)["bot"]["id"] == assistant
    assert_role_ended(store, manual, *manual_work)
    assert (
        store.list_assignments(OWNER, manual, before=manual_work[1])["assistant_coordinator"]
        is True
    )
    with pytest.raises(Conflict, match="Turn off Assist"):
        store.set_coordinator(OWNER, recipient, True)
    assistant_work = active_routine_and_pending_draft(store, assistant, recipient)
    store.set_assistant(OWNER, False)
    assert_role_ended(store, assistant, *assistant_work)
    assert store.is_coordinator(OWNER, manual)
    assert store.list_schedules(OWNER, manual)[0]["state"] == "paused"

    task, data = proposal(store, manual, assistant)
    with pytest.raises(Conflict, match="Choose another owl"):
        store.propose_assignment(OWNER, task["id"], data)
    store.finish(OWNER, task["id"], "completed", content="Διάλεξε άλλη κουκουβάγια.")
    with pytest.raises(Conflict, match="Use Assist"):
        store.set_coordinator(OWNER, assistant, True)
    store.set_assistant(OWNER, True)
    assert store.list_schedules(OWNER, assistant)[0]["state"] == "paused"


@pytest.mark.parametrize("previous_deleted", [False, True])
def test_deleting_active_assistant_detaches_it_and_restores_only_an_existing_coordinator(
    store, previous_deleted
):
    manual = store.save_bot(OWNER, BotInput(name="Manual coordinator"))["id"]
    store.set_coordinator(OWNER, manual, True)
    assistant = store.set_assistant(OWNER, True)["bot"]["id"]
    with pytest.raises(NotFound):
        store.delete_bot("another owner", assistant)
    if previous_deleted:
        store.delete_bot(OWNER, manual)
        assert store.is_coordinator(OWNER, assistant)
    store.delete_bot(OWNER, assistant)
    assert store.assistant(OWNER) == {"enabled": False, "bot": None}
    assert store.is_coordinator(OWNER, manual) is not previous_deleted
    assert store.set_assistant(OWNER, True)["bot"]["id"] != assistant
    assert store.claim(OWNER) is None
