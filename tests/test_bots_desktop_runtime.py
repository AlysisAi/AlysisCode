"""Offline tool/runtime integration fixtures. Actual Linux/model QA is separate."""

import json
from dataclasses import replace

import pytest
from test_bots_browser_runtime import ClientFixture, advertised_names, response
from test_bots_desktop import Executor

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import ObjectStorageError
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, Store


def test_combined_shutdown_attempts_both_surfaces_after_cleanup_failure(tmp_path):
    views = ComputerViews()
    attempted = []
    browser = BrowserComputers("owner", views, lambda *_: None)
    desktop = DesktopComputers("owner", views)

    def fail():
        attempted.append("browser")
        raise RuntimeError("Fixture failed cleanup")

    browser.close = fail
    desktop.close = lambda: attempted.append("desktop")
    runtime = BotRuntime(
        Store(tmp_path / "bots.db"),
        "owner",
        lambda _: None,
        browser_computers=browser,
        desktop_computers=desktop,
    )
    with pytest.raises(RuntimeError, match="retained service ownership"):
        runtime.stop()
    assert attempted == ["browser", "desktop"]


@pytest.mark.parametrize("cleanup_uncertain", [False, True])
def test_storage_failure_releases_other_surface_without_claiming_cleanup_succeeded(
    tmp_path, cleanup_uncertain
):
    views = ComputerViews()
    browser = BrowserComputers("owner", views, lambda *_: None)
    desktop = DesktopComputers("owner", views)
    attempted = []

    def storage_failure(*args, **kwargs):
        attempted.append("browser")
        raise ObjectStorageError("private upstream detail")

    def other_surface(*args, **kwargs):
        attempted.append("desktop")
        if cleanup_uncertain:
            raise RuntimeError("private cleanup detail")

    browser.release, desktop.release = storage_failure, other_surface
    runtime = BotRuntime(
        Store(tmp_path / "bots.db"),
        "owner",
        lambda _: None,
        browser_computers=browser,
        desktop_computers=desktop,
    )
    with pytest.raises(RuntimeError) as raised:
        runtime._release_computers("task", cancelled=False)
    assert attempted == ["browser", "desktop"]
    assert "private" not in str(raised.value)
    assert isinstance(raised.value, ObjectStorageError) is not cleanup_uncertain
    if cleanup_uncertain:
        assert "cleanup is pending" in str(raised.value)


@pytest.fixture
def study(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Work offline"))
    chat = store.create_chat("owner", bot["id"], "Conversation")
    store.enqueue("owner", chat["id"], TaskInput(message="Prepare work", request_id="prepare-work"))
    task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    proposal = proposals.propose(
        task,
        ActionProposal(
            question="Run the offline task?",
            instructions="Inspect the requested file.",
            offline_computer=True,
        ),
    )
    store.finish("owner", task["id"], "completed", content="Please review the scope.")
    proposals.decide(
        bot["id"],
        proposal["id"],
        ActionDecision(action="accept", expected_revision=1, offline_computer=True),
    )
    task = store.claim("owner")
    instances = []

    def factory(*args, **kwargs):
        executor = Executor(*args, **kwargs)
        instances.append(executor)
        return executor

    manager = DesktopComputers("owner", ComputerViews(), factory=factory)
    yield store, task, manager, instances
    manager.close()


def run_task(runtime, task):
    """Drive foreground admission and the durable execution lane separately."""
    runtime.run(task, runtime.task_token(task["id"]))
    queued = runtime.background.claim()
    if queued is not None:
        assert queued["id"] == task["id"]
        runtime.run(queued, runtime.task_token(queued["id"]), background=True)


@pytest.mark.parametrize("image_input", [False, True])
def test_approved_route_delivers_real_tool_shape_without_text_only_pixels(study, image_input):
    store, task, manager, instances = study
    name = "computer_observe" if image_input else "computer_run"
    args = {} if image_input else {"command": "fixture"}
    client = ClientFixture(
        [response(name=name, arguments=args), response("Reported actual result")]
    )
    client._alysis_browser_image_input = image_input
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    run_task(runtime, task)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert len(instances) == 1 and instances[0].closed
    assert not manager._computers
    assert ("computer_observe" in advertised_names(client.requests[0])) is image_input
    messages = client.requests[-1]["messages"]
    receipt = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
    if image_input:
        image = next(
            part
            for m in messages
            if isinstance(m.get("content"), list)
            for part in m["content"]
            if part["type"] == "image_url"
        )
        assert image["image_url"]["url"].startswith("data:image/png;base64,")
        assert receipt["frame"]["surface"] == "offline_desktop"
        assert "png" not in receipt
    else:
        assert receipt["exit_code"] == 0
    assert store.usage("owner")["calls"] == 2


@pytest.mark.parametrize("restriction", ["no_grant", "text_only", "activation", "schedule"])
def test_forged_or_ineligible_visual_call_never_allocates(study, monkeypatch, restriction):
    store, task, manager, instances = study
    client = ClientFixture([response(name="computer_observe"), response("Unavailable")])
    client._alysis_browser_image_input = restriction != "text_only"
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    if restriction == "no_grant":
        with store.connect(write=True) as db:
            db.execute("UPDATE action_proposals SET offline_computer=0")
    elif restriction == "activation":
        task["config"]["activation"] = {"event": "file_added"}
    elif restriction == "schedule":
        monkeypatch.setattr(store, "schedule_context", lambda *_: {"request": "Read the page"})
    run_task(runtime, task)
    assert "computer_observe" not in advertised_names(client.requests[0])
    assert not instances
    assert any(
        json.loads(m["content"]).get("error")
        for m in client.requests[-1]["messages"]
        if m["role"] == "tool"
    )


def test_computer_grant_is_rechecked_before_dispatch(study, monkeypatch):
    store, task, manager, instances = study
    client = ClientFixture(
        [response(name="computer_run", arguments={"command": "fixture"}), response()]
    )
    original = client.chat

    def revoke(**kwargs):
        reply = original(**kwargs)
        with store.connect(write=True) as db:
            db.execute("UPDATE action_proposals SET offline_computer=0")
        return reply

    client.chat = revoke
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    run_task(runtime, task)
    assert "computer_run" in advertised_names(client.requests[0])
    assert not instances
    assert any(
        "approval" in m["content"] for m in client.requests[-1]["messages"] if m["role"] == "tool"
    )


def test_offline_backend_configuration_is_explicit_linux_only_and_lazy(tmp_path, monkeypatch):
    settings = Settings(
        tmp_path / "desktop.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    assert create_app(settings).state.runtime.desktop_computers is None
    with pytest.raises(ValueError, match="boolean"):
        replace(settings, desktop_enabled="false")
    combined = replace(settings, desktop_enabled=True, browser_enabled=True)
    assert combined.desktop_enabled and combined.browser_enabled
    monkeypatch.setattr("alysis_code.bots.app.platform.system", lambda: "Darwin")
    with pytest.raises(ValueError, match="Linux"):
        create_app(replace(settings, desktop_enabled=True))
    monkeypatch.setattr("alysis_code.bots.app.platform.system", lambda: "Linux")
    manager = create_app(replace(settings, desktop_enabled=True)).state.runtime.desktop_computers
    assert manager.views.frame(settings.owner, "bot") == {"state": "not_running"}
    assert not manager._computers
    manager.close()


@pytest.mark.parametrize(
    "scope", [{"offline_computer": True}, {"browser_origin": "https://example.org"}]
)
def test_disabled_computer_cannot_offer_unusable_permissions(study, scope):
    store, task, _, instances = study
    runtime = BotRuntime(store, "owner", lambda _: None)
    with pytest.raises(Conflict, match="unavailable"):
        runtime._tool(
            task,
            "propose_action",
            {
                "question": "Run this?",
                "instructions": "Do unavailable work",
                "offline_computer": False,
                "browser_origin": None,
                **scope,
            },
            ["propose_action"],
        )
    assert not instances


def test_execution_cannot_spawn_peer_and_retires_launched_application(study):
    store, task, manager, instances = study
    peer = store.save_bot("owner", BotInput(name="Peer", purpose="Review only"))
    client = ClientFixture(
        [
            response(name="computer_launch", arguments={"command": "synthetic delayed write"}),
            response(
                name="request_peer", arguments={"recipient_bot_id": peer["id"], "brief": "Review"}
            ),
            response("Peer review is unavailable during this execution."),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    run_task(runtime, task)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert runtime.peers.for_task(task["id"]) is None
    assert instances[0].closed
    runtime.cancel(task["id"])
    assert manager.reap() is True
    assert not manager._computers


def test_budget_failure_retires_launched_application(study):
    from alysis_code.bots.runtime import RuntimeLimits

    store, task, manager, instances = study
    client = ClientFixture(
        [
            response(name="computer_launch", arguments={"command": "delayed write"}),
            response(name="computer_run", arguments={"command": "must not run"}),
        ]
    )
    runtime = BotRuntime(
        store,
        "owner",
        lambda _: client,
        desktop_computers=manager,
        limits=RuntimeLimits(calls_per_task=2),
    )
    run_task(runtime, task)
    assert store.task("owner", task["id"])["status"] == "failed"
    assert instances[0].closed and not manager._computers


def test_failed_cleanup_retains_handles_and_reports_uncertainty(study):
    store, task, manager, instances = study
    client = ClientFixture(
        [
            response(name="computer_launch", arguments={"command": "fixture"}),
            RuntimeError("model failed"),
        ]
    )
    original = manager.factory

    def fail_close(*args, **kwargs):
        executor = original(*args, **kwargs)
        executor.fail_close = True
        return executor

    manager.factory = fail_close
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    try:
        run_task(runtime, task)
        current = store.task("owner", task["id"])
        assert current["status"] == "failed" and runtime.browser_cleanup_pending
        assert any(e["kind"] == "computer_cleanup_pending" for e in current["events"])
        assert instances[0].workspace.exists() and manager._computers[task["bot_id"]].retired
    finally:
        instances[0].fail_close = False
    manager.reap()
    assert instances[0].closed and not manager._computers


def test_stop_before_completion_commit_cannot_leave_detached_app(study, monkeypatch):
    store, task, manager, instances = study
    client = ClientFixture(
        [
            response(name="computer_launch", arguments={"command": "synthetic delayed write"}),
            response("The application was launched."),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    finish = store.finish

    def stop_before_commit(owner, task_id, status, **kwargs):
        if task_id == task["id"] and status == "completed":
            runtime.cancel(task_id)
        return finish(owner, task_id, status, **kwargs)

    monkeypatch.setattr(store, "finish", stop_before_commit)
    run_task(runtime, task)
    assert store.task("owner", task["id"])["status"] == "cancelled"
    assert instances[0].closed and not manager._computers


def test_mixed_background_batch_rejected_whole_then_repairs_within_budget(study):
    from alysis_code.llm.types import LLMResponse, ToolCall

    store, task, manager, instances = study
    mixed = LLMResponse(
        "",
        [
            ToolCall("handoff", "continue_in_background", {"summary": "Work locally"}),
            ToolCall("action", "computer_run", {"command": "must not run"}),
        ],
        {},
    )
    client = ClientFixture(
        [
            mixed,
            response(name="computer_run", arguments={"command": "approved after repair"}),
            response("Finished the corrected batch."),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    run_task(runtime, task)
    assert store.task("owner", task["id"])["status"] == "completed"
    feedback = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in feedback] == ["handoff", "action"]
    assert all(json.loads(m["content"])["executed"] is False for m in feedback)
    assert store.usage("owner")["calls"] == 3
    assert instances[0].calls == [("run", {"command": "approved after repair", "timeout": 10})]
    assert instances[0].closed and store.task("owner", task["id"])["status"] == "completed"


def test_repeated_invalid_control_batches_exhaust_budget_without_any_action(study):
    from alysis_code.bots.runtime import RuntimeLimits
    from alysis_code.llm.types import LLMResponse, ToolCall

    store, task, manager, instances = study
    mixed = LLMResponse(
        "",
        [
            ToolCall("handoff", "continue_in_background", {"summary": "Work locally"}),
            ToolCall("action", "computer_run", {"command": "must not run"}),
        ],
        {},
    )
    client = ClientFixture([mixed, mixed])
    runtime = BotRuntime(
        store,
        "owner",
        lambda _: client,
        desktop_computers=manager,
        limits=RuntimeLimits(calls_per_task=2),
    )
    run_task(runtime, task)
    assert not instances and not runtime.background.active(task["bot_id"])
    assert store.task("owner", task["id"])["status"] == "failed"
    assert store.usage("owner")["calls"] == 2


def test_later_plain_request_is_its_own_computer_permission(study):
    store, previous, manager, instances = study
    store.finish("owner", previous["id"], "completed", content="Earlier work completed.")
    current = store.enqueue(
        "owner",
        previous["chat_id"],
        TaskInput(
            message="Create a chart from my data and return it as an image.",
            request_id="new-file-task",
        ),
    )
    task = store.claim("owner")
    assert task["id"] == current["id"]
    # The earlier task's grant does not carry over to a new request.
    assert not ProposalStore(store, "owner").offline_computer_allowed(task)
    client = ClientFixture(
        [
            response(
                name="propose_action",
                arguments={
                    "question": "May I use the computer to create the chart?",
                    "instructions": "Read the actual source data, generate and verify the requested image, and save it to the library.",
                    "offline_computer": True,
                    "browser_origin": None,
                },
            ),
            response(name="computer_run", arguments={"command": "fixture"}),
            response("Reported actual result"),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    run_task(runtime, task)
    # Ordinary turn start is unchanged: computer tools come only after the request asks for them.
    names = advertised_names(client.requests[0])
    assert "propose_action" in names and "computer_run" not in names
    # The person's own request approved it: the same task continued on the computer, no card.
    assert "computer_run" in advertised_names(client.requests[1])
    proposals = ProposalStore(store, "owner").list(task["bot_id"])
    assert not [p for p in proposals if p["status"] == "pending"]
    grant = [p for p in proposals if p["source_task_id"] == task["id"]]
    assert len(grant) == 1 and grant[0]["status"] == "accepted"
    assert grant[0]["task_id"] == task["id"] and grant[0]["offline_computer"] == 1
    assert store.task("owner", task["id"])["status"] == "completed"
    assert len(instances) == 1 and instances[0].closed
    assert not manager._computers
    assert store.usage("owner")["calls"] == 3


def test_request_moved_to_the_background_first_still_approves_its_own_computer(study):
    store, previous, manager, instances = study
    store.finish("owner", previous["id"], "completed", content="Earlier work completed.")
    store.enqueue(
        "owner",
        previous["chat_id"],
        TaskInput(
            message="Research the figures and put them in a spreadsheet for me.",
            request_id="background-first",
        ),
    )
    task = store.claim("owner")
    client = ClientFixture(
        [
            response(
                "I'll research the figures and build the spreadsheet.",
                name="continue_in_background",
                arguments={"summary": "Research the figures and build the spreadsheet."},
            ),
            response(
                name="propose_action",
                arguments={
                    "question": "May I use the computer to build the spreadsheet?",
                    "instructions": "Build and check the spreadsheet from the researched figures.",
                    "offline_computer": True,
                    "browser_origin": None,
                },
            ),
            response(name="computer_run", arguments={"command": "fixture"}),
            response("Reported actual result"),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.run(task, runtime.task_token(task["id"]))
    turns = 0
    while (queued := runtime.background.claim()) is not None:
        turns += 1
        assert queued["id"] == task["id"]
        runtime.run(queued, runtime.task_token(queued["id"]), background=True)
    # The background turn asked for the computer, got it from the request itself and
    # restarted once with the computer tools; nobody had to press Do it.
    assert turns == 2
    assert "computer_run" not in advertised_names(client.requests[1])
    assert "computer_run" in advertised_names(client.requests[2])
    proposals = ProposalStore(store, "owner").list(task["bot_id"])
    assert not [p for p in proposals if p["status"] == "pending"]
    grant = [p for p in proposals if p["source_task_id"] == task["id"]]
    assert len(grant) == 1 and grant[0]["status"] == "accepted"
    assert grant[0]["task_id"] == task["id"]
    assert store.task("owner", task["id"])["status"] == "completed"
    assert len(instances) == 1 and instances[0].closed
    assert not manager._computers
    assert store.usage("owner")["calls"] == 4


def test_uncertain_computer_action_ends_task_without_another_model_turn(study, monkeypatch):
    from alysis_code.bots.desktop_linux import ComputerActionUncertain

    store, task, manager, instances = study
    calls = []

    def lost_receipt(self, owner, operation, args, *, cancel):
        calls.append((operation, args))
        raise ComputerActionUncertain("Unconfirmed fixture dispatch")

    monkeypatch.setattr(Executor, "call", lost_receipt)
    client = ClientFixture([response(name="computer_run", arguments={"command": "one-write"})])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    run_task(runtime, task)
    final = store.task("owner", task["id"])
    assert final["status"] == "interrupted"
    assert "without repeating" in final["error"]
    assert len(client.requests) == len(calls) == 1
    assert len(instances) == 1 and instances[0].closed
    assert not manager._computers
