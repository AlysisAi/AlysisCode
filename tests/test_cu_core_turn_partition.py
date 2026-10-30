"""Real turn consumers for answer-only/CU separation and resolved stream ownership."""

import copy
import json
from types import SimpleNamespace

import pytest
from test_computer_start_resolved_turn import CORPUS, body, session_for
from test_sensitive_tool_output_boundary import _make_computer_use_session

from alysis_code import computer_use_platform_policy as policy
from alysis_code.agent import session as session_module
from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent.tool_definition import ToolDef
from alysis_code.agent.turn import core
from alysis_code.llm.openai_compat import LLMResponse, ToolCall


@pytest.fixture(autouse=True)
def mac_platform(monkeypatch):
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="darwin"))


@pytest.mark.parametrize("damage", ["empty", "foreign", "replace"])
def test_original_protected_surface_cannot_downgrade(tmp_path, monkeypatch, damage):
    host_calls = []
    session, (client, *_rest) = _make_computer_use_session(
        tmp_path,
        responses=[LLMResponse(content="Must not run", tool_calls=[], raw={})],
        host_request=lambda *args: host_calls.append(args),
    )
    observed_errors = []
    original_failure = session_module._protected_computer_use_failure

    def observe_failure(exc):
        observed_errors.append((type(exc), str(exc)))
        return original_failure(exc)

    monkeypatch.setattr(session_module, "_protected_computer_use_failure", observe_failure)
    foreign_calls = []
    foreign = ToolDef(
        name="foreign",
        description="Inert",
        parameters={"type": "object", "properties": {}},
        run=lambda args: foreign_calls.append(args),
    )
    if damage in {"empty", "replace"}:
        session.tools.clear()
        session.tool_list.clear()
    if damage in {"foreign", "replace"}:
        session.tools[foreign.name] = foreign
        session.tool_list.append(foreign.as_openai_tool())
    try:
        with pytest.raises(AgentRuntimeError, match="protected provider or host request failed"):
            session.run_turn("Observe the selected app.")
        assert observed_errors == [
            (ValueError, "Sensitive session contains an invalid Computer Use tool surface.")
        ]
        assert client.calls == 0 and host_calls == [] and foreign_calls == []
    finally:
        session.close()


def test_valid_protected_cu_stays_nonstreaming_and_scrubbed(tmp_path, monkeypatch):
    host_calls = []

    def host(name, args):
        host_calls.append(name)
        return {"snapshot_id": "original-snapshot", "source": "chrome-dom", "elements": []}

    session, (client, *_rest) = _make_computer_use_session(
        tmp_path,
        host_request=host,
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(content="Read complete.", tool_calls=[], raw={}),
        ],
    )
    try:
        assert session.stream is True
        assert session.run_turn("Read the selected app.") == 0
        assert host_calls == ["computer_state"]
        assert client.stream_flags == [False, False]
        assert client.reasoning_callback_flags == [False, False]
    finally:
        session.close()


@pytest.mark.parametrize("protocol", ["compat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_resolved_initial_and_correction_keep_original_stream_without_display(
    tmp_path, monkeypatch, protocol, stream
):
    plan = copy.deepcopy(CORPUS["bases"]["plan"])
    bad = copy.deepcopy(plan)
    bad["app_steps"][0]["app"] = "Unknown"
    session, calls, requests = session_for(
        tmp_path,
        monkeypatch,
        protocol,
        stream,
        [body(protocol, stream, json.dumps(bad)), body(protocol, stream, json.dumps(plan))],
    )
    observed = []
    original = core._main_agent_chat

    def observe(**kwargs):
        observed.append(
            (kwargs["stream"], kwargs.get("on_text_delta"), kwargs.get("on_reasoning_delta"))
        )
        return original(**kwargs)

    monkeypatch.setattr(core, "_main_agent_chat", observe)
    try:
        assert session.sensitive_media is False
        assert session.run_turn("Read Alpha.") == 0
        assert len(calls) == 1 and len(requests) == 2
        assert observed == [(stream, None, None), (stream, None, None)]
        assert all(request.get("stream", False) is stream for request in requests)
        assert session.resolved_entry.retired == "consumed"
    finally:
        session.close()
