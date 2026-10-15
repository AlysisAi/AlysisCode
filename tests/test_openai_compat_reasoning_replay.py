from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code.llm import openai_compat as compat
from alysis_code.llm.metadata import (
    OPENAI_COMPAT_REASONING_METADATA_KEY,
    PROVIDER_METADATA_KEY,
    assistant_message_from_response,
    build_provider_route_identity,
    credential_scope_fingerprint,
    stamp_provider_metadata_for_route,
)
from alysis_code.llm.protocols import resolve_reasoning_trace_capability
from alysis_code.llm.types import ReasoningOutputKind
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)
from alysis_code.reasoning_contracts import ReasoningContract, reasoning_contract_for

_STATE = "  Synthetic fixture state Ω\n" * 3000
_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
_MODEL = "glm-5.3-flash"


@pytest.fixture(autouse=True)
def isolated_telemetry() -> Any:
    reset_provider_telemetry_for_tests()
    yield
    reset_provider_telemetry_for_tests()


def _client(
    requests: list[dict[str, Any]],
    *,
    fields: dict[str, Any] | None = None,
    deltas: list[dict[str, Any]] | None = None,
    tool_call: bool = False,
    profile_name: str = "synthetic-profile",
    **overrides: Any,
) -> compat.OpenAICompatClient:
    options = {
        "base_url": _BASE_URL,
        "provider_key": "zai_coding_plan",
        "model": _MODEL,
        "api_key": "synthetic-not-a-credential",
        "reasoning_trace_adapter": "auto",
        **overrides,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        message = {"role": "assistant", "content": "Local reply.", **(fields or {})}
        if tool_call:
            message["tool_calls"] = [
                {
                    "id": "synthetic-call",
                    "type": "function",
                    "function": {"name": "fs_read", "arguments": '{"path":"README.md"}'},
                }
            ]
        if not body.get("stream"):
            return httpx.Response(200, json={"choices": [{"message": message}]})
        final_delta = copy.deepcopy(message)
        final_delta.pop("role")
        if tool_call:
            final_delta["tool_calls"][0]["index"] = 0
        chunks = [*copy.deepcopy(deltas or []), final_delta]
        events = [
            {"choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}]} for delta in chunks
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events)
            + "data: [DONE]\n\n",
        )

    identity = build_provider_route_identity(
        protocol="openai_compat",
        base_url=options["base_url"],
        provider_key=options["provider_key"],
        model=options["model"],
        profile_name=profile_name,
        credential_scope=credential_scope_fingerprint(options["api_key"]),
        reasoning_state_adapter=options["reasoning_trace_adapter"],
    )
    return compat.OpenAICompatClient(
        **options, route_identity=identity, transport=httpx.MockTransport(handler)
    )


def _follow_up(
    client: compat.OpenAICompatClient, *, stream: bool = False, expected_trace: str = ""
) -> dict[str, Any]:
    messages = [{"role": "user", "content": "Inspect the synthetic input."}]
    displayed_reasoning: list[str] = []
    response = client.chat(
        messages=messages, stream=stream, on_reasoning_delta=displayed_reasoning.append
    )
    assert [item.text for item in response.reasoning] == (
        [expected_trace] if expected_trace.strip() else []
    )
    assert "".join(displayed_reasoning) == expected_trace
    assert all(item.kind == ReasoningOutputKind.PROVIDER_REASONING for item in response.reasoning)
    assistant = assistant_message_from_response(response)
    # Exercise JSON persistence as well as the adapter's ordinary history input.
    assistant = json.loads(json.dumps(assistant, ensure_ascii=False))
    messages.append(assistant)
    if response.tool_calls:
        messages.append(
            {"role": "tool", "tool_call_id": "synthetic-call", "content": "Synthetic result."}
        )
    messages.append({"role": "user", "content": "Continue the same request."})
    before = copy.deepcopy(messages)
    client.chat(messages=messages, stream=stream)
    assert messages == before
    return assistant


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tool_call", [False, True])
@pytest.mark.parametrize(
    "state", [_STATE, "", " \t\nΩ "], ids=["long-state", "empty", "whitespace"]
)
def test_contract_replays_exact_native_reasoning_content(
    stream: bool, tool_call: bool, state: str
) -> None:
    requests: list[dict[str, Any]] = []
    client = _client(requests, fields={"reasoning_content": state}, tool_call=tool_call)
    assistant = _follow_up(client, stream=stream, expected_trace=state)
    assert assistant[PROVIDER_METADATA_KEY][OPENAI_COMPAT_REASONING_METADATA_KEY] == {
        "reasoning_content": state
    }
    wire = requests[-1]["messages"][1]
    assert wire["reasoning_content"] == state
    assert PROVIDER_METADATA_KEY not in wire
    assert wire["content"] == "Local reply."
    if tool_call:
        assert wire["tool_calls"] == assistant["tool_calls"]
        assert requests[-1]["messages"][2]["role"] == "tool"
    # Full private state must not enter public content or request diagnostics.
    if state == _STATE:
        assert state not in assistant["content"]
        assert state not in json.dumps(assistant[PROVIDER_METADATA_KEY]["openai_compat"])
        telemetry = last_provider_call_summary()
        assert telemetry is not None
        assert state not in json.dumps(telemetry)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "fields", [{}, {"reasoning_content": None}, {"reasoning_content": 7}, {"reasoning": _STATE}]
)
def test_missing_native_string_never_manufactures_replay_state(
    stream: bool, fields: dict[str, Any]
) -> None:
    requests: list[dict[str, Any]] = []
    assistant = _follow_up(_client(requests, fields=fields), stream=stream)
    assert PROVIDER_METADATA_KEY not in assistant
    assert "reasoning_content" not in requests[-1]["messages"][1]


def test_stream_native_fragments_do_not_concatenate_separate_reasoning_alias() -> None:
    requests: list[dict[str, Any]] = []
    deltas = [
        {"reasoning_content": " Ω"},
        {"reasoning": "ALIAS_MUST_NOT_REPLAY"},
        {"reasoning_content": "", "reasoning": "SIMULTANEOUS_ALIAS"},
        {"reasoning_content": "\n tail  "},
    ]
    _follow_up(_client(requests, deltas=deltas), stream=True, expected_trace=" Ω\n tail  ")
    assert requests[-1]["messages"][1]["reasoning_content"] == " Ω\n tail  "


@pytest.mark.parametrize("adapter", ["none", "openai_compat_passive"])
@pytest.mark.parametrize("stream", [False, True])
def test_disabled_or_passive_adapter_neither_captures_nor_replays(
    adapter: str, stream: bool
) -> None:
    requests: list[dict[str, Any]] = []
    client = _client(
        requests, fields={"reasoning_content": _STATE}, reasoning_trace_adapter=adapter
    )
    assistant = _follow_up(client, stream=stream)
    assert PROVIDER_METADATA_KEY not in assistant
    # Even same-route private metadata must not bypass the disabled capability.
    assistant[PROVIDER_METADATA_KEY] = stamp_provider_metadata_for_route(
        {OPENAI_COMPAT_REASONING_METADATA_KEY: {"reasoning_content": _STATE}},
        client.route_identity,
    )
    client.chat(messages=[assistant, {"role": "user", "content": "Continue."}])
    assert "reasoning_content" not in requests[-1]["messages"][0]


@pytest.mark.parametrize(
    "route_change",
    [
        {"api_key": "other-synthetic-credential"},
        {"model": "glm-5.3"},
        {"profile_name": "another-synthetic-profile"},
        {"base_url": _BASE_URL + "/other"},
    ],
)
def test_protocol_state_cannot_cross_route_identity(route_change: dict[str, str]) -> None:
    original_requests: list[dict[str, Any]] = []
    producer = _client(original_requests, fields={"reasoning_content": _STATE})
    response = producer.chat(messages=[{"role": "user", "content": "Start."}])
    assistant = assistant_message_from_response(response)
    requests: list[dict[str, Any]] = []
    consumer = _client(requests, **route_change)
    consumer.chat(messages=[assistant, {"role": "user", "content": "Continue."}])
    assert requests[-1]["messages"][0] == {"role": "assistant", "content": "Local reply."}


def test_unknown_contract_does_not_replay_even_stamped_protocol_state() -> None:
    requests: list[dict[str, Any]] = []
    client = _client(
        requests,
        provider_key="synthetic-vendor",
        model="synthetic-model",
        base_url="https://synthetic.invalid/v1",
        fields={"reasoning_content": _STATE},
    )
    assistant = _follow_up(client)
    assert PROVIDER_METADATA_KEY not in assistant
    assistant[PROVIDER_METADATA_KEY] = stamp_provider_metadata_for_route(
        {OPENAI_COMPAT_REASONING_METADATA_KEY: {"reasoning_content": _STATE}},
        client.route_identity,
    )
    client.chat(messages=[assistant])
    assert "reasoning_content" not in requests[-1]["messages"][0]


def test_generic_replay_depends_on_capability_not_vendor_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def synthetic_contract(provider_key: str | None, model: str | None) -> ReasoningContract:
        if provider_key == "synthetic_vendor":
            return ReasoningContract(replay_reasoning_content=True)
        return reasoning_contract_for(provider_key, model)

    monkeypatch.setattr(compat, "reasoning_contract_for", synthetic_contract)
    requests: list[dict[str, Any]] = []
    client = _client(
        requests,
        provider_key="synthetic-vendor",
        model="synthetic-model",
        base_url="https://synthetic.invalid/v1",
        fields={"reasoning_content": _STATE},
    )
    _follow_up(client)
    assert requests[-1]["messages"][1]["reasoning_content"] == _STATE


@pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash", "glm-4.7"])
def test_documented_coding_plan_capability_does_not_enable_standard_api(model: str) -> None:
    assert reasoning_contract_for("zai_coding_plan", model).replay_reasoning_content
    assert not reasoning_contract_for("zhipu", model).replay_reasoning_content


def test_coding_plan_declares_native_trace_and_sensitive_continuation() -> None:
    automatic = resolve_reasoning_trace_capability(
        provider_key="zai_coding_plan", protocol="openai_compat", adapter_override="auto"
    )
    assert automatic.continuation_state == "sensitive"
    assert automatic.output_kind == ReasoningOutputKind.PROVIDER_REASONING
    assert automatic.has_visible_reasoning
    assert not automatic.has_safe_summary
    for adapter in ("none", "openai_compat_passive"):
        explicit = resolve_reasoning_trace_capability(
            provider_key="zai_coding_plan", protocol="openai_compat", adapter_override=adapter
        )
        assert explicit.continuation_state == "none"
        assert explicit.output_kind is None


@pytest.mark.parametrize("adapter", ["auto", "openai_compat_passive"])
@pytest.mark.parametrize("stream", [False, True])
def test_configured_factory_route_preserves_auto_replay_and_explicit_passive(
    adapter: str, stream: bool
) -> None:
    from test_llm_factory_profiles import _cfg_with_profile

    from alysis_code.llm.factory import make_llm_client
    from alysis_code.profiles import ProfileSpec

    requests: list[dict[str, Any]] = []
    template = _client(requests, fields={"reasoning_content": _STATE})
    profile = ProfileSpec(
        name="zai-coding-plan",
        protocol="openai_compat",
        base_url=_BASE_URL,
        default_model=_MODEL,
        reasoning_trace_adapter=adapter,
    )
    client = make_llm_client(
        cfg=_cfg_with_profile(profile),
        api_key="synthetic-not-a-credential",
        model=_MODEL,
        transport=template._transport,
    )
    assert isinstance(client, compat.OpenAICompatClient)
    assert client.provider_key == "zai_coding_plan"
    assert client.reasoning_trace_adapter == adapter
    assert client.route_identity.reasoning_state_adapter == adapter
    assert client.route_identity.profile_name == profile.name
    assert client.reasoning_trace_capability.output_kind == (
        ReasoningOutputKind.PROVIDER_REASONING if adapter == "auto" else None
    )
    assert client.reasoning_trace_capability.continuation_state == (
        "sensitive" if adapter == "auto" else "none"
    )
    _follow_up(client, stream=stream, expected_trace=_STATE if adapter == "auto" else "")
    if adapter == "auto":
        assert requests[-1]["messages"][1]["reasoning_content"] == _STATE
    else:
        assert "reasoning_content" not in requests[-1]["messages"][1]


@pytest.mark.parametrize("stream", [False, True])
def test_actual_parent_turns_keep_complete_reasoning_state(tmp_path: Path, stream: bool) -> None:
    from test_agent_loop_event_emission import _make_session, _RecordingEventSurface
    from test_subagents import _readonly_subagent_tools

    requests: list[dict[str, Any]] = []
    session = _make_session(
        root=tmp_path,
        client=_client(requests, fields={"reasoning_content": _STATE}),
        surface=_RecordingEventSurface(),
        tool=_readonly_subagent_tools()["fs_read"],
        stream=stream,
    )
    try:
        assert session.run_turn("Inspect first source.") == 0
        assert session.run_turn("Inspect second source.") == 0
    finally:
        session.close()
    assert len(requests) == 2
    historical = [m for m in requests[-1]["messages"] if m.get("role") == "assistant"]
    assert len(historical) == 1
    assert historical[0]["reasoning_content"] == _STATE


@pytest.mark.parametrize("route", ["coding_plan", "qwen_control"])
def test_successful_child_resume_replays_complete_persisted_reasoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    import test_wire_request_diagnostics as wire_tests

    requests: list[dict[str, Any]] = []

    def capturing_client(_protocol: str, captured: list[dict[str, Any]]) -> Any:
        nonlocal requests
        requests = captured
        options = (
            {
                "base_url": "https://coding.dashscope.aliyuncs.com/v1",
                "provider_key": "qwen",
                "model": "qwen3.8-max",
            }
            if route == "qwen_control"
            else {}
        )
        return _client(captured, fields={"reasoning_content": _STATE}, **options)

    # The existing integration helper exercises actual AgentSession/SessionStore,
    # successful run, coordinator resume, wait and restored-history ordering.
    monkeypatch.setattr(wire_tests, "_capturing_client", capturing_client)
    wire_tests.test_native_successful_child_resume_keeps_recorded_history_order(
        tmp_path, monkeypatch, "compat"
    )
    historical = [m for m in requests[-1]["messages"] if m.get("role") == "assistant"]
    assert len(historical) == 1
    assert historical[0]["reasoning_content"] == _STATE
