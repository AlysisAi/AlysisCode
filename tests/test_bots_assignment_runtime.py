import json
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import AssignmentInput, BotInput, ReplyReviewSettings, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, ToolCall


def wait_for_assignment(client, path, status):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(path)
        response.raise_for_status()
        for item in response.json()["items"]:
            if item["status"] == status and (status != "proposed" or item["ready"]):
                return item
        time.sleep(0.02)
    pytest.fail(f"Assignment did not reach {status}: {response.json()}")


@pytest.mark.parametrize("supported", [True, False])
def test_assistant_model_can_request_a_screen_approval(tmp_path, supported):
    settings = Settings(
        tmp_path / "screen.db",
        "t" * 40,
        "fixture",
        "https://example.invalid",
        "fixture",
        owner="person",
    )
    goal = "Έλεγχος του εντύπου"
    offered = []

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, *, messages, tools, **_kwargs):
            if any(item["role"] == "tool" for item in messages):
                return LLMResponse("Μπορώ να βοηθήσω.", [], {})
            offered.append({tool["function"]["name"] for tool in tools})
            calls = (
                [ToolCall("screen", "request_screen_context", {"goal": goal})] if supported else []
            )
            return LLMResponse("" if calls else "Περίγραψέ μου τι βλέπεις.", calls, {})

    app = create_app(settings, client_factory=lambda _: Client())
    store = app.state.store
    store.reply_review_settings("person", ReplyReviewSettings(enabled=False))
    assistant = store.set_assistant("person", True)["bot"]
    chat = store.create_chat("person", assistant["id"], "Screen help")
    with TestClient(app, headers={"Authorization": "Bearer " + settings.token}) as client:
        assert "owl_assistant_screen_requests" in client.get("/v1/status").json()["capabilities"]
        path = f"/v1/chats/{chat['id']}"
        response = client.post(
            path + "/tasks",
            json={
                "message": "Μπορείς να δεις πού έχω κολλήσει στο έντυπο;",
                "request_id": uuid4().hex,
                **({"assistant_screen_requests": True} if supported else {}),
            },
        )
        response.raise_for_status()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            (task,) = client.get(path).json()["tasks"]
            if task["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert task["status"] == "completed", task
        assert ("request_screen_context" in offered[0]) is supported
        assert task.get("assistant_screen_request") == ({"goal": goal} if supported else None)


def test_chat_proposes_and_approved_assignment_runs_without_tools(tmp_path):
    settings = Settings(
        tmp_path / "assignments.db",
        "t" * 40,
        "fixture",
        "https://example.invalid",
        "fixture",
        owner="person",
    )
    calls = []
    recipient_id = ""
    brief = "Σύνταξε μια πρόσκληση για την Παρασκευή στις 15:00."
    expected_result = "Στο κείμενο αναφέρονται η ημέρα και η ώρα."
    worker_result = "Σας προσκαλούμε την Παρασκευή στις 15:00."
    private_marker = "unrelated conversation marker"

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, *, messages, tools, **kwargs):
            calls.append((messages, tools))
            if tools:
                if any(message["role"] == "tool" for message in messages):
                    return LLMResponse("Η ανάθεση περιμένει την έγκρισή σου.", [], {})
                offered = {tool["function"]["name"] for tool in tools}
                assert "propose_owl_assignment" in offered
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "assignment-call",
                            "propose_owl_assignment",
                            {
                                "recipient_bot_id": recipient_id,
                                "brief": brief,
                                "expected_result": expected_result,
                            },
                        )
                    ],
                    {},
                )
            assert private_marker not in str(messages)
            payload = json.loads(messages[-1]["content"])
            assert payload["brief"] == brief
            assert payload["expected_result"] == expected_result
            if "worker_result" not in payload:
                assert payload["recipient_name"] == "Νεφέλη"
                return LLMResponse(worker_result, [], {})
            assert payload["worker_result"] == worker_result
            return LLMResponse(
                '{"decision": "ready", "reason": "Περιλαμβάνει ημέρα και ώρα."}', [], {}
            )

    app = create_app(settings, client_factory=lambda _: Client())
    store = app.state.store
    store.reply_review_settings("person", ReplyReviewSettings(enabled=False))
    source = store.save_bot("person", BotInput(name="Συντονιστής"))
    recipient = store.save_bot("person", BotInput(name="Νεφέλη", purpose="Προσκλητήρια"))
    recipient_id = recipient["id"]
    source_chat = store.create_chat("person", source["id"], "Προσκλητήριο")
    recipient_chat = store.create_chat("person", recipient_id, "Previous work")
    older_task = store.enqueue(
        "person",
        recipient_chat["id"],
        TaskInput(message=private_marker, request_id=uuid4().hex),
    )
    assert store.claim("person")["id"] == older_task["id"]
    store.finish("person", older_task["id"], "completed", content=private_marker)
    headers = {"Authorization": "Bearer " + settings.token}
    route = f"/v1/bots/{source['id']}/assignments"
    with TestClient(app, headers=headers) as client:
        assert "owl_assignments" in client.get("/v1/status").json()["capabilities"]
        assert client.put(
            f"/v1/bots/{source['id']}/coordinator", json={"enabled": True}
        ).json() == {"coordinator": True}
        response = client.post(
            f"/v1/chats/{source_chat['id']}/tasks",
            json={"message": brief, "request_id": uuid4().hex},
        )
        response.raise_for_status()
        proposed = wait_for_assignment(client, route, "proposed")
        assert proposed["source_task_id"] == response.json()["id"]
        assert proposed["child_task_id"] is None
        assert all(tools for _, tools in calls)
        approve = f"{route}/{proposed['id']}"
        approved = client.patch(approve, json={"decision": "approve"})
        approved.raise_for_status()
        repeated = client.patch(approve, json={"decision": "approve"})
        repeated.raise_for_status()
        assert approved.json()["child_task_id"] == repeated.json()["child_task_id"]
        completed = wait_for_assignment(client, route, "completed")
        assert completed["result"] == worker_result
        assert completed["verification"] == {
            "status": "met",
            "detail": "Περιλαμβάνει ημέρα και ώρα.",
        }
        recipient_path = f"/v1/chats/{recipient_chat['id']}"
        for response in (
            client.get(recipient_path),
            client.post(recipient_path + "/page", json={}),
        ):
            response.raise_for_status()
            assert [item["id"] for item in response.json()["tasks"]] == [older_task["id"]]
    assert sum(tools is None for _, tools in calls) == 2
    assert [
        message["content"]
        for message in store.conversation("person", recipient_chat["id"])["messages"]
    ] == [private_marker, private_marker]
    persisted = Store(settings.database).list_assignments("person", source["id"])["items"][0]
    assert persisted == completed


@pytest.mark.parametrize("bad_result", ["tool_request", "unparseable_review"])
def test_assigned_task_never_executes_tools_and_preserves_unchecked_text(tmp_path, bad_result):
    store = Store(tmp_path / "assignments.db")
    owner = "person"
    coordinator = store.save_bot(owner, BotInput(name="Coordinator"))
    recipient = store.save_bot(owner, BotInput(name="Analyst"))
    store.set_coordinator(owner, coordinator["id"], True)
    chat = store.create_chat(owner, coordinator["id"], "Draft")
    parent = store.enqueue(
        owner, chat["id"], TaskInput(message="Draft a headline.", request_id=uuid4().hex)
    )
    assert store.claim(owner)["id"] == parent["id"]
    proposed = store.propose_assignment(
        owner,
        parent["id"],
        AssignmentInput(
            recipient_bot_id=recipient["id"],
            brief="Draft a headline about a library opening.",
            expected_result="One headline mentioning the library.",
        ),
    )
    store.finish(owner, parent["id"], "completed", content="Please approve the assignment.")
    store.decide_assignment(owner, coordinator["id"], proposed["id"], "approve")
    replies = iter(
        [
            LLMResponse(
                "",
                [ToolCall("forbidden", "propose_owl_assignment", {})],
                {},
            )
        ]
        if bad_result == "tool_request"
        else [
            LLMResponse("The library opens its doors", [], {}),
            LLMResponse("I cannot prepare a verdict.", [], {}),
        ]
    )

    class Client:
        model = "text-fixture"
        supports_tool_calling = False

        def chat(self, *, tools, **kwargs):
            assert tools is None
            return next(replies)

    runtime = BotRuntime(store, owner, lambda _: Client(), limits=RuntimeLimits())
    runtime.run(store.claim(owner), EventCancellationToken(threading.Event()))
    receipt = store.list_assignments(owner, coordinator["id"])["items"][0]
    if bad_result == "tool_request":
        assert receipt["status"] == "failed"
        assert receipt["result"] is None
        assert receipt["verification"] is None
    else:
        assert receipt["status"] == "completed"
        assert receipt["result"] == "The library opens its doors"
        assert receipt["verification"] == {"status": "unavailable", "detail": None}
    assert store.list_assignments(owner, recipient["id"])["items"] == []


@pytest.mark.parametrize("scope", ["workbook", "web_research"])
def test_scoped_assignment_needs_explicit_approval_and_returns_real_output(
    tmp_path, monkeypatch, scope
):
    from io import BytesIO

    from alysis_code.bots.store import Conflict

    if scope == "workbook":
        openpyxl = pytest.importorskip("openpyxl")
    owner = "person"
    store = Store(tmp_path / "assignments.db")
    coordinator = store.set_assistant(owner, True)["bot"]
    recipient = store.save_bot(owner, BotInput(name="Analyst"))
    chat = store.create_chat(owner, coordinator["id"], "Comparison")
    parent = store.enqueue(owner, chat["id"], TaskInput(message="Compare", request_id=uuid4().hex))
    assert store.claim(owner)["id"] == parent["id"]
    assignment = store.propose_assignment(
        owner,
        parent["id"],
        AssignmentInput(
            recipient_bot_id=recipient["id"],
            brief="Compare the documented registrations.",
            expected_result="A comparison with headcounts.",
            work_scope=scope,
        ),
    )
    store.finish(owner, parent["id"], "completed", content="Approve the handoff to Analyst.")
    with pytest.raises(Conflict, match="Review the assignment"):
        store.decide_assignment(owner, coordinator["id"], assignment["id"], "approve")
    assert store.claim(owner) is None
    queued = store.decide_assignment(owner, coordinator["id"], assignment["id"], "approve", scope)
    source = "https://example.com/registrations"
    monkeypatch.setattr("alysis_code.bots.runtime.web_tools_available", lambda: True)
    fetched = []

    def fetch(url, max_chars):
        fetched.append((url, max_chars))
        return {"url": source, "title": "Register", "text": "Κύμα A: 42", "status_code": 200}

    monkeypatch.setattr("alysis_code.bots.runtime.web_fetch", fetch)
    checked = []
    prompts = []

    class Client:
        model = "tool-fixture"
        supports_tool_calling = True

        def chat(self, *, messages, tools, **kwargs):
            if "worker_result" in messages[-1]["content"]:
                checked.append(json.loads(messages[-1]["content"]))
                prompts.append(messages[0]["content"])
                return LLMResponse('{"decision":"ready","reason":"Checked comparison."}', [], {})
            if messages[-1]["role"] == "tool":
                receipt = json.loads(messages[-1]["content"])
                assert scope == "web_research" and "source_number" in receipt
                return LLMResponse("Κύμα A: 42", [], {})
            names = {tool["function"]["name"] for tool in tools}
            assert "local_command" not in names and "propose_owl_assignment" not in names
            if scope == "web_research":
                assert names == {"web_search", "web_fetch"}
                call = ToolCall("fetch", "web_fetch", {"url": source})
            else:
                assert "create_assignment_workbook" in names
                assert names <= {
                    "create_assignment_workbook",
                    "describe_document_format",
                    "create_document",
                    "inspect_document",
                    "edit_document",
                    "check_document",
                }
                call = ToolCall(
                    "xlsx",
                    "create_assignment_workbook",
                    {
                        "name": "Εγγραφές",
                        "sheets": [{"name": "Κύματα", "rows": [["Κύμα", "Αριθμός"], ["=A1", 42]]}],
                    },
                )
            return LLMResponse("", [call], {})

    runtime = BotRuntime(store, owner, lambda _: Client(), limits=RuntimeLimits())
    runtime.run(store.claim(owner), EventCancellationToken(threading.Event()))
    receipt = Store(tmp_path / "assignments.db").read_assignment(
        owner, coordinator["id"], assignment["id"]
    )
    assert receipt["status"] == "completed", store.task(owner, queued["child_task_id"])["error"]
    assert receipt["child_task_id"] == queued["child_task_id"]
    if scope == "web_research":
        assert fetched == [(source, 6000)]
        assert source in receipt["result"]
        assert "artifact_id" not in receipt
        assert "workbook_contents" not in checked[0]
    else:
        # The check sees what the workbook holds, not only its name.
        assert checked[0]["worker_result"] == checked[0]["workbook_file"]
        # The run code the service adds to the file name is not held against the result.
        assert checked[0]["workbook_file"].startswith("Εγγραφές (")
        assert "code in parentheses to its file name" in prompts[0]
        assert "Κύμα\tΑριθμός" in checked[0]["workbook_contents"]
        assert "=A1\t42" in checked[0]["workbook_contents"]
        assert checked[0]["formula_count"] == 0
        asset = store.file_data(owner, recipient["id"], receipt["artifact_id"])
        workbook = openpyxl.load_workbook(BytesIO(asset))
        assert workbook["Κύματα"]["B2"].value == 42
        assert workbook["Κύματα"]["A2"].value == "=A1"
        assert workbook["Κύματα"]["A2"].data_type == "s"


@pytest.mark.parametrize("pages_read", [True, False])
def test_research_still_looking_when_its_actions_run_out_answers_from_what_it_read(
    tmp_path, monkeypatch, pages_read
):
    owner = "person"
    store = Store(tmp_path / "research.db")
    coordinator = store.set_assistant(owner, True)["bot"]
    recipient = store.save_bot(owner, BotInput(name="Analyst"))
    chat = store.create_chat(owner, coordinator["id"], "Prices")
    parent = store.enqueue(owner, chat["id"], TaskInput(message="Prices", request_id=uuid4().hex))
    assert store.claim(owner)["id"] == parent["id"]
    assignment = store.propose_assignment(
        owner,
        parent["id"],
        AssignmentInput(
            recipient_bot_id=recipient["id"],
            brief="Find today's average fuel prices.",
            expected_result="Two prices with their sources.",
            work_scope="web_research",
        ),
    )
    store.finish(owner, parent["id"], "completed", content="Approve the handoff to Analyst.")
    store.decide_assignment(owner, coordinator["id"], assignment["id"], "approve", "web_research")
    source = "https://example.com/prices"
    monkeypatch.setattr("alysis_code.bots.runtime.web_tools_available", lambda: True)
    monkeypatch.setattr(
        "alysis_code.bots.runtime.web_fetch",
        lambda url, max_chars: {
            "url": source,
            "title": "Prices",
            "text": "Unleaded 95: 1.85" if pages_read else "",
            "status_code": 200 if pages_read else 403,
        },
    )
    offered = []

    class Client:
        model = "tool-fixture"
        supports_tool_calling = True

        def chat(self, *, messages, tools, **kwargs):
            if "worker_result" in messages[-1]["content"]:
                return LLMResponse('{"decision":"ready","reason":"Checked prices."}', [], {})
            offered.append(tools is not None)
            if tools is None:
                assert "Answer now from the pages you read" in messages[-1]["content"]
                return LLMResponse("Unleaded 95: 1.85 [1]. Diesel: not found.", [], {})
            # Keeps looking for as long as it may.
            call = ToolCall(f"fetch{len(offered)}", "web_fetch", {"url": source})
            return LLMResponse("", [call], {})

    runtime = BotRuntime(store, owner, lambda _: Client(), limits=RuntimeLimits())
    runtime.run(store.claim(owner), EventCancellationToken(threading.Event()))
    receipt = store.read_assignment(owner, coordinator["id"], assignment["id"])
    if pages_read:
        # The last round answers without tools, and the source it read is attached.
        assert offered == [True] * 7 + [False]
        assert receipt["status"] == "completed"
        assert receipt["result"].startswith("Unleaded 95: 1.85 [1]. Diesel: not found.")
        assert source in receipt["result"]
    else:
        # Nothing was read: nothing can be verified, so the task still fails.
        assert offered == [True] * 8
        assert receipt["status"] == "failed"
        assert receipt["result"] is None


@pytest.mark.parametrize(
    "first_save,review", [(1, "ready"), (1, "repair"), (12, "ready"), (12, "repair")]
)
def test_workbook_at_authoring_limit_only_delivers_the_independently_reviewed_revision(
    tmp_path, monkeypatch, first_save, review
):
    import hashlib

    monkeypatch.setenv("ALYSIS_OFFICE_SOFFICE", str(tmp_path / "missing"))
    store = Store(tmp_path / "ceiling.db")
    lead = store.save_bot("person", BotInput(name="Lead"))
    worker = store.save_bot("person", BotInput(name="Analyst"))
    store.set_coordinator("person", lead["id"], True)
    source = store.save_bytes("person", lead["id"], "counts.csv", b"SKU,Count\nA,3\n")
    chat = store.create_chat("person", lead["id"], "Stock")
    parent = store.enqueue(
        "person",
        chat["id"],
        TaskInput(
            message="Report the counts.", request_id="report-counts", file_ids=[source["id"]]
        ),
    )
    store.claim("person")
    proposal = store.propose_assignment(
        "person",
        parent["id"],
        AssignmentInput(
            recipient_bot_id=worker["id"],
            brief="Report the counts.",
            expected_result="A has three units.",
            work_scope="workbook",
            input_file_ids=[source["id"]],
        ),
    )
    store.finish("person", parent["id"], "completed", content="Please approve the draft.")
    store.decide_assignment(
        "person", lead["id"], proposal["id"], "approve", "workbook", input_file_ids=[source["id"]]
    )
    child = store.claim("person")
    offered = []
    reviewed = []

    def current():
        return next(
            f
            for f in store.list_files("person", worker["id"], latest_only=True)
            if f["name"] == "Stock.xlsx"
        )

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, *, messages, tools, **kwargs):
            request = json.loads(messages[1]["content"])
            if "worker_result" in request:
                assert not tools and "A\t3" in request["workbook_contents"]
                file = current()
                raw = store.file_data("person", worker["id"], file["id"])
                assert (
                    request["workbook_sha256"] == file["sha256"] == hashlib.sha256(raw).hexdigest()
                )
                assert file["revision"] == (2 if first_save == 1 else 1)
                assert [f["id"] for f in store.list_files("person", lead["id"])] == [source["id"]]
                reviewed.append(file)
                return LLMResponse(
                    json.dumps({"decision": review, "reason": "Checked the count."}), [], {}
                )
            offered.append(bool(tools))
            number = len(offered)
            if number == first_save:
                sheet = {
                    "name": "Counts",
                    "rows": [["SKU", "Count"], ["A", 2 if number == 1 else 3]],
                }
                action = ToolCall(
                    "create",
                    "create_document",
                    {"format": "xlsx", "name": "Stock.xlsx", "spec": {"sheets": [sheet]}},
                )
            elif first_save == 1 and number in (11, 12):
                if number == 12:
                    assert not tools
                    if review == "ready":
                        return LLMResponse("A has three units.", [], {})
                    # Some providers emit tool calls with no tools offered. This request must not run.
                file = current()
                change = {"sheet": "Counts", "cell": "B2", "value": 3 if number == 11 else 999}
                spec = {"expected_source_sha256": file["sha256"], "changes": [change]}
                action = ToolCall(
                    "edit",
                    "edit_document",
                    {"file_id": file["id"], "spec": spec},
                )
            else:
                action = ToolCall("read", "read_file", {"file_id": request["input_files"][0]["id"]})
            return LLMResponse("", [action], {})

    runtime = BotRuntime(
        store, "person", lambda _: Client(), limits=RuntimeLimits(calls_per_task=13)
    )
    runtime.run(child, EventCancellationToken(threading.Event()))
    result = store.read_assignment("person", lead["id"], proposal["id"])
    assert offered == [True] * 11 + [first_save == 12]
    assert store.task("person", child["id"])["calls"] == 13
    assert len(reviewed) == 1
    if review == "ready":
        (returned,) = result["returned_files"]
        assert (
            returned["source_file_id"] == reviewed[0]["id"]
            and returned["sha256"] == reviewed[0]["sha256"]
        )
        assert store.claim("person")["chat_id"] == chat["id"]
    else:
        assert result["verification"]["status"] == "needs_review" and not result.get(
            "returned_files"
        )
        assert store.claim("person") is None


@pytest.mark.parametrize("case", ["ready", "repaired", "still_wrong", "unavailable"])
def test_shared_csv_to_checked_workbook_and_one_coordinator_reply(tmp_path, monkeypatch, case):
    from io import BytesIO

    from openpyxl import load_workbook

    monkeypatch.setenv("ALYSIS_OFFICE_SOFFICE", str(tmp_path / "missing"))
    owner = "person"
    store = Store(tmp_path / "file-handoff.db")
    coordinator = store.save_bot(owner, BotInput(name="Lead"))
    specialist = store.save_bot(owner, BotInput(name="Inventory"))
    store.set_coordinator(owner, coordinator["id"], True)
    source = store.save_bytes(
        owner, coordinator["id"], "inventory.csv", b"SKU,expected,actual\nN-27,10,7\n"
    )
    private = store.save_bytes(
        owner, specialist["id"], "unrelated.csv", b"private unrelated marker"
    )
    chat = store.create_chat(owner, coordinator["id"], "Review")
    parent = store.enqueue(
        owner,
        chat["id"],
        TaskInput(
            message="Check the attached inventory.", request_id=uuid4().hex, file_ids=[source["id"]]
        ),
    )
    assert store.claim(owner)["id"] == parent["id"]
    proposed = store.propose_assignment(
        owner,
        parent["id"],
        AssignmentInput(
            recipient_bot_id=specialist["id"],
            brief="Return an editable discrepancy sheet.",
            expected_result="Show stock overages or shortages.",
            work_scope="workbook",
            input_file_ids=[source["id"]],
        ),
    )
    store.finish(owner, parent["id"], "completed", content="Please review the handoff.")
    approved = store.decide_assignment(
        owner,
        coordinator["id"],
        proposed["id"],
        "approve",
        "workbook",
        input_file_ids=[source["id"]],
    )
    child = store.claim(owner)
    assert child["id"] == approved["child_task_id"]
    actual_calls = []
    revisions = []
    reviewed = []

    def edit(file, value):
        return ToolCall(
            "edit",
            "edit_document",
            {
                "file_id": file["id"],
                "spec": {
                    "expected_source_sha256": file["sha256"],
                    "changes": [{"sheet": "Shortages", "cell": "B2", "value": value}],
                },
            },
        )

    class Client:
        model = "test"
        supports_tool_calling = True

        def chat(self, *, messages, tools, **kwargs):
            system, user = messages[0]["content"], json.loads(messages[1]["content"])
            assert "private unrelated marker" not in str(messages)
            if "worker_result" in user:
                assert not tools and "N-27" in user["workbook_contents"]
                assert "N-27" in str(user["input_samples"])
                assert "N-27\t8" not in user["workbook_contents"]
                reviewed.append(user["workbook_sha256"])
                if case == "unavailable":
                    return LLMResponse("not a verdict", [], {})
                correct = "N-27\t3" in user["workbook_contents"]
                actual_calls.append("review")
                return LLMResponse(
                    json.dumps(
                        {
                            "decision": "ready" if correct else "repair",
                            "reason": "Three missing units."
                            if correct
                            else "Correct the shortage using the two input counts.",
                        }
                    ),
                    [],
                    {},
                )
            if "review_feedback" in user:
                assert "create_document" not in {tool["function"]["name"] for tool in tools}
                actual_calls.append("repair")
                return LLMResponse("", [edit(user["workbook"], 3 if case == "repaired" else 2)], {})
            if "returned_files" in user:
                assert not tools and "coordinating owl" in system
                assert user["coordinator_name"] == coordinator["name"]
                assert user["recipient_name"] == specialist["name"]
                assert set(user["returned_files"][0]) == {"name", "revision"}
                assert "sandbox:/" not in user["result"]
                assert returned["source_file_id"] not in user["result"]
                actual_calls.append("return")
                if case == "ready":
                    return LLMResponse("", [], {})  # Use the specialist's result as fallback.
                return LLMResponse(
                    f"Trois unités manquent. Voir [le relevé](sandbox:/library/{returned['source_file_id']}).",
                    [],
                    {},
                )
            available = {t["function"]["name"] for t in tools}
            assert "list_files" not in available and "local_command" not in available
            assert {"read_file", "create_document", "check_document"} <= available
            assert "unrelated.csv" not in str(messages)
            if not actual_calls:
                approved_file = user["input_files"][0]
                assert (
                    approved_file["id"] != source["id"] and approved_file["name"] == source["name"]
                )
                call = ToolCall("approved", "read_file", {"file_id": approved_file["id"]})
            elif actual_calls[-1] == "approved":
                assert "N-27,10,7" in messages[-1]["content"]
                call = ToolCall("denied", "read_file", {"file_id": private["id"]})
            elif actual_calls[-1] == "denied":
                assert "approved input" in messages[-1]["content"]
                call = ToolCall(
                    "create",
                    "create_document",
                    {
                        "format": "xlsx",
                        "name": "Discrepancies.xlsx",
                        "spec": {
                            "sheets": [
                                {
                                    "name": "Shortages",
                                    "header_row": True,
                                    "column_widths": [22, 18],
                                    "rows": [["SKU", "Shortage"], ["N-27", 8]],
                                }
                            ]
                        },
                    },
                )
            elif actual_calls[-1] == "create":
                draft = json.loads(messages[-1]["content"])
                assert draft.get("saved"), draft
                revisions.append(draft)
                call = ToolCall("check", "check_document", {"file_id": draft["id"]})
            elif actual_calls[-1] == "check":
                call = edit(revisions[0], 3 if case == "ready" else 17)
            else:
                receipt = json.loads(messages[-1]["content"])
                assert receipt["saved"] and receipt["checks"]
                revisions.append(receipt)
                return LLMResponse(
                    f"Trois unités manquent. Voir [le relevé](sandbox:/library/{receipt['id']}).",
                    [],
                    {},
                )
            actual_calls.append(call.id)
            return LLMResponse("", [call], {})

    runtime = BotRuntime(store, owner, lambda _: Client())
    token = EventCancellationToken(threading.Event())
    runtime.run(child, token)
    result = store.read_assignment(owner, coordinator["id"], proposed["id"])
    assert result["status"] == "completed"
    assert reviewed[0] == revisions[1]["sha256"] != revisions[0]["sha256"]
    assert len(reviewed) == (2 if case in {"repaired", "still_wrong"} else 1)
    assert actual_calls.count("repair") == (case in {"repaired", "still_wrong"})
    if case in {"still_wrong", "unavailable"}:
        assert result["verification"]["status"] == (
            "needs_review" if case == "still_wrong" else "unavailable"
        )
        assert not result.get("returned_files")
        assert store.claim(owner) is None
        return
    returned = result["returned_files"][0]
    raw = store.file_data(owner, coordinator["id"], returned["id"])
    assert returned["source_revision"] == (2 if case == "ready" else 3)
    assert returned["sha256"] == reviewed[-1]
    assert returned["id"] != returned["source_file_id"] and returned["name"] == "Discrepancies.xlsx"
    assert load_workbook(BytesIO(raw)).active["B2"].value == 3
    delivery = store.claim(owner)
    assert delivery["chat_id"] == parent["chat_id"]
    runtime.run(delivery, token)
    assert actual_calls[-1] == "return" and store.claim(owner) is None
    reply = store.task(owner, delivery["id"])["output"]
    assert "Trois unités manquent. Voir le relevé." in reply
    assert returned["name"] in reply
    assert "sandbox:/" not in reply and returned["source_file_id"] not in reply


@pytest.mark.parametrize("successor_saved", [False, True])
def test_interrupted_workbook_repair_resumes_same_revision_without_duplicate_delivery(
    tmp_path, monkeypatch, successor_saved
):
    from io import BytesIO

    from openpyxl import Workbook

    def workbook(shortage):
        book = Workbook()
        book.security = None
        book.active.title = "Count"
        book.active.append(["Item", "Missing"])
        book.active.append(["Q", shortage])
        buf = BytesIO()
        book.save(buf)
        return buf.getvalue()

    monkeypatch.setenv("ALYSIS_OFFICE_SOFFICE", str(tmp_path / "missing"))
    owner = "person"
    store = Store(tmp_path / "retry.db")
    lead = store.save_bot(owner, BotInput(name="Lead"))
    worker = store.save_bot(owner, BotInput(name="Worker"))
    store.set_coordinator(owner, lead["id"], True)
    chat = store.create_chat(owner, lead["id"], "Count")
    parent = store.enqueue(
        owner, chat["id"], TaskInput(message="Report Q shortage of three.", request_id=uuid4().hex)
    )
    assert store.claim(owner)["id"] == parent["id"]
    proposal = store.propose_assignment(
        owner,
        parent["id"],
        AssignmentInput(
            recipient_bot_id=worker["id"],
            brief="Write the missing count for Q.",
            expected_result="Q has three missing.",
            work_scope="workbook",
        ),
    )
    store.finish(owner, parent["id"], "completed", content="Submitted for approval.")
    store.decide_assignment(owner, lead["id"], proposal["id"], "approve", "workbook")
    child = store.claim(owner)
    draft = store.save_bytes(
        owner, worker["id"], "Review.xlsx", workbook(8), source_task_id=child["id"]
    )
    store.checkpoint_assignment_result(
        owner, child["id"], "Review.xlsx", output_file_ids=[draft["id"]]
    )
    assert store.begin_assignment_repair(
        owner, child["id"], draft["id"], "Correct the shortage for Q to three."
    )
    if successor_saved:
        # Crash after Office published the successor but before the runtime checkpointed it.
        store.save_bytes(
            owner,
            worker["id"],
            "Review.xlsx",
            workbook(3),
            source_task_id=child["id"],
            previous_file_id=draft["id"],
        )
    fresh = Store(store.path)
    assert fresh.recover(owner) == 1
    retry = fresh.claim(owner)
    assert retry["id"] == child["id"]
    authors = []

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, *, messages, tools, **kwargs):
            data = json.loads(messages[1]["content"])
            if "review_feedback" in data:
                assert not successor_saved and data["workbook"]["id"] == draft["id"]
                if messages[-1]["role"] == "tool":
                    authors.append(messages[-1]["content"])
                    return LLMResponse("No revised workbook could be saved.", [], {})
                authors.append(data["workbook"]["id"])
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "fix",
                            "edit_document",
                            {
                                "file_id": draft["id"],
                                "spec": {
                                    "expected_source_sha256": draft["sha256"],
                                    "changes": [{"sheet": "Count", "cell": "B2", "value": 3}],
                                },
                            },
                        )
                    ],
                    {},
                )
            assert "Q\t3" in data["workbook_contents"] and "Q\t8" not in data["workbook_contents"]
            return LLMResponse('{"decision":"ready","reason":"Q is three."}', [], {})

    runtime = BotRuntime(fresh, owner, lambda _: Client())
    runtime.run(retry, EventCancellationToken(threading.Event()))
    outcome = fresh.read_assignment(owner, lead["id"], proposal["id"])
    assert outcome.get("returned_files"), (
        outcome["status"],
        outcome["verification"],
        fresh.task(owner, child["id"])["error"],
        authors,
    )
    delivered = outcome["returned_files"]
    assert len(delivered) == 1 and delivered[0]["source_revision"] == 2
    assert len(authors) == (not successor_saved)
    assert len(fresh.list_files(owner, worker["id"])) == 2
    assert fresh.claim(owner)["config"]["assignment_return"]["assignment_id"] == proposal["id"]
    assert fresh.claim(owner) is None


def test_workbook_text_shows_rows_until_its_limit():
    from alysis_code.bots.assignment_work import CreateWorkbook, workbook_text

    data = CreateWorkbook(
        name="Τιμές",
        sheets=[
            {"name": "Καύσιμα", "rows": [["Καύσιμο", "Τιμή"], ["Αμόλυβδη 95", 1.95], [None, True]]},
            {"name": "Πηγές", "rows": [["energeiakos.gr"]]},
        ],
    )
    assert workbook_text(data).splitlines() == [
        'Sheet "Καύσιμα":',
        '["Καύσιμο", "Τιμή"]',
        '["Αμόλυβδη 95", 1.95]',
        "[null, true]",
        'Sheet "Πηγές":',
        '["energeiakos.gr"]',
    ]
    assert workbook_text(data, max_chars=40).splitlines() == [
        'Sheet "Καύσιμα":',
        '["Καύσιμο", "Τιμή"]',
        "(3 more rows not shown)",
    ]


def test_a_computer_task_runs_as_the_owls_turn_with_only_what_its_card_lists(tmp_path):
    from alysis_code.bots.models import AssignmentAccess

    store = Store(tmp_path / "computer-task.db")
    owner = "person"
    store.reply_review_settings(owner, ReplyReviewSettings(enabled=False))
    coordinator = store.save_bot(owner, BotInput(name="Coordinator"))
    recipient = store.save_bot(owner, BotInput(name="Παραγγελίες"))
    store.set_coordinator(owner, coordinator["id"], True)
    chat = store.create_chat(owner, coordinator["id"], "Orders")
    parent = store.enqueue(
        owner, chat["id"], TaskInput(message="Πέρασε τις παραγγελίες.", request_id=uuid4().hex)
    )
    assert store.claim(owner)["id"] == parent["id"]
    access = AssignmentAccess(apps=["alysis.gmail"])
    proposed = store.propose_assignment(
        owner,
        parent["id"],
        AssignmentInput(
            recipient_bot_id=recipient["id"],
            brief="Πέρασε τις σημερινές παραγγελίες στο φύλλο.",
            expected_result="Ένα ενημερωμένο φύλλο.",
            steps=["Άνοιξε το email.", "Πέρασε τον κωδικό."],
            work_scope="computer",
            access=access,
        ),
    )
    store.finish(owner, parent["id"], "completed", content="Έτοιμο για έγκριση.")
    # The person approves what it may use with the work: the scope must match.
    from alysis_code.bots.store import Conflict

    with pytest.raises(Conflict):
        store.decide_assignment(owner, coordinator["id"], proposed["id"], "approve")
    store.decide_assignment(owner, coordinator["id"], proposed["id"], "approve", "computer")
    offered, requests = [], []
    replies = iter(
        [
            LLMResponse("", [ToolCall("s1", "report_step", {"step": 1})], {}),
            LLMResponse("Πέρασα τις παραγγελίες. Το φύλλο είναι στη βιβλιοθήκη μου.", [], {}),
            # Its report is checked against the approved brief, as any assignment's result.
            LLMResponse(
                '{"decision":"repair","reason":"Έλεγξε τον κωδικό.\\nΈλεγξε το φύλλο."}', [], {}
            ),
        ]
    )

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, *, messages, tools, **_kwargs):
            offered.append({tool["function"]["name"] for tool in tools or []})
            requests.append(messages)
            return next(replies)

    runtime = BotRuntime(store, owner, lambda _: Client(), limits=RuntimeLimits())
    task = store.claim(owner)
    assert task["config"]["assignment_access"] == access.model_dump()
    runtime.run(task, EventCancellationToken(threading.Event()))
    final = store.task(owner, task["id"])
    assert offered, [e for e in final["events"] if e["kind"] == "runtime_failure"]
    names = offered[0]
    assert "report_step" in names
    # Nothing on the person's computer, no web without the browser, no other owls' work.
    assert not names & {"local_command", "list_local_computers", "web_search", "web_fetch"}
    assert not names & {"propose_owl_assignment", "request_peer", "propose_schedule"}
    request = next(
        message["content"]
        for message in requests[0]
        if message["role"] == "user" and "Approved card" in str(message["content"])
    )
    assert "Πέρασε τις σημερινές παραγγελίες" in request and "alysis.gmail" in request
    assert "1. Άνοιξε το email." in request
    events = [event for event in store.task(owner, task["id"])["events"]]
    assert [e["data"] for e in events if e["kind"] == "assignment_step"] == [{"step": 1}]
    receipt = store.list_assignments(owner, coordinator["id"])["items"][0]
    assert receipt["status"] == "completed"
    assert receipt["work_scope"] == "computer"
    assert receipt["access"] == access.model_dump()
    assert receipt["result"].startswith("Πέρασα τις παραγγελίες")
    assert receipt["verification"] == {
        "status": "needs_review",
        "detail": "Έλεγξε τον κωδικό.\nΈλεγξε το φύλλο.",
    }
    review = requests[-1]
    assert (
        "worker_result" in review[-1]["content"] and "Πέρασε τις σημερινές" in review[-1]["content"]
    )


def test_files_on_a_computer_tasks_card_must_be_in_its_owls_library(tmp_path):
    from alysis_code.bots.models import AssignmentAccess

    store = Store(tmp_path / "computer-limits.db")
    owner = "person"
    coordinator = store.save_bot(owner, BotInput(name="Coordinator"))
    recipient = store.save_bot(owner, BotInput(name="Worker", files_enabled=False))
    store.set_coordinator(owner, coordinator["id"], True)
    chat = store.create_chat(owner, coordinator["id"], "Files")
    parent = store.enqueue(owner, chat["id"], TaskInput(message="Sort.", request_id=uuid4().hex))
    assert store.claim(owner)["id"] == parent["id"]
    # Files on the card must already be in that owl's library, which must keep files.
    from alysis_code.bots.store import Conflict

    with pytest.raises(Conflict):
        store.propose_assignment(
            owner,
            parent["id"],
            AssignmentInput(
                recipient_bot_id=recipient["id"],
                brief="Sort the files.",
                expected_result="Sorted.",
                work_scope="computer",
                access=AssignmentAccess(offline_computer=True, file_ids=["f" * 32]),
            ),
        )


def test_computer_assignment_lists_and_selects_only_its_scoped_files(tmp_path):
    from alysis_code.bots.models import AssignmentAccess
    from alysis_code.bots.store import Conflict

    owner = "person"
    store = Store(tmp_path / "scoped-computer.db")
    lead = store.save_bot(owner, BotInput(name="Lead"))
    worker = store.save_bot(owner, BotInput(name="Worker"))
    store.set_coordinator(owner, lead["id"], True)
    approved = store.save_bytes(owner, worker["id"], "approved.txt", b"approved contents")
    private = store.save_bytes(owner, worker["id"], "private.txt", b"private contents")
    chat = store.create_chat(owner, lead["id"], "Files")
    source = store.enqueue(
        owner, chat["id"], TaskInput(message="Make a report.", request_id=uuid4().hex)
    )
    assert store.claim(owner)["id"] == source["id"]
    proposal = store.propose_assignment(
        owner,
        source["id"],
        AssignmentInput(
            recipient_bot_id=worker["id"],
            brief="Write a report.",
            expected_result="Report.",
            work_scope="computer",
            access=AssignmentAccess(offline_computer=True, file_ids=[approved["id"]]),
        ),
    )
    store.finish(owner, source["id"], "completed", content="Approve the report.")
    store.decide_assignment(owner, lead["id"], proposal["id"], "approve", "computer")
    child = store.claim(owner)
    runtime = BotRuntime(store, owner, lambda _: None)
    own = store.save_bytes(owner, worker["id"], "report.txt", b"report", source_task_id=child["id"])

    def call(name, args):
        return runtime._tool(child, name, args, [name], token=runtime.task_token(child["id"]))

    entries = call("list_files", {})
    assert private["id"] not in str(entries)
    assert entries["approved_inputs"][0]["id"] == approved["id"] and own["id"] in str(entries)
    assert "approved contents" in call("read_file", {"file_id": approved["id"]})["content"]
    with pytest.raises(Conflict, match="approved input"):
        call("read_file", {"file_id": private["id"]})
    with pytest.raises(Conflict):
        call("select_assignment_outputs", {"file_ids": [approved["id"]]})
    assert (
        call("select_assignment_outputs", {"file_ids": [own["id"]]})["selected_files"][0]["id"]
        == own["id"]
    )
    assert store.assignment_context(owner, child["id"])["selected_output_file_ids"] == [own["id"]]
