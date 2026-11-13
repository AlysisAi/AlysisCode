"""Scripted durable admission/recovery evidence, not native/model qualification."""

import json
from dataclasses import replace

import pytest
from test_bots_action_review import review_response
from test_bots_browser_runtime import ClientFixture, response
from test_bots_desktop_runtime import run_task
from test_bots_desktop_runtime import study as execution_study

from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.local_computers import (
    DeviceConnection,
    LocalComputerPending,
    LocalComputers,
    LocalDecision,
)
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_review import ReplyReview
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound, Store

desktop_study = execution_study

D = "a" * 32
S = "b" * 32
ARGS = {"device_id": D, "command": "printf 'Καλημέρα 東京'", "cwd": "/tmp"}


@pytest.fixture
def study(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Host test", purpose="Test"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue(
        "owner",
        chat["id"],
        TaskInput(message="Run the synthetic command on my Mac", request_id="local-command"),
    )
    task = store.claim("owner")
    local = LocalComputers(store, "owner")
    local.connect(D, DeviceConnection(session_id=S, name="Test Mac", platform="darwin"))
    return store, task, local


def pending(study, *, park=True):
    store, task, local = study
    reviews = []
    with pytest.raises(LocalComputerPending):
        local.request(task, "local_command", ARGS, lambda *args: reviews.append(args))
    assert reviews[0][2]["operating_system"] == "darwin"
    if park:
        local.park(
            task,
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call",
                            "type": "function",
                            "function": {"name": "local_command", "arguments": json.dumps(ARGS)},
                        }
                    ],
                }
            ],
        )
    return local.connect(D, DeviceConnection(session_id=S, name="Test Mac", platform="darwin"))[
        "requests"
    ][0]


def test_local_permission_waits_for_saved_checkpoint(study):
    _, task, local = study
    job = pending(study, park=False)
    assert not job["ready"]
    with pytest.raises(Conflict):
        local.decide(D, job["id"], LocalDecision(action="claim", session_id=S))
    local.park(task, [])
    assert local.decide(D, job["id"], LocalDecision(action="claim", session_id=S))["claimed"]


def test_connected_native_command_cannot_be_put_behind_a_no_access_proposal(study):
    from test_bots_browser_runtime import ClientFixture, advertised_names, response

    store, task, local = study
    # A stale/incorrect provider call must not recreate the extra Do it card,
    # even though that function is absent from its offered tools.
    client = ClientFixture(
        [
            response(
                name="propose_action",
                arguments={
                    "question": "Run it?",
                    "instructions": task["message"],
                    "offline_computer": False,
                },
            ),
            response(name="local_command", arguments=ARGS),
            response(
                json.dumps(
                    {
                        "decision": "allow",
                        "risk": "low",
                        "user_authorization": "high",
                        "reason": "Requested read-only fixture command",
                    }
                )
            ),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client)
    runtime.action_review_enabled = True
    runtime.run(task, runtime.task_token(task["id"]))
    assert all("propose_action" not in advertised_names(request) for request in client.requests)
    assert not runtime.proposals.list(task["bot_id"])
    assert store.task("owner", task["id"])["wait_reason"] == "local_approval"
    jobs = local.connect(D, DeviceConnection(session_id=S, name="Test Mac", platform="darwin"))[
        "requests"
    ]
    assert len(jobs) == 1 and jobs[0]["arguments"] == ARGS and jobs[0]["ready"]


def test_native_device_does_not_remove_real_private_computer_access_proposals(study):
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.runtime import tool_schema

    store, task, _ = study
    desktop = DesktopComputers("owner", ComputerViews())
    runtime = BotRuntime(store, "owner", lambda _: None, desktop_computers=desktop)
    try:
        schemas = runtime._operational_schemas(task, [], [tool_schema("propose_action")])
        assert len(schemas) == 1
        function = schemas[0]["function"]
        assert (
            "must request an actual website or private Linux permission" in function["description"]
        )
        # Model APIs reject combinators at the top of a tool schema; the rule is enforced below.
        assert not {"anyOf", "oneOf", "allOf"} & set(function["parameters"])
        with pytest.raises(Conflict, match="handles its own command approval"):
            runtime._tool(
                task,
                "propose_action",
                {
                    "question": "Run?",
                    "instructions": "Read on the connected Mac",
                    "offline_computer": False,
                },
                ["propose_action"],
            )
        requested = runtime._tool(
            task,
            "propose_action",
            {
                "question": "Use the private Linux computer?",
                "instructions": "Make the requested spreadsheet in the private Linux workspace.",
                "offline_computer": True,
            },
            ["propose_action"],
        )
        assert requested["offline_computer"] is True
    finally:
        desktop.close()


@pytest.mark.parametrize("native_approval", [True, False])
@pytest.mark.parametrize("browser", [True, False])
@pytest.mark.parametrize("offline_computer", [True, False])
def test_no_tool_schema_has_a_top_level_combinator(offline_computer, browser, native_approval):
    # Anthropic rejects the whole request when one tool's input schema starts with anyOf,
    # oneOf or allOf, so a single such tool would fail every model call that offers it.
    from alysis_code.bots.runtime import TOOL_DEFINITIONS, tool_schema

    for name in TOOL_DEFINITIONS:
        parameters = tool_schema(
            name,
            offline_computer=offline_computer,
            browser=browser,
            native_approval=native_approval,
        )["function"]["parameters"]
        assert parameters.get("type") == "object", name
        assert not {"anyOf", "oneOf", "allOf"} & set(parameters), name


def test_runtime_facts_say_whether_pages_can_be_seen_as_images(study):
    # Without the screenshot tool an owl still reads pages as text; the facts say so, so the
    # owl (and the reply checker) doesn't call the whole browser unavailable.
    from alysis_code.bots.runtime import tool_schema

    store, task, _ = study
    runtime = BotRuntime(store, "owner", lambda _: None)
    screenshot = {
        "type": "function",
        "function": {"name": "browser_screenshot", "description": "", "parameters": {}},
    }
    seen = []
    for extra in ([], [screenshot]):
        messages = [{"role": "system", "content": "Owl instructions"}]
        runtime._operational_schemas(task, messages, [tool_schema("read_file"), *extra])
        facts = next(
            m["content"]
            for m in messages
            if str(m["content"]).startswith("Current execution availability")
        )
        assert "website_screenshots=false means pages can be read as text" in facts
        seen.append(facts.split("website_screenshots")[1][:8])
    assert "false" in seen[0] and "true" in seen[1]


def test_receipt_survives_restart_queues_once_and_never_reclaims(study):
    store, task, local = study
    job = pending(study)
    assert store.task("owner", task["id"])["wait_reason"] == "local_approval"
    claim = LocalDecision(action="claim", session_id=S)
    assert local.decide(D, job["id"], claim) == local.decide(D, job["id"], claim)
    assert store.task("owner", task["id"])["wait_reason"] == "local_result"
    assert store.recover("owner") == 0
    other = LocalComputers(Store(store.path), "owner")
    other.connect(D, DeviceConnection(session_id="c" * 32, name="Test Mac", platform="darwin"))
    with pytest.raises(Conflict):
        other.decide(D, job["id"], LocalDecision(action="claim", session_id="c" * 32))
    result = LocalDecision(
        action="result",
        session_id=S,
        result={"executed": True, "stdout": "Καλημέρα 東京", "exit_code": 0},
    )
    other.decide(D, job["id"], result)
    other.decide(D, job["id"], result)
    events = store.task("owner", task["id"])["events"]
    assert len([e for e in events if e["kind"] == "local_computer_result"]) == 1
    background = BackgroundStore(store, "owner")
    assert background.claim()["id"] == task["id"]
    assert background.claim() is None
    assert "Καλημέρα 東京" in str(background.checkpoint(task["id"]))
    with pytest.raises(Conflict):
        other.decide(D, job["id"], claim)


@pytest.mark.parametrize("claimed", [False, True])
def test_stop_fences_admission_and_waits_for_running_ack(study, claimed):
    store, task, local = study
    job = pending(study)
    if claimed:
        local.decide(D, job["id"], LocalDecision(action="claim", session_id=S))
    stopped = store.cancel("owner", task["id"])
    assert stopped["status"] == ("cancelling" if claimed else "cancelled")
    if not claimed:
        with pytest.raises(Conflict):
            local.decide(D, job["id"], LocalDecision(action="claim", session_id=S))
    else:
        local.decide(
            D,
            job["id"],
            LocalDecision(
                action="result", session_id=S, result={"status": "stopped", "executed": True}
            ),
        )
    assert store.task("owner", task["id"])["status"] == "cancelled"
    assert BackgroundStore(store, "owner").claim() is None


def test_correction_invalidates_pending_action_and_cannot_restart_old_plan(study):
    store, task, local = study
    job = pending(study)
    store.enqueue(
        "owner",
        task["chat_id"],
        TaskInput(message="Do not write anything", request_id="correction"),
    )
    with pytest.raises(Conflict):
        local.decide(D, job["id"], LocalDecision(action="claim", session_id=S))
    current = local.connect(D, DeviceConnection(session_id=S, name="Test Mac", platform="darwin"))[
        "requests"
    ][0]
    assert current["cancel_requested"]
    local.decide(D, job["id"], LocalDecision(action="deny", session_id=S))
    assert store.task("owner", task["id"])["status"] == "interrupted"


def test_review_failure_offline_and_owner_separation_never_enqueue(study):
    store, task, local = study
    with pytest.raises(Conflict):
        local.request(task, "local_command", ARGS, None)

    def deny(*_):
        raise Conflict("Denied")

    with pytest.raises(Conflict):
        local.request(task, "local_command", ARGS, deny)
    other = LocalComputers(store, "other")
    assert other.devices() == []
    with pytest.raises(NotFound):
        other.connect(D, DeviceConnection(session_id=S, name="Test", platform="darwin"))
    local.clock = lambda: 10**12
    with pytest.raises(Conflict, match="offline"):
        local.request(task, "local_command", ARGS, lambda *_: None)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM bot_local_requests").fetchone()[0] == 0


def test_correction_during_review_never_creates_local_request(study):
    store, task, local = study

    def correction(*_):
        store.enqueue(
            "owner", task["chat_id"], TaskInput(message="Stop", request_id="new-instruction")
        )

    with pytest.raises(Conflict):
        local.request(task, "local_command", ARGS, correction)


def test_runtime_real_review_pause_receipt_and_mandatory_completion_gate(study):
    store, task, local = study
    client = ClientFixture(
        [
            response(name="local_command", arguments=ARGS),
            review_response("allow", "Synthetic command requested"),
            response("The command returned Καλημέρα 東京, exit 0."),
            response('{"decision":"ready","reason":"Matches the native receipt"}'),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.action_review_enabled = True
    runtime.reply_review = ReplyReview(runtime)
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "waiting"
    job = local.connect(D, DeviceConnection(session_id=S, name="Test Mac", platform="darwin"))[
        "requests"
    ][0]
    local.decide(D, job["id"], LocalDecision(action="claim", session_id=S))
    local.decide(
        D,
        job["id"],
        LocalDecision(
            action="result",
            session_id=S,
            result={"stdout": "Καλημέρα 東京", "exit_code": 0, "executed": True},
        ),
    )
    queued = runtime.background.claim()
    runtime.run(queued, runtime.task_token(task["id"]), background=True)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed", final["error"]
    assert len(client.requests) == 4
    assert any(e["kind"] == "action_review_call" for e in final["events"])
    assert any(e["kind"] == "reply_review_started" for e in final["events"])


def test_runtime_declined_command_stops_the_task(study):
    store, task, local = study
    client = ClientFixture(
        [
            response(name="local_command", arguments=ARGS),
            review_response("allow", "Synthetic command requested"),
            response("I did not run it on your Mac, so the greeting is unchecked."),
            response('{"decision":"ready","reason":"Honest about the declined command"}'),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.action_review_enabled = True
    runtime.reply_review = ReplyReview(runtime)
    runtime.run(task, runtime.task_token(task["id"]))
    job = local.connect(D, DeviceConnection(session_id=S, name="Test Mac", platform="darwin"))[
        "requests"
    ][0]
    local.decide(D, job["id"], LocalDecision(action="deny", session_id=S))
    # "Deny and stop" ends the task: the owl is not resumed to write more.
    assert runtime.background.claim() is None
    final = store.task("owner", task["id"])
    assert final["status"] == "cancelled"
    assert (
        final["error"]
        == "You declined the work on your computer, so this task stopped. Nothing ran."
    )
    assert len(client.requests) == 2  # the request and its action review, nothing after


@pytest.mark.parametrize(
    "result",
    [
        {"executed": "unknown", "status": "interrupted"},
        {"executed": True, "status": "stopped"},
    ],
)
def test_uncertain_or_stopped_execution_cannot_resume_to_retry(study, result):
    store, task, local = study
    job = pending(study)
    local.decide(D, job["id"], LocalDecision(action="claim", session_id=S))
    local.decide(D, job["id"], LocalDecision(action="result", session_id=S, result=result))
    assert store.task("owner", task["id"])["status"] in {"cancelled", "interrupted"}
    assert BackgroundStore(store, "owner").claim() is None


def test_declined_request_ends_the_task_and_cannot_be_asked_again(study):
    store, task, local = study
    job = pending(study)
    local.decide(D, job["id"], LocalDecision(action="deny", session_id=S))
    # A parked task ends too: nothing resumes it after the person said Deny and stop.
    assert BackgroundStore(store, "owner").claim() is None
    final = store.task("owner", task["id"])
    assert final["status"] == "cancelled" and "declined" in final["error"]
    # Nothing in the same task can ask for that computer again, not even a different command.
    with pytest.raises(Conflict, match="declined"):
        local.request(
            task,
            "local_command",
            {**ARGS, "command": "echo other"},
            lambda *_: pytest.fail("A declined task reached review again"),
        )
    with store.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM bot_local_requests WHERE status='pending'").fetchone()[
                0
            ]
            == 0
        )


class Reviewed(Exception):
    pass


def reached_review(*_):
    raise Reviewed


def test_a_transfer_that_failed_without_changing_anything_is_not_asked_again(study):
    store, task, local = study
    export = {"device_id": D, "file_id": "f" * 32, "path": "/tmp/report.txt"}
    with pytest.raises(LocalComputerPending):
        local.request(task, "export_local_file", export, lambda *_: None)
    call = {"name": "export_local_file", "arguments": json.dumps(export)}
    local.park(
        task,
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call", "type": "function", "function": call}],
            }
        ],
    )
    connection = DeviceConnection(session_id=S, name="Test Mac", platform="darwin")
    job = local.connect(D, connection)["requests"][0]
    local.decide(D, job["id"], LocalDecision(action="claim", session_id=S))
    refused = (
        "A file already exists at /tmp/report.txt; it was not overwritten. Nothing was changed."
    )
    receipt = {"executed": False, "status": "failed", "message": refused}
    local.decide(D, job["id"], LocalDecision(action="result", session_id=S, result=receipt))
    # The owl hears why and carries on; the same request is not put to the person again.
    background = BackgroundStore(store, "owner")
    assert background.claim()["id"] == task["id"]
    assert refused in str(background.checkpoint(task["id"]))
    with pytest.raises(Conflict, match="already failed: A file already exists"):
        local.request(task, "export_local_file", export, reached_review)
    with pytest.raises(Reviewed):
        local.request(
            task, "export_local_file", {**export, "path": "/tmp/report-2.txt"}, reached_review
        )


def test_decline_before_the_checkpoint_still_ends_the_task(study):
    store, task, local = study
    job = pending(study, park=False)
    local.decide(D, job["id"], LocalDecision(action="deny", session_id=S))
    assert store.task("owner", task["id"])["status"] == "cancelled"
    assert BackgroundStore(store, "owner").claim() is None


@pytest.mark.parametrize("arguments", [{**ARGS, "cwd": "."}, {**ARGS, "command": "bad\0command"}])
def test_invalid_host_paths_fail_before_review_or_queue(study, arguments):
    _, task, local = study
    with pytest.raises(ValueError):
        local.request(
            task,
            "local_command",
            arguments,
            lambda *_: pytest.fail("Invalid command reached review"),
        )


def test_review_mode_revocation_invalidates_local_admission(study):
    from alysis_code.bots.action_review import ActionReviews, ReviewSettings

    store, task, local = study
    job = pending(study)
    ActionReviews(store, "owner").settings(task["bot_id"], ReviewSettings(mode="review"))
    with pytest.raises(Conflict):
        local.decide(D, job["id"], LocalDecision(action="claim", session_id=S))


def test_native_api_requires_auth_and_does_not_echo_malformed_data(tmp_path):
    import hashlib

    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "api.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings)
    with TestClient(app) as client:
        path = f"/v1/local-computers/{D}/connect"
        assert client.post(path, json={}).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        credential = "device_" + "a" * 64
        assert (
            client.post(
                f"/v1/local-computers/{D}/pair",
                json={
                    "session_id": S,
                    "name": "Test",
                    "platform": "darwin",
                    "token_hash": hashlib.sha256(credential.encode()).hexdigest(),
                },
            ).status_code
            == 200
        )
        client.headers["Authorization"] = "Bearer " + credential
        assert (
            client.post(
                path, json={"session_id": S, "name": "Test", "platform": "darwin"}
            ).status_code
            == 200
        )
        bad = client.post(
            f"/v1/local-computers/{D}/requests/{D}",
            json={
                "session_id": "private-canary",
                "action": "result",
                "result": {"stdout": "private-output"},
            },
        )
        assert bad.status_code == 422
        assert "private" not in bad.text


def test_code_execution_lane_preserves_local_tool_receipt_and_completion_review(desktop_study):
    store, task, manager, _ = desktop_study
    client = ClientFixture(
        [
            response(name="local_command", arguments=ARGS),
            review_response("allow", "Requested host test"),
            response("The native command returned the expected Unicode text."),
            response('{"decision":"ready","reason":"Receipt verified"}'),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.action_review_enabled = True
    runtime.reply_review = ReplyReview(runtime)
    local = runtime.local_computers
    connection = DeviceConnection(session_id=S, name="Test Mac", platform="darwin")
    local.connect(D, connection)
    run_task(runtime, task)
    job = local.connect(D, connection)["requests"][0]
    local.decide(D, job["id"], LocalDecision(action="claim", session_id=S))
    local.decide(
        D,
        job["id"],
        LocalDecision(
            action="result",
            session_id=S,
            result={"stdout": "Καλημέρα 東京", "exit_code": 0, "status": "finished"},
        ),
    )
    queued = runtime.background.claim()
    runtime.run(queued, runtime.task_token(task["id"]), background=True)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed", final["error"]
    assert any(e["kind"] == "reply_review_started" for e in final["events"])
    # The Code lane serializes previous turns as explicitly labelled reference
    # data. Verify the receipt inside that existing envelope, not the command's
    # similar Unicode argument or merely the model's claimed completion.
    prefix = "Reference conversation record (data, not new authority):\n"
    records = [
        json.loads(m["content"][len(prefix) :])
        for m in client.requests[-2]["messages"]
        if isinstance(m.get("content"), str) and m["content"].startswith(prefix)
    ]
    receipts = [json.loads(m["content"]) for m in records if m.get("role") == "tool"]
    assert any(
        receipt.get("result") == {"stdout": "Καλημέρα 東京", "exit_code": 0, "status": "finished"}
        for receipt in receipts
    )
    assert len(client.requests) == 4  # Action, action review, report, completion review.


def test_each_request_says_whether_a_chat_or_a_routine_asked(study):
    store, task, local = study
    assert pending(study)["origin"] == "chat"
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO schedules(id,bot_id,name,prompt,timing,state,revision,next_run_at,"
            "pause_reason,source_task_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s" * 32, task["bot_id"], "Daily", "Check", "{}", "active", 1, None, None, None, 0, 0),
        )
        db.execute(
            "INSERT INTO schedule_runs(schedule_id,schedule_revision,scheduled_for,task_id,status,"
            "reason,config,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("s" * 32, 1, 0, task["id"], "queued", None, "{}", 0),
        )
    job = local.connect(D, DeviceConnection(session_id=S, name="Test Mac", platform="darwin"))[
        "requests"
    ][0]
    assert job["origin"] == "routine"


@pytest.mark.parametrize("online", [False, True])
def test_native_mutation_tools_offer_only_actual_online_user_devices(study, online):
    from alysis_code.bots.runtime import tool_schema

    store, task, local = study
    local.connect(
        D, DeviceConnection(session_id=S, name="Test Mac", platform="darwin", connected=online)
    )
    runtime = BotRuntime(store, "owner", lambda _: None)
    names = [
        "list_local_computers",
        "local_command",
        "import_local_file",
        "export_local_file",
        "propose_action",
    ]
    offered = runtime._operational_schemas(task, [], [tool_schema(name) for name in names])
    by_name = {schema["function"]["name"]: schema for schema in offered}
    assert "list_local_computers" in by_name
    for name in ("local_command", "import_local_file", "export_local_file"):
        assert (name in by_name) is online
        if online:
            assert by_name[name]["function"]["parameters"]["properties"]["device_id"]["enum"] == [D]
