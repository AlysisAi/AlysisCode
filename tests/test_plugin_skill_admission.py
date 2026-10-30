"""Selected skill raw bridge/queue/host event -> production run_turn -> model request."""

import io
import json
import os
import subprocess
import threading
import time

import httpx
import pytest

from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from tests.test_computer_start_resolved_review import text_body
from tests.test_custom_plugins import _catalog, _descriptor, _save, root  # noqa: F401
from tests.test_ide_host_actions import _request, _respond, _response_for
from tests.test_plugin_skill_projection import TEXT


@pytest.fixture
def setup(root, monkeypatch):  # noqa: F811
    import hashlib

    from alysis_code import token_budget

    text = _descriptor(
        auth="none",
        extra='\n[[components.plugin_skill]]\nname="notes"\ntitle="Notes"\ndescription="Read notes."\ntext='
        + json.dumps(TEXT, ensure_ascii=False)
        + "\n",
    )
    _save(root, text)
    selected = dict(
        plugin_id="custom.tools",
        skill_name="notes",
        descriptor_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
        admission_id="d" * 32,
    )
    calls = []

    def provider(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, content=text_body("responses", False), headers={"content-type": "application/json"}
        )

    client = OpenAIResponsesClient(
        base_url="https://fixture.invalid/v1",
        api_key="fixture",
        model="test-model",
        transport=httpx.MockTransport(provider),
    )
    monkeypatch.setenv("ALYSIS_API_KEY", "fixture")
    monkeypatch.setattr("alysis_code.agent.session._make_session_llm_client", lambda **_: client)
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    monkeypatch.setattr(stdio_bridge, "_checkpointing_supported", lambda _: False)
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(
            model="test-model",
            stream=False,
            routing_mode="code_only",
            web_search_mode="off",
            skills_auto_invoke=False,
        ),
    )
    original = subprocess.run

    def inert_git(command, *a, **kw):
        if isinstance(command, (list, tuple)) and os.path.basename(str(command[0])) == "git":
            return subprocess.CompletedProcess(
                command,
                128,
                "" if kw.get("text") else b"",
                "not a git repository" if kw.get("text") else b"not a git repository",
            )
        return original(command, *a, **kw)

    monkeypatch.setattr(subprocess, "run", inert_git)
    bridge = stdio_bridge.StdioBridge(stdout=io.StringIO(), host_action_timeout_seconds=0.5)
    yield bridge, root, _catalog(text), selected, calls
    bridge.close()


def create(data, *, actions=None, approval=1):
    bridge, root, catalog, _, _ = data  # noqa: F811
    bridge.process_line(
        _request(
            "session.create",
            dict(
                workspace=str(root),
                workspace_trusted=True,
                model="test-model",
                no_log=True,
                connector_mcp=[catalog],
                host_capabilities=dict(
                    protocol_version="1",
                    approval_protocol=approval,
                    actions=actions
                    if actions is not None
                    else ["mcp.connector.call", "plugin.skill.admit"],
                ),
            ),
            "create",
        )
    )
    reply = _response_for(bridge.stdout, "create")
    assert reply["ok"], reply
    return reply["result"]["session_id"]


def wait_for(bridge, kind):
    end = time.monotonic() + 4
    while time.monotonic() < end:
        for line in bridge.stdout.getvalue().splitlines():
            e = json.loads(line)
            if e.get("type") == kind:
                return e
        time.sleep(0.005)
    raise AssertionError(bridge.stdout.getvalue()[-3000:])


@pytest.mark.parametrize("method", ["chat.send", "run.start"])
def test_selected_carriage_reaches_real_model_only_after_admission(setup, method):
    bridge, _, _, selected, calls = setup
    sid = create(setup)
    bridge.process_line(
        _request(
            method,
            dict(
                session_id=sid, message="Summarize synthetic notes.", explicit_plugin_skill=selected
            ),
            "send",
        )
    )
    ack = _response_for(bridge.stdout, "send")
    assert ack["ok"], ack
    event = wait_for(bridge, "host_action_requested")
    assert calls == []
    p = event["payload"]
    assert p["action"] == "plugin.skill.admit" and p["arguments"] == selected
    assert p["phase"] == "execute" and p["preparation_id"] is None
    _respond(bridge, event, request_id="admit", result={"admit": True})
    bridge._jobs[ack["result"]["job_id"]].thread.join(4)
    assert calls
    assert finish(bridge, ack).status == "completed"
    assert finish(bridge, ack).exit_code == 0
    wire = json.dumps(calls)
    assert selected["admission_id"] not in wire and "plugin.skill.admit" not in wire
    assert "Read the notes." in wire
    assert "prompt_for_input" not in bridge.stdout.getvalue()


def send(data, sid, **extras):
    bridge, _, _, selected, _ = data
    bridge.process_line(
        _request(
            "chat.send",
            dict(
                session_id=sid,
                message="Synthetic selected turn.",
                explicit_plugin_skill=selected,
                **extras,
            ),
            "send",
        )
    )
    return _response_for(bridge.stdout, "send")


def finish(bridge, ack):
    job = bridge._jobs[ack["result"]["job_id"]]
    if job.thread:
        job.thread.join(4)
    assert job.status not in {"running", "queued"}
    return job


@pytest.mark.parametrize(
    "reply,reason",
    [
        ({"admit": False, "reason": "stale"}, "stale"),
        ({"admit": False, "reason": "not_bound"}, "not_bound"),
        ({"admit": True, "extra": 1}, "unavailable"),
        ({"admit": "yes"}, "unavailable"),
        ({"admit": False, "reason": "invented"}, "unavailable"),
    ],
)
def test_refusal_or_malformed_result_has_zero_model_calls(setup, reply, reason):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    ack = send(setup, sid)
    event = wait_for(bridge, "host_action_requested")
    _respond(bridge, event, request_id="admit", result=reply)
    job = finish(bridge, ack)
    assert calls == [] and job.status == "failed"
    assert job.result["error_code"] == "plugin_skill_unavailable"
    assert job.result["error_details"] == {"reason": reason}
    assert job.result["verified_success"] is False
    assert bridge._p2_readiness == {}


@pytest.mark.parametrize(
    "approval,actions",
    [(0, ["mcp.connector.call", "plugin.skill.admit"]), (1, ["mcp.connector.call"])],
)
def test_session_negotiation_required_before_queue(setup, approval, actions):
    bridge, _, _, _, calls = setup
    sid = create(setup, approval=approval, actions=actions)
    ack = send(setup, sid)
    assert ack["error"]["details"]["reason"] == "unsupported"
    assert calls == [] and bridge._jobs == {}


@pytest.mark.parametrize("mutation", ["feature", "events", "methods", "authority", "fingerprint"])
def test_contradictory_negotiation_never_enqueues(setup, monkeypatch, mutation):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    cap = bridge._runtime_capabilities_payload()
    if mutation == "feature":
        cap["features"]["host_actions"]["supported"] = False
    elif mutation == "events":
        cap["events"].remove("host_action_cancelled")
    elif mutation == "methods":
        cap["methods"].remove("host.action.dispatched")
    elif mutation == "authority":
        cap["features"]["host_actions"]["approval_authority"] = "host"
    else:
        bridge._sessions[sid].host_capability_fingerprint = "f" * 64
    monkeypatch.setattr(bridge, "_runtime_capabilities_payload", lambda: cap)
    ack = send(setup, sid)
    assert ack["error"]["details"]["reason"] == "unsupported"
    assert calls == [] and bridge._jobs == {}


def test_prior_worker_drain_waits_for_exact_ack_and_reader_stays_responsive(setup, monkeypatch):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    enqueued = bridge._p2_enqueue
    worker_waiting = threading.Event()

    def early_drain(*args):
        queued = enqueued(*args)
        ready = bridge._p2_readiness[queued.item.prompt_id]
        original_wait = ready.event.wait

        def observed_wait(timeout):
            worker_waiting.set()
            return original_wait(timeout)

        ready.event.wait = observed_wait
        bridge._drain_prompt_queue(bridge._sessions[sid])
        return queued

    monkeypatch.setattr(bridge, "_p2_enqueue", early_drain)
    entered = threading.Event()
    release = threading.Event()
    write = bridge._write

    def held(payload):
        if payload.get("id") == "send" and payload.get("ok"):
            entered.set()
            assert release.wait(3)
        write(payload)

    monkeypatch.setattr(bridge, "_write", held)
    thread = threading.Thread(target=lambda: send(setup, sid))
    thread.start()
    try:
        assert entered.wait(3)
        assert worker_waiting.wait(2)
        job = bridge._sessions[sid].active_job
        assert job and job.thread and job.thread.is_alive()
        assert not bridge._p2_readiness[job.prompt_id].ready
        bridge.process_line(_request("getCapabilities", {}, "responsive"))
        assert _response_for(bridge.stdout, "responsive")["ok"]
        assert calls == []
        assert not any(
            json.loads(line).get("type") == "host_action_requested"
            for line in bridge.stdout.getvalue().splitlines()
        )
        release.set()
        thread.join(3)
        event = wait_for(bridge, "host_action_requested")
        lines = [json.loads(s) for s in bridge.stdout.getvalue().splitlines()]
        assert next(i for i, e in enumerate(lines) if e.get("id") == "send") < next(
            i for i, e in enumerate(lines) if e.get("type") == "host_action_requested"
        )
        _respond(bridge, event, request_id="admit", result={"admit": True})
        finish(bridge, _response_for(bridge.stdout, "send"))
        assert calls
    finally:
        release.set()
        thread.join(3)


@pytest.mark.parametrize("during", ["ack", "admit"])
def test_stop_during_admission_preserves_cancellation(setup, monkeypatch, during):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    if during == "ack":
        original = bridge._p2_reply_written
        monkeypatch.setattr(
            bridge,
            "_p2_reply_written",
            lambda req, result: None if req.id == "send" else original(req, result),
        )
    ack = send(setup, sid)
    if during == "admit":
        wait_for(bridge, "host_action_requested")
    bridge.process_line(_request("session.cancel", dict(session_id=sid), "stop"))
    job = finish(bridge, ack)
    assert job.status == "cancelled" and calls == []
    assert "error_code" not in job.result
    assert bridge._p2_readiness == {}


def test_held_ack_timeout_is_unavailable(setup, monkeypatch):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    original = bridge._p2_reply_written
    monkeypatch.setattr(
        bridge,
        "_p2_reply_written",
        lambda req, result: None if req.id == "send" else original(req, result),
    )
    job = finish(bridge, send(setup, sid))
    assert calls == [] and job.result["error_details"]["reason"] == "unavailable"


def test_failed_ack_never_starts_or_leaks_readiness(setup, monkeypatch):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    write = bridge._write

    def failed(payload):
        if payload.get("id") == "send" and payload.get("ok"):
            raise OSError("inert disconnected writer")
        write(payload)

    monkeypatch.setattr(bridge, "_write", failed)
    ack = send(setup, sid)
    assert ack["ok"] is False and calls == [] and bridge._p2_readiness == {}
    assert all(job.status == "failed" for job in bridge._jobs.values())
    bridge._drain_prompt_queue(bridge._sessions[sid])
    assert calls == []


def test_bytes_change_during_host_wait_refuses_exact_object(setup):
    from alysis_code.extensions.manifest import resolve_manifest_path
    from alysis_code.extensions.paths import installed_plugin_root

    bridge, root, _, _, calls = setup  # noqa: F811
    sid = create(setup)
    ack = send(setup, sid)
    event = wait_for(bridge, "host_action_requested")
    path = resolve_manifest_path(
        installed_plugin_root("custom.tools", project=False, repo_root=root)
    )
    path.write_bytes(path.read_bytes() + b"\n# drift\n")
    _respond(bridge, event, request_id="admit", result={"admit": True})
    job = finish(bridge, ack)
    assert calls == [] and job.result["error_details"]["reason"] == "pin_mismatch"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("hash", "text_changed"),
        ("pin", "pin_mismatch"),
        ("name", "unknown_skill"),
        ("extra", "invalid_request"),
        ("bool", "invalid_request"),
        ("ordinary", "invalid_request"),
        ("protected", "protected_session"),
    ],
)
def test_sync_refusals_are_closed_and_never_queue(setup, mutation, reason):
    bridge, _, _, selected, calls = setup
    sid = create(setup)
    extras = {}
    if mutation == "hash":
        selected["text_sha256"] = "a" * 64
    elif mutation == "pin":
        selected["descriptor_sha256"] = "a" * 64
    elif mutation == "name":
        selected["skill_name"] = "missing"
    elif mutation == "extra":
        selected["extra"] = "not allowed"
    elif mutation == "bool":
        selected["admission_id"] = True
    elif mutation == "ordinary":
        extras["skill"] = {"name": "computer-use"}
    elif mutation == "protected":
        bridge._sessions[sid].sensitive_media = True
    ack = send(setup, sid, **extras)
    assert ack["error"]["code"] == "plugin_skill_unavailable", ack
    assert ack["error"]["details"] == {"reason": reason}
    assert bridge._jobs == {} and calls == []


@pytest.mark.parametrize("method", ["chat.send", "run.start"])
def test_neighbor_secret_and_duplicate_id_refused_at_real_ingress(setup, method):
    bridge, _, _, selected, calls = setup
    sid = create(setup)
    value = dict(
        session_id=sid,
        message="Synthetic.",
        explicit_plugin_skill={**selected, "api_key": "sk-abcdefghijklmnopqrst"},
    )
    bridge.process_line(_request(method, value, "secret"))
    assert _response_for(bridge.stdout, "secret")["ok"] is False
    assert "sk-abcdefghijklmnopqrst" not in bridge.stdout.getvalue()
    value["explicit_plugin_skill"] = selected
    line = _request(method, value, "duplicate").replace(
        '"admission_id":', '"admission_id": "e' + ("e" * 31) + '", "admission_id":'
    )
    bridge.process_line(line)
    assert (
        _response_for(bridge.stdout, "duplicate")["error"]["details"]["reason"] == "invalid_request"
    )
    assert calls == [] and bridge._jobs == {}


def test_no_selection_on_session_create(setup):
    bridge, root, _, selected, calls = setup  # noqa: F811
    bridge.process_line(
        _request("session.create", dict(workspace=str(root), explicit_plugin_skill=selected), "bad")
    )
    assert _response_for(bridge.stdout, "bad")["error"]["details"]["reason"] == "invalid_request"
    assert calls == [] and bridge._sessions == {}


def test_duplicate_does_not_release_or_replace_held_readiness(setup, monkeypatch):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    original = bridge._p2_reply_written
    monkeypatch.setattr(
        bridge,
        "_p2_reply_written",
        lambda req, res: None if req.id == "send" else original(req, res),
    )
    ack = send(setup, sid, idempotency_key="same")
    ready = bridge._p2_readiness[ack["result"]["prompt_id"]]
    _, _, _, selected, _ = setup
    bridge.process_line(
        _request(
            "chat.send",
            dict(
                session_id=sid,
                message="Synthetic selected turn.",
                explicit_plugin_skill=selected,
                idempotency_key="same",
            ),
            "duplicate",
        )
    )
    duplicate = _response_for(bridge.stdout, "duplicate")
    assert duplicate["ok"] and duplicate["result"]["created"] is False
    assert bridge._p2_readiness[ack["result"]["prompt_id"]] is ready and not ready.ready
    assert finish(bridge, ack).result["error_details"]["reason"] == "unavailable"
    assert calls == []


def test_recovered_queue_has_no_manufactured_readiness(setup, monkeypatch):
    bridge, _, _, selected, calls = setup
    sid = create(setup)
    drain = bridge._drain_prompt_queue
    monkeypatch.setattr(bridge, "_drain_prompt_queue", lambda _: None)
    ack = send(setup, sid)
    bridge._p2_retire(session_id=sid)
    # Same durable queued item, now without the old process's private barrier.
    monkeypatch.setattr(bridge, "_drain_prompt_queue", drain)
    drain(bridge._sessions[sid])
    job = finish(bridge, ack)
    assert job.result["error_details"]["reason"] == "stale" and calls == []
    assert bridge._p2_readiness == {}


def test_queue_deletion_retires_barrier_and_hides_identifier(setup, monkeypatch):
    bridge, _, _, selected, calls = setup
    sid = create(setup)
    monkeypatch.setattr(bridge, "_drain_prompt_queue", lambda _: None)
    ack = send(setup, sid)
    pid = ack["result"]["prompt_id"]
    for method in ["chat.queue.list", "chat.queue.get"]:
        params = {"session_id": sid}
        if method.endswith("get"):
            params["prompt_id"] = pid
        bridge.process_line(_request(method, params, method))
        assert selected["admission_id"] not in json.dumps(_response_for(bridge.stdout, method))
    bridge.process_line(
        _request("chat.queue.delete", dict(session_id=sid, prompt_id=pid), "delete")
    )
    assert _response_for(bridge.stdout, "delete")["ok"]
    assert bridge._p2_readiness == {} and calls == []


@pytest.mark.parametrize("during", ["ack", "admit"])
def test_deadline_during_admission_is_not_skill_error(setup, monkeypatch, during):
    from alysis_code.execution_deadline import ExecutionDeadline

    bridge, _, _, _, calls = setup
    sid = create(setup)
    if during == "ack":
        original = bridge._p2_reply_written
        monkeypatch.setattr(
            bridge,
            "_p2_reply_written",
            lambda req, res: None if req.id == "send" else original(req, res),
        )
    ack = send(setup, sid)
    if during == "admit":
        wait_for(bridge, "host_action_requested")
    bridge._sessions[sid].agent_session.execution_deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=time.monotonic() - 2, deadline_monotonic=time.monotonic() - 1
    )
    job = finish(bridge, ack)
    assert calls == [] and job.status == "failed"
    assert job.result.get("error_code") != "plugin_skill_unavailable"
    assert job.result["task_outcome"]["reason"] == "deadline_exhausted"
    assert job.result["verified_success"] is False


def test_internal_admission_rejects_dispatch_mark(setup):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    ack = send(setup, sid)
    event = wait_for(bridge, "host_action_requested")
    p = event["payload"]
    bridge.process_line(
        _request(
            "host.action.dispatched",
            dict(
                session_id=sid,
                **{
                    k: p[k] for k in ["host_action_id", "workspace_fence", "capability_fingerprint"]
                },
            ),
            "dispatch",
        )
    )
    assert _response_for(bridge.stdout, "dispatch")["ok"] is False
    _respond(bridge, event, request_id="admit", result={"admit": False, "reason": "stale"})
    finish(bridge, ack)
    assert calls == []


def test_same_value_different_object_after_reply_is_stale(setup):
    from dataclasses import replace

    bridge, _, _, _, calls = setup
    sid = create(setup)
    ack = send(setup, sid)
    event = wait_for(bridge, "host_action_requested")
    agent = bridge._sessions[sid].agent_session
    agent.plugin_skills = tuple(replace(s) for s in agent.plugin_skills)
    _respond(bridge, event, request_id="admit", result={"admit": True})
    assert finish(bridge, ack).result["error_details"]["reason"] == "stale"
    assert calls == []


def test_google_only_admission_with_no_github_binding(setup, monkeypatch):
    import hashlib

    from alysis_code.extensions import plugin_skills
    from alysis_code.extensions.install import install_plugin
    from alysis_code.mcp import github_host
    from tests.test_google_mcp_plugin import catalogs

    bridge, workspace, _, _, calls = setup
    registry = github_host.load_registry()
    entry = github_host.find_by_id(registry, "alysis.google_drive")
    original = github_host.bundled_manifest
    content = (
        original(entry)
        + b'\n[[components.plugin_skill]]\nname="notes"\ntitle="Notes"\ndescription="Read notes."\ntext="Read the synthetic Drive notes."\n'
    )
    digest = hashlib.sha256(content).hexdigest()
    entry.package_sha256 = digest
    entry.commit = "sha256:" + digest

    def shipped(item):
        return content if item.id == "alysis.google_drive" else original(item)

    for target in [
        "alysis_code.mcp.github_host",
        "alysis_code.ide.management_protocol",
        "alysis_code.extensions.install",
    ]:
        monkeypatch.setattr(target + ".load_registry", lambda: registry)
        monkeypatch.setattr(target + ".bundled_manifest", shipped)
    monkeypatch.setattr(plugin_skills, "load_registry", lambda: registry)
    install_plugin(source="alysis.google_drive", repo_root=workspace, trust_prompt=lambda _: True)
    catalog = next(c for c in catalogs() if c["service"] == "drive")
    bridge.process_line(
        _request(
            "session.create",
            dict(
                workspace=str(workspace),
                workspace_trusted=True,
                model="test-model",
                no_log=True,
                google_mcp=[catalog],
                host_capabilities=dict(
                    protocol_version="1",
                    approval_protocol=1,
                    actions=["mcp.google.call", "plugin.skill.admit"],
                ),
            ),
            "google",
        )
    )
    created = _response_for(bridge.stdout, "google")
    assert created["ok"], created
    sid = created["result"]["session_id"]
    session = bridge._sessions[sid]
    assert session.host_mcp is None and not session.connector_mcp
    selected = dict(
        plugin_id="alysis.google_drive",
        skill_name="notes",
        descriptor_sha256=digest,
        text_sha256=hashlib.sha256(b"Read the synthetic Drive notes.").hexdigest(),
        admission_id="e" * 32,
    )
    bridge.process_line(
        _request(
            "chat.send",
            dict(
                session_id=sid,
                message="Summarize synthetic Drive notes.",
                explicit_plugin_skill=selected,
            ),
            "send",
        )
    )
    ack = _response_for(bridge.stdout, "send")
    assert ack["ok"], ack
    event = wait_for(bridge, "host_action_requested")
    assert event["payload"]["arguments"] == selected
    assert calls == []
    _respond(bridge, event, request_id="admit", result={"admit": True})
    finish(bridge, ack)
    assert calls and "Read the synthetic Drive notes." in json.dumps(calls)


def test_actual_admission_timeout_and_neighbor_secret_response(setup):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    ack = send(setup, sid)
    event = wait_for(bridge, "host_action_requested")
    _respond(
        bridge,
        event,
        request_id="bad",
        result={"admit": True, "api_key": "sk-abcdefghijklmnopqrst"},
    )
    assert _response_for(bridge.stdout, "bad")["ok"] is False
    job = finish(bridge, ack)
    assert calls == [] and job.result["error_details"]["reason"] == "unavailable"
    assert "sk-abcdefghijklmnopqrst" not in bridge.stdout.getvalue()


def test_actual_response_duplicate_is_refused_only_for_p2_result(setup):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    ack = send(setup, sid)
    event = wait_for(bridge, "host_action_requested")
    p = event["payload"]
    raw = _request(
        "host.action.respond",
        dict(
            session_id=sid,
            host_action_id=p["host_action_id"],
            workspace_fence=p["workspace_fence"],
            capability_fingerprint=p["capability_fingerprint"],
            ok=True,
            result={"admit": True},
        ),
        "duplicate",
    )
    bridge.process_line(raw.replace('"admit": true', '"admit": false, "admit": true'))
    assert (
        _response_for(bridge.stdout, "duplicate")["error"]["details"]["reason"] == "invalid_request"
    )
    assert finish(bridge, ack).result["error_details"]["reason"] == "unavailable"
    assert calls == []


def test_readiness_capacity_and_enqueue_failure_cleanup(setup, monkeypatch):
    from alysis_code.ide.plugin_skill_admission import Readiness
    from alysis_code.ide.prompt_queue import PromptQueueError

    bridge, _, _, _, calls = setup
    sid = create(setup)
    bridge._p2_readiness = {str(i): Readiness(object(), sid) for i in range(256)}
    assert send(setup, sid)["error"]["details"]["reason"] == "unavailable"
    bridge._p2_readiness.clear()

    def fail(**kwargs):
        raise PromptQueueError("Inert failure")

    monkeypatch.setattr(bridge._prompt_queue, "enqueue", fail)
    assert send(setup, sid)["ok"] is False
    assert bridge._p2_readiness == {} and calls == []


def test_disconnected_admission_channel_is_unavailable(setup, monkeypatch):
    from alysis_code.host_actions import HostActionError

    bridge, _, _, _, calls = setup
    sid = create(setup)

    def disconnected(*args, **kwargs):
        raise HostActionError("host_action_failed", "Inert disconnected channel")

    monkeypatch.setattr(bridge, "_exchange_host_action", disconnected)
    job = finish(bridge, send(setup, sid))
    assert calls == [] and job.result["error_details"]["reason"] == "unavailable"


def test_private_readiness_retires_on_session_close(setup, monkeypatch):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    monkeypatch.setattr(bridge, "_drain_prompt_queue", lambda _: None)
    send(setup, sid)
    assert bridge._p2_readiness
    bridge._close_session(bridge._sessions[sid])
    assert bridge._p2_readiness == {} and calls == []


def test_initial_run_start_forwards_selected_field_to_real_queue(setup):
    bridge, workspace, catalog, selected, calls = setup
    bridge.process_line(
        _request(
            "run.start",
            dict(
                workspace=str(workspace),
                workspace_trusted=True,
                model="test-model",
                no_log=True,
                connector_mcp=[catalog],
                host_capabilities=dict(
                    protocol_version="1",
                    approval_protocol=1,
                    actions=["mcp.connector.call", "plugin.skill.admit"],
                ),
                message="Synthetic initial selected run.",
                explicit_plugin_skill=selected,
            ),
            "initial",
        )
    )
    ack = _response_for(bridge.stdout, "initial")
    assert ack["ok"], ack
    event = wait_for(bridge, "host_action_requested")
    assert event["payload"]["arguments"] == selected and calls == []
    _respond(bridge, event, request_id="admit", result={"admit": True})
    finish(bridge, ack)
    assert calls


def test_internal_action_is_not_a_model_tool_or_general_tool_route(setup):
    from alysis_code.host_actions import HostActionError

    bridge, _, _, selected, calls = setup
    sid = create(setup)
    session = bridge._sessions[sid]
    assert not any(
        "plugin.skill.admit" in name or "admission" in name for name in session.agent_session.tools
    )
    with pytest.raises(HostActionError, match="not a tool action"):
        bridge._request_host_action(session, "plugin.skill.admit", selected)
    assert calls == []


@pytest.mark.parametrize("malformation", ["extra", "bool", "duplicate", "secret", "missing"])
def test_malformed_owned_reply_cannot_be_retried_into_admission(setup, malformation):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    ack = send(setup, sid)
    event = wait_for(bridge, "host_action_requested")
    p = event["payload"]
    params = dict(
        session_id=sid,
        host_action_id=p["host_action_id"],
        workspace_fence=p["workspace_fence"],
        capability_fingerprint=p["capability_fingerprint"],
        ok=True,
        result={"admit": True},
    )
    if malformation == "extra":
        params["result"]["extra"] = 1
    elif malformation == "bool":
        params["result"]["admit"] = "yes"
    elif malformation == "secret":
        params["result"]["api_key"] = "sk-abcdefghijklmnopqrst"
    elif malformation == "missing":
        del params["result"]
    raw = _request("host.action.respond", params, "malformed")
    if malformation == "duplicate":
        raw = raw.replace('"admit": true', '"admit": false, "admit": true')
    bridge.process_line(raw)
    assert not _response_for(bridge.stdout, "malformed")["ok"]
    _respond(bridge, event, request_id="retry", result={"admit": True})
    job = finish(bridge, ack)
    assert calls == []
    assert job.result["error_details"] == {"reason": "unavailable"}
    assert job.result["verified_success"] is False


@pytest.mark.parametrize(
    "fence", ["workspace_fence", "capability_fingerprint", "session_id", "host_action_id"]
)
def test_unowned_malformed_reply_cannot_retire_admission(setup, fence):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    ack = send(setup, sid)
    event = wait_for(bridge, "host_action_requested")
    p = event["payload"]
    params = dict(
        session_id=sid,
        host_action_id=p["host_action_id"],
        workspace_fence=p["workspace_fence"],
        capability_fingerprint=p["capability_fingerprint"],
        ok=True,
        result={"admit": "bad"},
    )
    params[fence] = "0" * 64
    bridge.process_line(_request("host.action.respond", params, "unowned"))
    assert not _response_for(bridge.stdout, "unowned")["ok"]
    _respond(bridge, event, request_id="valid", result={"admit": True})
    assert finish(bridge, ack).status == "completed"
    assert calls
