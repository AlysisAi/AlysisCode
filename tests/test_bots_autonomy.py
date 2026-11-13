"""Real runtime/Code loops with scripted providers; no live-model or cloud claims."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_bots_browser_runtime import ClientFixture, response
from test_bots_coding import handoff as handoff
from test_bots_coding import submit
from test_bots_desktop_runtime import study as study
from test_bots_runtime import ScriptedClient, setup_task
from test_bots_runtime import response as main_response

from alysis_code.bots.app import Settings
from alysis_code.bots.coding import CodingModelRequest
from alysis_code.bots.execution import CompactionClient
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import LimitReached, Store
from alysis_code.llm.types import LLMError, ToolCall
from alysis_code.request_estimation import estimate_request_tokens


def test_main_runs_past_old_call_limits_and_keeps_durable_accounting(tmp_path):
    store = Store(tmp_path / "bots.db")
    _, _, queued = setup_task(store)
    client = ScriptedClient(
        [
            *[main_response(calls=[ToolCall(str(i), "list_files", {})]) for i in range(105)],
            main_response("Inspected the library."),
        ]
    )
    runtime = BotRuntime(store, "alice", lambda _: client)
    task = store.claim("alice")
    runtime.run(task, runtime.task_token(task["id"]))
    reopened = Store(store.path)
    result = reopened.task("alice", queued["id"])
    assert result["status"] == "completed", result
    assert result["calls"] == reopened.usage("alice")["calls"] == 106
    setup_task(reopened, message="New task after reopening")
    next_task = reopened.claim("alice")
    with pytest.raises(LimitReached):
        # A newly configured operator cap still applies to existing usage.
        reopened.reserve_call("alice", next_task["id"], "fixture", 100)


@pytest.mark.parametrize("stop", [False, True])
def test_code_loop_passes_old_limits_and_remains_cancellable(study, monkeypatch, stop):
    import alysis_code.bots.execution as execution

    store, task, manager, instances = study
    original = execution.create_session
    sessions = []

    def create(**kwargs):
        session = original(**kwargs)
        sessions.append(session)
        assert session.max_steps is None
        assert session.execution_deadline.deadline_monotonic is None
        assert session.conversation_compactor.compactor_client.model == session.client.model
        return session

    monkeypatch.setattr(execution, "create_session", create)
    count = 0

    class Client:
        model = "autonomy-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            nonlocal count
            count += 1
            if count == 106:
                if stop:
                    runtime.cancel(task["id"])
                return response("Inspected the requested files.")
            return response(name="computer_run", arguments={"command": f"printf phase_{count}"})

    runtime = BotRuntime(store, "owner", lambda _: Client(), desktop_computers=manager)
    runtime.run(task, runtime.task_token(task["id"]))
    claimed = runtime.background.claim()
    runtime.run(claimed, runtime.task_token(task["id"]), background=True)
    result = store.task("owner", task["id"])
    assert result["status"] == ("cancelled" if stop else "completed"), result
    assert result["calls"] == 106
    assert len(instances[0].calls) == 105
    assert instances[0].closed
    assert len(sessions) == 1


def test_model_context_replaces_prototype_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", "256000")
    monkeypatch.setenv("ALYSIS_MAX_OUTPUT_TOKENS", "16000")
    limits = RuntimeLimits.for_model(
        model="capacity-fixture", base_url="https://example.invalid/v1"
    )
    assert limits.context_tokens == 256000 - 16000 - 512
    assert limits.history_tokens > 6000
    assert limits.context_characters is None
    store = Store(tmp_path / "bots.db")
    setup_task(store)
    task = store.claim("alice")
    runtime = BotRuntime(store, "alice", lambda _: None, limits=limits)
    messages = [{"role": "user", "content": "東京 Αθήνα Δεδομένα 12345\n" * 6000}]
    assert len(messages[0]["content"]) > 64000
    assert estimate_request_tokens(messages, []) > 24000
    wire = runtime._fit_context(task, messages.copy(), [])
    assert [m for m in wire if m["role"] == "user"] == messages
    assert all(m["role"] in {"system", "user"} for m in wire)
    assert wire[-1]["role"] == "system"


def test_coding_jobs_are_uncapped_and_survive_reopening(handoff):
    store, coding, _, workspace, worker, task = handoff
    submit(handoff)
    job = coding.claim(workspace, worker)
    assert job["context"]["execution_calls"] is None
    for _ in range(106):
        req = CodingModelRequest(
            worker_id=worker, request_id=uuid4().hex, phase="execute", messages=[]
        )
        call, saved = coding.reserve_model(job["id"], req, "fixture", None)
        assert saved is None
        store.finish_call(call, "completed", None)
        coding.finish_model(call, {"content": "receipt"})
    assert Store(store.path).usage("owner")["calls"] == 106
    assert coding.reserve_model(job["id"], req, "fixture", None) == (call, {"content": "receipt"})


def test_compaction_is_accounted_tool_free_and_never_published_as_answer(study):
    store, task, manager, _ = study
    client = ClientFixture([response('{"goal":"continue"}')])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    compact = CompactionClient(runtime, task, client, runtime.task_token(task["id"]))
    compact.chat(messages=[{"role": "user", "content": "Summarize prior steps"}])
    assert store.usage("owner")["calls"] == 1
    assert client.requests[0]["tools"] is None
    assert store.task("owner", task["id"])["output"] == ""
    runtime.limits = replace(runtime.limits, calls_per_task=2)
    with pytest.raises(LLMError):
        compact.chat(messages=[])
    assert len(client.requests) == 1


def test_settings_default_to_autonomous_and_allow_explicit_study_budgets(tmp_path):
    values = dict(
        database=tmp_path / "bots.db",
        token="x" * 40,
        api_key="fixture",
        base_url="https://example.invalid/v1",
        model="fixture",
    )
    settings = Settings(**values)
    assert settings.max_calls is settings.coding_calls is settings.output_tokens is None
    bounded = Settings(**values, max_calls=0, coding_calls=150, output_tokens=16000)
    assert bounded.max_calls == 0 and bounded.coding_calls == 150


def test_long_code_context_compacts_and_original_receipts_remain_readable(study, monkeypatch):
    from test_bots_desktop import Executor

    import alysis_code.bots.execution as execution

    monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", "32000")
    monkeypatch.setenv("ALYSIS_MAX_OUTPUT_TOKENS", "4000")
    monkeypatch.setenv("ALYSIS_COMPACTION_TRIGGER_RATIO", "0.55")
    monkeypatch.setenv("ALYSIS_COMPACTION_TARGET_RATIO", "0.30")
    store, task, manager, instances = study
    original_call = Executor.call
    original_create = execution.create_session
    sessions = []
    commands = summaries = 0
    history_read = False

    def command(*args, **kwargs):
        result = original_call(*args, **kwargs)
        if args[2] == "run":
            result["output"] = f"Receipt {len(args[0].calls)} 東京\n" + (
                "sample observations " * 650
            )
        return result

    def create(**kwargs):
        session = original_create(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(Executor, "call", command)
    monkeypatch.setattr(execution, "create_session", create)

    class Client:
        model = "context-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            nonlocal commands, summaries, history_read
            if kwargs.get("tools") is None:
                summaries += 1
                return response(
                    json.dumps(
                        {
                            "goal": "Inspect the requested file",
                            "constraints": [],
                            "decisions": [],
                            "work_done": ["Earlier commands returned receipts."],
                            "open_threads": [],
                            "next_steps": ["Continue inspecting"],
                        }
                    )
                )
            if commands < 22:
                commands += 1
                return response(name="computer_run", arguments={"command": f"inspect_{commands}"})
            # Read actual archived data through the exact same confined tools the
            # model is offered, without filesystem or shell access to the host.
            session = sessions[0]
            found = session.tools["history_search"].run({"pattern": "Receipt 1 "})
            assert found["matches"], found
            receipt = session.tools["session_artifact_read"].run(
                {"locator": found["matches"][0]["path"]}
            )
            assert "Receipt 1 東京" in receipt["content"]
            from alysis_code.tools.artifacts import SessionArtifactReadError

            with pytest.raises(SessionArtifactReadError):
                session.tools["session_artifact_read"].run({"locator": "/etc/passwd"})
            history_read = True
            return response("Inspection complete.")

    runtime = BotRuntime(
        store,
        "owner",
        lambda _: Client(),
        desktop_computers=manager,
        limits=RuntimeLimits.for_model(
            model="context-fixture", base_url="https://example.invalid/v1"
        ),
    )
    runtime.run(task, runtime.task_token(task["id"]))
    claimed = runtime.background.claim()
    runtime.run(claimed, runtime.task_token(task["id"]), background=True)
    result = store.task("owner", task["id"])
    assert result["status"] == "completed", result
    assert summaries > 0 and history_read
    assert result["calls"] == commands + summaries + 1
    assert len(instances[0].calls) == commands  # Archived work was never replayed.
    assert "goal" not in result["output"]  # Internal summaries stay internal.


def test_coding_handoff_has_no_fixed_character_or_message_count_ceiling(handoff):
    store, coding, _, workspace, worker, task = handoff
    conversation = [{"role": "user", "content": "東京 Αθήνα " * 20} for _ in range(1001)]
    job = coding.submit(task, "Inspect the sources", conversation)
    restored = coding.claim(workspace, worker)
    assert restored["context"]["conversation"] == conversation
    request = CodingModelRequest(
        worker_id=worker,
        request_id=uuid4().hex,
        phase="execute",
        messages=conversation,
    )
    # Actual provider admission is separately model-token-budgeted at the API.
    assert len(request.messages) == 1001
    assert restored["id"] == job["id"]
