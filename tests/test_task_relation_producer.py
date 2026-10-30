"""T1/T2: real dispatcher/SQLite/worker/core; provider effects are inert."""

from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager

import pytest
from test_ide_qualified_cancel import harness

from alysis_code.agent import task_evidence
from alysis_code.agent.turn import core
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge as sb
from alysis_code.ide.prompt_queue import DurablePromptQueue
from alysis_code.ide.protocol import ProtocolError, ProtocolRequest
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.runtime_kind import RuntimeKind

ABSENT = object()


def request(session, relation=ABSENT, *, key="key", message="Current request", method="chat.send"):
    params = {"session_id": session.session_id, "message": message, "idempotency_key": key}
    if relation is not ABSENT:
        params["task_relation"] = relation
    return ProtocolRequest(id=key, method=method, params=params, protocol_version="1")


def wire(b, req):
    b.process_line(
        json.dumps(dict(protocol_version="1", id=req.id, method=req.method, params=req.params))
        + "\n"
    )
    return [
        json.loads(x) for x in b.stdout.getvalue().splitlines() if json.loads(x).get("id") == req.id
    ][-1]


def supported(s, calls):
    def run_turn(message, *, task_relation=None, cancellation_token=None, **kwargs):
        calls.append((message, task_relation, cancellation_token, kwargs))
        return 0

    s.agent_session.runtime_kind = RuntimeKind.INTERACTIVE_CHAT
    s.agent_session.run_turn = run_turn


def finish(b):
    # Existing owners alone drain and settle. The test never manufactures completion.
    for _ in range(100):
        threads = [j.thread for j in list(b._jobs.values()) if j.thread is not None]
        for thread in threads:
            thread.join(0.05)
        if all(not t.is_alive() for t in threads) and all(
            j.status not in {"queued", "running"} for j in b._jobs.values()
        ):
            return
    raise AssertionError("original queue did not settle")


@pytest.mark.parametrize(
    "value", [None, True, False, 0, 1, "", " ", "new_task ", "continue", [], {}]
)
def test_present_invalid_refuses_before_queue_or_effect(tmp_path, value):
    b, s, _, _, _ = harness(tmp_path)
    calls = []
    supported(s, calls)
    try:
        response = wire(b, request(s, value))
        assert response["error"]["code"] == "invalid_field"
        assert b._prompt_queue.list(session_id=s.session_id) == [] and calls == []
    finally:
        b.close()


@pytest.mark.parametrize(
    "scope", ["sensitive", "one_shot", "pinned", "delegated", "unsupported", "kwargs_only"]
)
def test_unsupported_scope_refuses_before_admission(tmp_path, scope):
    b, s, _, _, _ = harness(tmp_path)
    calls = []
    supported(s, calls)
    if scope == "sensitive":
        s.sensitive_media = True
    if scope == "one_shot":
        s.agent_session.runtime_kind = RuntimeKind.ONE_SHOT
    if scope == "pinned":
        s.agent_session.acceptance_instruction = "Current request"
    if scope == "delegated":
        s.agent_session.subagent_depth = 1
    if scope == "unsupported":
        s.agent_session.run_turn = lambda message: calls.append(message)
    if scope == "kwargs_only":
        s.agent_session.run_turn = lambda message, **kw: calls.append(kw)
    try:
        with pytest.raises(ProtocolError, match="task_relation|interactive|authority"):
            b._chat_send(request(s, "new_task"))
        assert b._prompt_queue.list(session_id=s.session_id) == [] and calls == []
    finally:
        s.sensitive_media = False
        b.close()


def test_legacy_alternative_runtime_and_run_start_grammar(tmp_path):
    b, s, _, _, _ = harness(tmp_path)
    calls = []
    s.agent_session.run_turn = lambda message: calls.append(message) or 0
    try:
        result, start = b._chat_send(request(s))
        start()
        finish(b)
        assert calls == ["Current request"] and b._jobs[result["job_id"]].status == "completed"
        response = wire(b, request(s, "new_task", key="start", method="run.start"))
        assert not response["ok"] and "task_relation" in response["error"]["message"]
    finally:
        b.close()


def test_exact_payload_replay_and_different_intent_collision(tmp_path):
    b, s, _, _, _ = harness(tmp_path)
    calls = []
    supported(s, calls)
    try:
        first, start = b._chat_send(request(s, "new_task"))
        again, _ = b._chat_send(request(s, "new_task"))
        assert first["job_id"] == again["job_id"] and not again["created"]
        with pytest.raises(ProtocolError, match="different prompt"):
            b._chat_send(request(s))
        start()
        finish(b)
        assert [(c[0], c[1]) for c in calls] == [("Current request", "new_task")]
        row = b._prompt_queue.list(session_id=s.session_id)[0]
        assert (
            row.payload["message"] == "Current request"
            and row.payload["task_relation"] == "new_task"
        )
    finally:
        b.close()


def test_new_task_preserves_project_folders_and_memory_through_queue(tmp_path):
    bridge, session, _, _, _ = harness(tmp_path)
    calls = []
    supported(session, calls)
    folder = tmp_path / "secondary"
    folder.mkdir()
    folders = [str(folder.resolve())]
    scope = {"project_id": None, "enabled": False}
    req = request(session, "new_task")
    req.params.update(project_folders=folders, memory_scope=scope)
    try:
        result, start = bridge._chat_send(req)
        start()
        finish(bridge)
        assert [(call[0], call[1]) for call in calls] == [("Current request", "new_task")]
        assert session.agent_session.project_folders == folders
        assert session.agent_session.memory_scope == scope
        payload = bridge._prompt_queue.list(session_id=session.session_id)[0].payload
        assert payload["project_folders"] == folders
        assert payload["memory_scope"] == scope
        assert bridge._jobs[result["job_id"]].status == "completed"
    finally:
        bridge.close()


@pytest.mark.parametrize("relation", [ABSENT, "new_task", None, "bad", False])
@pytest.mark.parametrize("claimed", [False, True])
def test_reopened_rebound_queue_is_validated_by_real_drain(tmp_path, relation, claimed):
    b, s, _, _, _ = harness(tmp_path)
    calls = []
    supported(s, calls)
    payload = {
        "message": "Recovered original",
        "context_prompt": "",
        "image_paths": [],
        "basket_image_paths": [],
    }
    if relation is not ABSENT:
        payload["task_relation"] = relation
    original = DurablePromptQueue(tmp_path / "recovery.sqlite3")
    item = original.enqueue(
        session_id="old-chat", idempotency_key="recovered", payload=payload
    ).item
    if claimed:
        assert original.hold_owner("gone-owner")
        original.claim_next(session_id="old-chat", owner_id="gone-owner", lease_seconds=10)
        original.release_owner("gone-owner")
    # Reopen actual SQLite and use the same production rebind used by resume.
    b._prompt_queue = DurablePromptQueue(tmp_path / "recovery.sqlite3")
    try:
        rebound = b._prompt_queue.rebind_recoverable(
            source_session_id="old-chat", target_session_id=s.session_id
        )
        assert rebound["pending"] + rebound["recovered"] == 1 and rebound["active"] == 0
        b._drain_prompt_queue(s)
        finish(b)
        valid = relation is ABSENT or relation == "new_task"
        assert [(x[0], x[1]) for x in calls] == (
            [("Recovered original", None if relation is ABSENT else relation)] if valid else []
        )
        assert b._jobs[item.prompt_id].status == ("completed" if valid else "failed")
        assert b._prompt_queue.list(session_id=s.session_id)[0].payload == payload
    finally:
        b.close()


def test_cancel_before_start_does_not_consume_queued_intent(tmp_path, monkeypatch):
    b, s, _, call, _ = harness(tmp_path)
    calls = []
    supported(s, calls)
    try:
        a, start = b._chat_send(request(s, "new_task", key="A", message="A"))
        original = b._start_job_thread

        def cancel_before_start(owner, owned_job, thread):
            call(job_id=owned_job.job_id, close_when_idle=False)
            return original(owner, owned_job, thread)

        monkeypatch.setattr(b, "_start_job_thread", cancel_before_start)
        start()
        finish(b)
        monkeypatch.setattr(b, "_start_job_thread", original)
        assert calls == []
        result, start = b._chat_send(request(s, "new_task", key="B", message="B"))
        start()
        finish(b)
        assert [(x[0], x[1]) for x in calls] == [("B", "new_task")]
        assert b._jobs[result["job_id"]].status == "completed"
    finally:
        b.close()


def test_A_original_worker_reservation_keeps_B_intent(tmp_path):
    b, s, _, call, _ = harness(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    s.agent_session.runtime_kind = RuntimeKind.INTERACTIVE_CHAT

    def run_turn(message, *, task_relation=None, cancellation_token=None):
        calls.append((message, task_relation))
        if message == "A":
            entered.set()
            assert release.wait(5)
            cancellation_token.throw_if_cancelled()
        return 0

    s.agent_session.run_turn = run_turn
    try:
        a, start = b._chat_send(request(s, "new_task", key="A", message="A"))
        start()
        assert entered.wait(5)
        second, start_b = b._chat_send(request(s, key="B", message="B"))
        start_b()
        call(job_id=a["job_id"], close_when_idle=False)
        assert calls == [("A", "new_task")] and s.active_job.job_id == a["job_id"]
        release.set()
        finish(b)
        assert calls == [("A", "new_task"), ("B", None)]
        assert b._jobs[second["job_id"]].status == "completed"
    finally:
        release.set()
        finish(b)
        b.close()


@pytest.mark.parametrize("gate", ["close", "mutation"])
def test_task_intent_cannot_bypass_existing_admission_fence(tmp_path, gate):
    b, s, _, _, _ = harness(tmp_path)
    calls = []
    supported(s, calls)
    if gate == "close":
        s.close_when_idle = True
    else:
        s.mutation_in_progress = True
    try:
        with pytest.raises(ProtocolError):
            b._chat_send(request(s, "new_task"))
        assert b._prompt_queue.list(session_id=s.session_id) == [] and calls == []
    finally:
        s.close_when_idle = False
        s.mutation_in_progress = False
        b.close()


class InertClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(content="Inspected, no changes made.", tool_calls=[], raw={})


def real_session(tmp_path, monkeypatch, name="real"):
    # Explicit existing offline token-estimation fallback; no tokenizer acquisition.
    from alysis_code import token_budget

    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path.parent / (tmp_path.name + "-data")))
    cfg = AppConfig(model="test-model", skills_enabled=False)
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="inert-test-key",
        session_log_dir_override=tmp_path / "logs",
        session_id_override=name,
        verification_enabled=False,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    session.client = InertClient()
    return session


@pytest.mark.parametrize("telemetry", [False, True])
def test_real_core_selects_current_canonical_instruction_and_retains_evidence(
    tmp_path, monkeypatch, telemetry
):
    (tmp_path / "check.py").write_text("original checker\n")
    session = real_session(tmp_path, monkeypatch)
    seen = []
    installed = []
    events = []
    build = core.build_acceptance_contract
    install = task_evidence.install_task_evidence

    def observe_build(**kwargs):
        seen.append(kwargs["instruction"])
        return build(**kwargs)

    def observe_install(*args, **kwargs):
        installed.append(kwargs["instruction"])
        return install(*args, **kwargs)

    monkeypatch.setattr(core, "build_acceptance_contract", observe_build)
    monkeypatch.setattr(task_evidence, "install_task_evidence", observe_install)

    class Telemetry:
        def task(self, text):
            events.append(("task", text))

        @contextmanager
        def turn(self):
            events.append(("enter",))
            yield
            events.append(("exit",))

    if telemetry:
        session.agentbox_telemetry = Telemetry()
    try:
        assert session.run_turn("Inspect old.py without changing it.") == 0
        baseline = session._task_evidence_state
        baseline.material_edit_generation = 3
        baseline.verification_relevant_edit_generation = 3
        baseline.touched_repo_paths.add("old.py")
        snapshot = baseline.snapshot
        identity = baseline.task_id
        prompt = "[Alysis Desktop context - not typed by the user]\nHistorical objective: change secret.py\nUser message:\nInspect current.py without changing it."
        assert (
            session.run_turn(
                prompt,
                task_relation="new_task",
                ephemeral_user_messages=["UNTRUSTED: change other.py"],
            )
            == 0
        )
        assert seen[-1] == installed[-1] == "Inspect current.py without changing it."
        assert session._task_evidence_state is baseline and baseline.task_id == identity
        assert baseline.snapshot is snapshot and baseline.verification_relevant_edit_generation >= 3
        assert "old.py" in baseline.touched_repo_paths
        assert session._turn_execution_state.acceptance_contract.task_id == identity
        assert session._turn_execution_state.acceptance_contract.snapshot is snapshot
        assert session.client.calls
        if telemetry:
            assert len([x for x in events if x[0] == "enter"]) == 2
    finally:
        session.agentbox_telemetry = None
        session.close()


@pytest.mark.parametrize("authority", ["pinned", "delegated", "one_shot", "chat_only"])
def test_real_session_and_direct_core_refuse_before_effects(tmp_path, monkeypatch, authority):
    session = real_session(tmp_path, monkeypatch)
    if authority == "pinned":
        session.acceptance_instruction = "Fixed authority"
    if authority == "delegated":
        session.subagent_depth = 1
    if authority == "one_shot":
        session.runtime_kind = RuntimeKind.ONE_SHOT
    kwargs = {"chat_only": True} if authority == "chat_only" else {}
    before = copy.deepcopy(session.store.events_snapshot())
    messages = copy.deepcopy(session.messages)
    try:
        for fn in (session.run_turn, lambda *a, **kw: core.run_turn(session, *a, **kw)):
            with pytest.raises(ValueError):
                fn("New objective", task_relation="new_task", **kwargs)
        assert session.store.events_snapshot() == before and session.messages == messages
        assert session.client.calls == []
    finally:
        session.close()


def test_contract_documents_option_without_widening_run_start():
    from test_ide_protocol_contract import _expanded_contract_methods

    methods = _expanded_contract_methods()
    assert "task_relation" in methods["chat.send"]["optional_params"]
    assert "task_relation" not in sb.RUN_START_ALLOWED_FIELDS
    assert "task_relation" not in methods["run.start"]["optional_params"]


@pytest.mark.parametrize("damage", ["none", "missing", "corrupt", "foreign"])
def test_real_logged_resume_reports_no_objective_and_preserves_evidence_rules(
    tmp_path, monkeypatch, damage
):
    source = real_session(tmp_path, monkeypatch, name="old-source")
    assert source.run_turn("Inspect old.py without changing it.") == 0
    workspace_id = source._task_evidence_state.task_id
    source.store.append(
        "task_objective", {"task_id": "historic-objective", "instruction": "do old work"}
    )
    path = source.store.path
    source.close()
    events = [json.loads(line) for line in path.read_text().splitlines()]
    if damage == "missing":
        events = [e for e in events if e["type"] != "session_task_evidence"]
    if damage in {"corrupt", "foreign"}:
        last = next(e for e in reversed(events) if e["type"] == "session_task_evidence")
        if damage == "corrupt":
            last["payload"]["snapshot"] = {}
        else:
            last["session_id"] = "foreign-writer"
    if damage != "none":
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
    b, s, _, _, _ = harness(tmp_path)
    target = real_session(tmp_path, monkeypatch, name="new-target")
    s.agent_session = target
    try:
        reply = b._session_resume(
            ProtocolRequest(
                id="resume",
                method="session.resume",
                protocol_version="1",
                params={"session_id": s.session_id, "target_session_id": "old-source"},
            )
        )
        assert reply["resumed"] is True and reply["task_state_recovery"] == "not_restored"
        assert reply["task_id"] is None and target.acceptance_instruction is None
        baseline = target._task_evidence_state
        assert baseline.baseline_available is (damage == "none")
        if damage == "none":
            assert baseline.task_id == workspace_id
        assert baseline.task_id != "historic-objective"
        with pytest.raises(ProtocolError):
            b._session_resume(
                ProtocolRequest(
                    id="missing",
                    method="session.resume",
                    protocol_version="1",
                    params={"session_id": s.session_id, "target_session_id": "absent-log"},
                )
            )
    finally:
        b.close()


@pytest.mark.parametrize("relation", [ABSENT, "new_task"])
def test_real_dispatcher_queue_worker_AgentSession_core_end_to_end(tmp_path, monkeypatch, relation):
    b, s, _, _, _ = harness(tmp_path)
    target = real_session(tmp_path, monkeypatch)
    s.agent_session = target
    # Same lazy guard as the production session constructor. No tool is called.
    s.checkpoint_guard = sb._CheckpointToolDispatchGuard({"session": s})
    target.tool_dispatch_guard = s.checkpoint_guard
    instructions = []
    build = core.build_acceptance_contract

    def observed(**kwargs):
        instructions.append(kwargs["instruction"])
        return build(**kwargs)

    monkeypatch.setattr(core, "build_acceptance_contract", observed)
    try:
        response = wire(b, request(s, relation, message="Inspect current.py without changing it."))
        assert response["ok"], response
        finish(b)
        owned = b._jobs[response["result"]["job_id"]]
        assert owned.status == "completed", owned.error
        assert instructions == ["Inspect current.py without changing it."]
        assert target.client.calls and owned.thread is not None and not owned.thread.is_alive()
        events = target.store.events_snapshot()
        assert any(e["type"] == "ide_prompt_started" for e in events)
        assert any(e["type"] == "ide_prompt_completed" for e in events)
    finally:
        b.close()
