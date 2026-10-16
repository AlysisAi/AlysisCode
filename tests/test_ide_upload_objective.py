from __future__ import annotations

import io
import threading
import time
from dataclasses import replace

import pytest
from test_computer_use_upload_objectives import binding, host_binding, proof, start
from test_computer_use_uploads import state, upload
from test_ide_computer_use_host import (
    _bridge_session,
    _lines,
    _request,
    _ScriptedComputerUseClient,
    _wait_for_event,
)

import alysis_code.agent.session as agent_module
import alysis_code.ide.stdio_bridge as stdio
from alysis_code.agent.computer_use_files import computer_use_file_delivery_status
from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.ide.protocol import ProtocolRequest
from alysis_code.profiles import get_active_profile


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "nonce",
        "operation",
        "session",
        "expiry",
        "cancel",
        "host-replaced",
        "job-replaced",
        "route-changed",
        "route-final-callback",
        "closed",
    ],
)
def test_actual_ide_evaluate_response_cannot_escape_original_live_session(tmp_path, mode):
    bridge, out, session, job = _bridge_session(tmp_path)
    actual_agent = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=4,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=lambda *_: {},
        computer_use_uploads_available=True,
        computer_use_verification_available=True,
        computer_use_upload_objective=binding(),
    )
    actual_agent.client = _ScriptedComputerUseClient([])
    session.agent_session = actual_agent
    session.computer_use_destination = stdio._actual_computer_use_destination(
        actual_agent, request_id="route"
    )
    session.computer_use_host = host_binding(upload_objective=binding())
    response = [upload()]

    def request(name, args):
        if name == "computer_verify":
            return bridge._request_computer_use(session, name, args)
        return state() if name == "computer_state" else response[0]

    tools = build_computer_use_tools(
        request, verification_available=True, uploads_available=True, upload_objective=binding()
    )
    start(tools, response)
    result, errors = {}, []
    if mode == "route-final-callback":
        original_get = actual_agent.model_registry.get
        reads = 0

        def change_during_final_route_check(model):
            nonlocal reads
            reads += 1
            value = original_get(model)
            if reads == 2:
                actual_agent.client.base_url = "https://other.invalid/v1"
            return value

        actual_agent.model_registry.get = change_during_final_route_check

    def run():
        session.surface.with_job(job.job_id)
        session.surface.emit_tool_call_started("verify-call", "computer_verify", "[redacted]")
        try:
            result.update(tools["computer_verify"].run({"operation": "evaluate"}))
        except Exception as exc:
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        event = _wait_for_event(out, "computer_use_request")
        p = event["payload"]
        answer = {
            key: p[key]
            for key in [
                "request_id",
                "response_nonce",
                "operation_id",
                "tool_call_id",
                "tool_name",
                "arguments_fingerprint",
                "expires_at",
            ]
        }
        answer.update(
            session_id=session.session_id, job_id=event["job_id"], ok=True, result=proof()
        )
        if mode in {"nonce", "operation", "session"}:
            key = {"nonce": "response_nonce", "operation": "operation_id", "session": "session_id"}[
                mode
            ]
            answer[key] = "wrong"
        if mode == "expiry":
            session.pending_computer_use[p["request_id"]].deadline = time.monotonic() - 1
        if mode == "cancel":
            job.cancellation_event.set()
        if mode == "host-replaced":
            session.computer_use_host = replace(session.computer_use_host)
        if mode == "job-replaced":
            session.active_job = replace(job)
        if mode == "route-changed":
            actual_agent.client.base_url = "https://other.invalid/v1"
        if mode == "closed":
            session.closed = True
        bridge.process_line(_request("computer.respond", answer, "answer") + "\n")
        if mode in {"nonce", "operation", "session"}:
            refused = next(line for line in _lines(out) if line.get("id") == "answer")
            assert not refused["ok"]
            assert not computer_use_file_delivery_status(tools)["complete"]
            job.cancellation_event.set()
            bridge._cancel_pending_computer_use(session, "cancelled_by_user")
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert computer_use_file_delivery_status(tools)["complete"] == (mode == "success")
        if mode == "success":
            assert result == proof() and not errors
            bridge.process_line(_request("computer.respond", answer, "replay") + "\n")
            assert (
                next(line for line in _lines(out) if line.get("id") == "replay")["error"]["code"]
                == "duplicate_response"
            )
        else:
            assert errors or "error" in result
    finally:
        job.cancellation_event.set()
        bridge._cancel_pending_computer_use(session, "cancelled_by_user")
        thread.join(timeout=3)
        actual_agent.close()


def test_actual_session_create_captures_original_binding_before_provider_callback_and_echoes_it(
    tmp_path, monkeypatch
):
    cfg = AppConfig(model="computer-use-test-model", routing_mode="code_only")
    client = _ScriptedComputerUseClient([])
    # A persisted credential outranks OPENAI_API_KEY in resolve_api_key, so without an
    # isolated config directory this asserts "env:OPENAI_API_KEY" against whatever the
    # developer happens to have logged in with, and reads their real credentials.json.
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENAI_API_KEY", "generated-key")
    monkeypatch.setattr(stdio, "load_config", lambda: cfg)
    destination = stdio._computer_use_destination(
        model=cfg.model,
        profile=get_active_profile(cfg).name,
        auth_source="env:OPENAI_API_KEY",
        account_id=client.route_identity.credential_scope,
        endpoint_digest=stdio._computer_use_endpoint_digest(client.base_url),
    )
    params = dict(
        workspace=str(tmp_path),
        mode="review",
        session_id="objective-create",
        sensitive_media=True,
        model=cfg.model,
        base_url=client.base_url,
        computer_use_destination=destination.to_protocol(),
        computer_use_host=host_binding(upload_objective=binding()).to_protocol(),
    )

    def make_client(**kwargs):
        params["computer_use_host"]["upload_objective"]["objective_digest"] = "sha256:" + "f" * 64
        return client

    monkeypatch.setattr(agent_module, "_make_session_llm_client", make_client)
    bridge = stdio.StdioBridge(stdout=io.StringIO())
    try:
        result = bridge._session_create(
            ProtocolRequest(id="create", method="session.create", params=params)
        )
        session = bridge._sessions[result["session_id"]]
        assert result["computer_use_host"]["upload_objective"] == binding()
        assert session.computer_use_host.to_protocol()["upload_objective"] == binding()
        held = session.agent_session.tools["computer_upload"].metadata[
            "_computer_use_upload_binding"
        ]
        assert held.objective.to_protocol() == binding()
    finally:
        for session in list(bridge._sessions.values()):
            bridge._close_session(session)
