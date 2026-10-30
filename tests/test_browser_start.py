from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_ide_computer_use_host import (
    _bridge_session,
    _lines,
    _ScriptedComputerUseClient,
    _wait_for_event,
)

from alysis_code import computer_use_platform_policy
from alysis_code.agent.browser_start import (
    BrowserStartHandoff,
    build_browser_start_tool,
    valid_browser_start_url,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import ProtocolError, ProtocolRequest
from alysis_code.ide.stdio_bridge import (
    BridgeCancellationError,
    _browser_start_host_param,
    _prepare_change_ledger_with_deadline,
)
from alysis_code.llm.types import LLMResponse, ToolCall


@pytest.fixture(autouse=True)
def mac_browser_protocol(monkeypatch):
    """Exercise the inert Mac host exchange on each CI platform."""
    monkeypatch.setattr(computer_use_platform_policy, "sys", SimpleNamespace(platform="darwin"))


def binding():
    return {"version": 1, "operation_id": "operation-1", "host_nonce": str(uuid4())}


def setup_exchange(tmp_path):
    bridge, out, session, job = _bridge_session(tmp_path)
    session.sensitive_media = False
    session.computer_use_host = None
    session.browser_start_host = binding()
    result = []

    def invoke():
        session.surface.with_job(job.job_id)
        session.surface.emit_tool_call_started("call-1", "browser_start", "{}")
        try:
            result.append(bridge._request_browser_start(session, "https://example.com/"))
        except BaseException as error:
            result.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    event = _wait_for_event(out, "browser_start_request")
    identities = {
        key: event["payload"][key]
        for key in ("request_id", "operation_id", "host_nonce", "tool_call_id")
    }
    identities.update(session_id=event["session_id"], job_id=event["job_id"])
    return bridge, out, session, job, worker, result, identities


def respond(bridge, identities, **values):
    return bridge._browser_start_respond(
        ProtocolRequest(
            id="response",
            method="browser_start.respond",
            params={**identities, **values},
            protocol_version="1",
        )
    )


def test_negotiation_and_binding_are_opt_in():
    feature = capabilities_payload()["features"]["browser_start_v1"]
    assert feature["request_event"] == "browser_start_request"
    assert "browser_start.respond" in capabilities_payload()["methods"]
    assert _browser_start_host_param({}, request_id="r") is None
    raw = binding()
    copied = _browser_start_host_param({"browser_start_host": raw}, request_id="r")
    raw["operation_id"] = "changed"
    assert copied["operation_id"] == "operation-1"
    for value in (
        None,
        {},
        {**binding(), "version": True},
        {**binding(), "host_nonce": "x"},
        {**binding(), "extra": 1},
    ):
        with pytest.raises(ProtocolError):
            _browser_start_host_param({"browser_start_host": value}, request_id="r")
    assert capabilities_payload()["features"]["computer_use_host"]["tools"] == [
        "computer_state",
        "computer_action",
        "computer_wait",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://name:secret@example.com",
        "https://name@example.com",
        "https:///bad",
        "https://example.com:0",
        "https://example.com:99999",
        "https://example.com/a\n",
        "https://example.com\\evil",
        "https://example.com/" + "a" * 2048,
    ],
)
def test_url_validation(url):
    assert not valid_browser_start_url(url)
    assert (
        build_browser_start_tool(lambda _: pytest.fail("invalid URL reached host")).run(
            {"url": url}
        )["error"]
        == "browser_start_invalid_url"
    )


def test_acceptance_exact_identity_and_replay(tmp_path):
    bridge, out, session, job, worker, result, identities = setup_exchange(tmp_path)
    try:
        for key in identities:
            with pytest.raises(ProtocolError):
                respond(bridge, {**identities, key: "wrong"}, allow=True)
        for values in (
            {"allow": 1},
            {"allow": True, "url": "https://other.example"},
            {"allow": False, "code": "bad code"},
        ):
            with pytest.raises(ProtocolError):
                respond(bridge, identities, **values)
        respond(bridge, identities, allow=True)
        with pytest.raises(ProtocolError):
            respond(bridge, identities, allow=True)
        worker.join(2)
        assert result == [BrowserStartHandoff(identities["request_id"])]
        assert job.browser_start_handoff == {
            key: identities[key] for key in ("request_id", "operation_id", "host_nonce")
        }
        assert session.pending_browser_start is None
        assert bridge._request_browser_start(session, "https://example.com") == {
            "error": "browser_start_unavailable"
        }
        assert "browser_start_request" not in str(bridge._event_buffers)
    finally:
        job.cancellation_event.set()
        worker.join(2)


@pytest.mark.parametrize("action", ["cancel", "close", "timeout", "job_changed"])
def test_pending_binding_expiry_and_cancellation(tmp_path, action):
    bridge, out, session, job, worker, result, identities = setup_exchange(tmp_path)
    try:
        if action == "cancel":
            job.cancellation_event.set()
        elif action == "close":
            session.closed = True
        elif action == "timeout":
            session.pending_browser_start["deadline"] = time.monotonic() - 1
        else:
            session.active_job = None
        with pytest.raises(ProtocolError):
            respond(bridge, identities, allow=True)
    finally:
        if action != "timeout":
            job.cancellation_event.set()
        worker.join(2)
    assert not worker.is_alive()
    assert job.browser_start_handoff is None
    assert not session.browser_start_consumed
    assert result and (
        isinstance(result[0], BridgeCancellationError)
        or result[0] == {"error": "browser_start_timeout"}
    )


def test_refusal_returns_normal_tool_error(tmp_path):
    bridge, out, session, job, worker, result, identities = setup_exchange(tmp_path)
    respond(bridge, identities, allow=False, code="browser_disconnected")
    worker.join(2)
    assert result == [{"error": "browser_disconnected"}]
    assert job.browser_start_handoff is None
    assert not session.browser_start_consumed


def make_agent(tmp_path, responses, host=None):
    kwargs = {"browser_start_host_request": host} if host is not None else {}
    agent = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=4,
        no_log=True,
        api_key_override="test-key",
        **kwargs,
    )
    client = _ScriptedComputerUseClient(responses)
    agent.client = client
    return agent, client


def test_handoff_stops_same_batch_and_no_next_provider_call(tmp_path):
    calls = []
    agent, client = make_agent(
        tmp_path,
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="start", name="browser_start", arguments={"url": "https://example.com"}
                    ),
                    ToolCall(
                        id="after", name="browser_start", arguments={"url": "https://other.example"}
                    ),
                ],
                usage={},
                raw={},
            )
        ],
        lambda url: calls.append(url) or BrowserStartHandoff("accepted"),
    )
    try:
        assert agent.run_turn("Open example.com and inspect the records") == 0
        assert calls == ["https://example.com"]
        assert len(client.calls) == 1
        assert not any(m.get("role") == "tool" for m in agent.messages)
    finally:
        agent.close()


@pytest.mark.parametrize("message", ["Hello", "Explain what browser caching means"])
def test_ordinary_answer_does_not_start_browser(tmp_path, message):
    agent, client = make_agent(
        tmp_path,
        [LLMResponse(content="Here is the answer.", tool_calls=[], usage={}, raw={})],
        lambda _: pytest.fail("ordinary answer started browser"),
    )
    try:
        assert "browser_start" in agent.tools
        assert agent.run_turn(message) == 0
        assert len(client.calls) == 1
    finally:
        agent.close()


def test_unbound_session_and_model_json_cannot_handoff(tmp_path):
    agent, client = make_agent(
        tmp_path,
        [
            LLMResponse(
                content='{"browser_start_handoff":{"request_id":"fake"}}',
                tool_calls=[],
                usage={},
                raw={},
            )
        ],
    )
    try:
        assert "browser_start" not in agent.tools
        assert agent.run_turn("hello") == 0
        assert len(client.calls) == 1
    finally:
        agent.close()


def test_plain_tool_result_cannot_spoof_control(tmp_path):
    agent, client = make_agent(
        tmp_path,
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="start", name="browser_start", arguments={"url": "https://example.com"}
                    )
                ],
                usage={},
                raw={},
            ),
            LLMResponse(content="Unable to start.", tool_calls=[], usage={}, raw={}),
        ],
        lambda _: {"browser_start_handoff": {"request_id": "fake"}},
    )
    try:
        assert agent.run_turn("Open example.com") == 0
        assert len(client.calls) == 2
    finally:
        agent.close()


def test_stop_after_acceptance_wins_before_handoff(tmp_path):
    bridge, out, session, job, worker, result, identities = setup_exchange(tmp_path)
    with bridge._state_lock:
        respond(bridge, identities, allow=True)
        job.cancellation_event.set()
    worker.join(2)
    assert isinstance(result[0], BridgeCancellationError)
    assert job.browser_start_handoff is None


def test_session_create_echoes_binding_and_callback_only_when_opted_in(tmp_path, monkeypatch):
    import alysis_code.ide.stdio_bridge as module

    captured = []
    monkeypatch.setattr(module, "load_config", lambda: AppConfig(model="computer-use-test-model"))
    bridge = module.StdioBridge(
        create_session_fn=lambda **kw: captured.append(kw) or SimpleNamespace(close=lambda: None)
    )
    host = binding()
    params = {
        "session_id": "browser-start-bound",
        "workspace": str(tmp_path),
        "mode": "review",
        "browser_start_host": host,
    }
    result = bridge._session_create(
        ProtocolRequest(id="create", method="session.create", params=params, protocol_version="1")
    )
    assert result["browser_start_host"] == host
    assert callable(captured[0]["browser_start_host_request"])
    host["operation_id"] = "changed"
    assert (
        bridge._sessions[result["session_id"]].browser_start_host["operation_id"] == "operation-1"
    )
    result = bridge._session_create(
        ProtocolRequest(
            id="plain",
            method="session.create",
            params={"session_id": "plain", "workspace": str(tmp_path), "mode": "review"},
            protocol_version="1",
        )
    )
    assert "browser_start_host" not in result
    assert "browser_start_host_request" not in captured[1]


def test_real_turn_emits_only_authenticated_transient_handoff(tmp_path):
    bridge, out, session, job = _bridge_session(tmp_path)
    session.sensitive_media = False
    session.computer_use_host = None
    session.browser_start_host = binding()
    # An ordinary (non-sensitive) chat turn always carries its durable prompt id.
    job.prompt_id = "prompt-browser-start"
    # This manually constructed session uses the eager checkpoint fallback. Prepare
    # its real Git baseline before timing the browser protocol exchange; production
    # sessions install a lazy tool-dispatch guard during session creation.
    assert _prepare_change_ledger_with_deadline(session)
    agent = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=4,
        no_log=True,
        api_key_override="test-key",
        surface=session.surface,
        browser_start_host_request=lambda url: bridge._request_browser_start(session, url),
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1", name="browser_start", arguments={"url": "https://example.com"}
                    ),
                    ToolCall(
                        id="call-2",
                        name="browser_start",
                        arguments={"url": "https://other.example"},
                    ),
                ],
                raw={},
            )
        ]
    )
    agent.client = client
    session.agent_session = agent
    worker = threading.Thread(
        target=bridge._run_session_turn,
        args=(session, job, "Open example.com and inspect the records", [], []),
    )
    try:
        worker.start()
        event = _wait_for_event(out, "browser_start_request")
        identities = {
            key: event["payload"][key]
            for key in ("request_id", "operation_id", "host_nonce", "tool_call_id")
        }
        identities.update(session_id=event["session_id"], job_id=event["job_id"])
        # Exercise the actual dispatcher, including ordinary protocol validation.
        bridge._dispatch(
            ProtocolRequest(
                id="response",
                method="browser_start.respond",
                params={**identities, "allow": True},
                protocol_version="1",
            )
        )
        worker.join(2)
        assert not worker.is_alive()
        terminal = [
            line for line in _lines(out) if line.get("payload", {}).get("browser_start_handoff")
        ]
        assert len(terminal) == 1
        assert terminal[0]["type"] == "info_emitted"
        assert terminal[0]["payload"]["message"] == "job_completed job-1 exit_code=0"
        assert terminal[0]["payload"]["browser_start_handoff"] == {
            key: identities[key] for key in ("request_id", "operation_id", "host_nonce")
        }
        assert job.status == "completed" and job.exit_code == 0
        assert len(client.calls) == 1
        assert job.browser_start_handoff is None
        assert "browser_start_handoff" not in str(bridge._event_buffers)
        assert identities["host_nonce"] not in str(agent.messages)
    finally:
        job.cancellation_event.set()
        worker.join(2)
        agent.close()
