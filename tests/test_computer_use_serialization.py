from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent_loop import create_session
from alysis_code.computer_use_diagnostics import (
    DIAGNOSTIC_ATTRIBUTE,
    protected_provider_diagnostic_marker,
)
from alysis_code.config import AppConfig
from alysis_code.llm.base import (
    require_omitted_reasoning_display,
    require_serial_tool_calls,
    require_stateless_responses,
    require_tool_calling,
)
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.provider_auth import ProviderRequestAuthorization
from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth
from alysis_code.surface.noop_surface import NoopSurface


class _SyntheticSubscriptionAuth:
    requires_streaming = True
    supports_previous_response_id = False
    adapt_responses_payload = OpenAICodexSubscriptionAuth.adapt_responses_payload

    def route_credential_scope(self) -> str:
        return "synthetic-scope"

    def authorize_request(self, *_args: Any, **_kwargs: Any) -> ProviderRequestAuthorization:
        return ProviderRequestAuthorization(
            credential_scope="synthetic-scope",
            headers={"Authorization": "Bearer synthetic-fixture"},
        )


def _stream_response(index: int, calls: list[tuple[str, dict[str, Any]]]) -> httpx.Response:
    items: list[dict[str, Any]] = [
        {
            "type": "reasoning",
            "id": f"reasoning-{index}",
            "summary": [],
            "encrypted_content": "synthetic-encrypted-fixture",
        }
    ]
    for position, (name, arguments) in enumerate(calls):
        items.append(
            {
                "type": "function_call",
                "id": f"function-{index}-{position}",
                "call_id": f"call-{index}-{position}",
                "name": name,
                "arguments": json.dumps(arguments),
                "status": "completed",
            }
        )
    if not calls:
        items.append(
            {
                "type": "message",
                "id": f"message-{index}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Verified fixture complete."}],
            }
        )
    events: list[dict[str, Any]] = []
    for output_index, item in enumerate(items):
        events.append(
            {"type": "response.output_item.added", "output_index": output_index, "item": item}
        )
        if item["type"] == "function_call":
            events.append(
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": output_index,
                    "item_id": item["id"],
                    "call_id": item["call_id"],
                    "name": item["name"],
                    "arguments": item["arguments"],
                }
            )
        events.append(
            {"type": "response.output_item.done", "output_index": output_index, "item": item}
        )
    events.append(
        {
            "type": "response.completed",
            "response": {
                "id": f"response-{index}",
                "model": "test-model",
                "status": "completed",
                "output": items,
            },
        }
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=b"".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
        ),
    )


def _session(tmp_path: Path, handler: Any, host: Any) -> Any:
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=True,
        api_key_override="synthetic-fixture",
        surface=NoopSurface(),
        verification_enabled=False,
        sensitive_media=True,
        computer_use_host_request=host,
    )
    client = OpenAIResponsesClient(
        base_url="https://synthetic.invalid/v1",
        api_key="synthetic-fixture",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_auth=_SyntheticSubscriptionAuth(),  # type: ignore[arg-type]
    )
    require_tool_calling(client, ("computer_state", "computer_action", "computer_wait"))
    require_omitted_reasoning_display(client)
    require_serial_tool_calls(client)
    require_stateless_responses(client)
    session.client = client
    return session


@pytest.mark.parametrize("initial_observation", [False, True])
def test_refused_streamed_batch_recovers_with_fresh_observation(
    tmp_path: Path, initial_observation: bool
) -> None:
    requests: list[dict[str, Any]] = []
    host_calls: list[tuple[str, dict[str, Any]]] = []
    batch_index = 2 if initial_observation else 1

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        if initial_observation and index == 1:
            return _stream_response(index, [("computer_state", {})])
        if index == batch_index:
            return _stream_response(
                index,
                [
                    ("computer_state", {}),
                    ("computer_action", {"action": "press", "element_id": "rejected"}),
                ],
            )
        if index == batch_index + 1:
            # Exercise the real stateless subscription payload after the refused
            # response, preserving later host correction as developer input
            # after the user turn rather than hoisting it into the prefix.
            developer_context = json.dumps(
                [item for item in body["input"] if item.get("role") == "developer"]
            )
            assert "Computer Use protocol correction" in developer_context
            assert "exactly one Computer Use tool call" in developer_context
            assert "Computer Use protocol correction" not in body["instructions"]
            # Subscription functions are namespaced: forced recovery keeps only
            # the selected callable and requires it, rather than sending the
            # bare function selector rejected by the subscription endpoint.
            assert body["tool_choice"] == "required"
            assert len(body["tools"]) == 1
            namespace = body["tools"][0]
            assert namespace["type"] == "namespace" and namespace["name"] == "alysis"
            assert [tool["name"] for tool in namespace["tools"]] == ["computer_state"]
            return _stream_response(index, [("computer_state", {})])
        if index == batch_index + 2:
            return _stream_response(
                index,
                [
                    (
                        "computer_action",
                        {
                            "action": "press",
                            "element_id": "fresh-element",
                            "snapshot_id": "untrusted-model-snapshot",
                        },
                    )
                ],
            )
        return _stream_response(index, [])

    def host(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, dict(arguments)))
        if name == "computer_state":
            return {
                "snapshot_id": f"trusted-{len(host_calls)}",
                "elements": [{"id": "fresh-element", "role": "AXButton"}],
            }
        return {"receipt": {"action": "press"}, "state": {"snapshot_id": "post-action"}}

    session = _session(tmp_path, handler, host)
    try:
        assert session.run_turn("Use the fixture button.") == 0
    finally:
        session.close()

    assert [name for name, _ in host_calls] == (
        (["computer_state"] if initial_observation else []) + ["computer_state", "computer_action"]
    )
    assert host_calls[-1][1]["snapshot_id"] == f"trusted-{len(host_calls) - 1}"
    assert host_calls[-1][1]["element_id"] == "fresh-element"
    for body in requests:
        assert body["parallel_tool_calls"] is False
        assert body["stream"] is True
        assert body["store"] is False
        assert "previous_response_id" not in body
        assert all(
            item.get("call_id") not in {f"call-{batch_index}-0", f"call-{batch_index}-1"}
            for item in body["input"]
        )


def test_repeated_streamed_batches_fail_closed_with_precise_diagnostic(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) > 1:
            developer_context = json.dumps(
                [item for item in body["input"] if item.get("role") == "developer"]
            )
            assert "Computer Use protocol correction" in developer_context
            assert "Computer Use protocol correction" not in body["instructions"]
        return _stream_response(
            len(requests), [("computer_state", {}), ("computer_wait", {"duration_ms": 10})]
        )

    def host(*_args: Any) -> dict[str, Any]:
        pytest.fail("A refused batch must never be sent to the host")

    session = _session(tmp_path, handler, host)
    try:
        with pytest.raises(AgentRuntimeError) as rejected:
            session.run_turn("Use the fixture button.")
    finally:
        session.close()
    assert len(requests) == 3, protected_provider_diagnostic_marker(rejected.value)
    diagnostic = getattr(rejected.value, DIAGNOSTIC_ATTRIBUTE)
    assert diagnostic.site == "turn_core"
    assert 1 <= diagnostic.line <= 99999
    assert protected_provider_diagnostic_marker(rejected.value) == (
        "[computer_use_provider_diagnostic class=AgentRuntimeError category=provider_error "
        f"status=0 code=multiple_calls_refused parameter=none site=turn_core line={diagnostic.line}]"
    )


def test_poisoned_session_fails_before_provider_or_host_with_precise_diagnostic(
    tmp_path: Path,
) -> None:
    def unexpected(*_args: Any) -> Any:
        pytest.fail("A poisoned session must not invoke provider or host")

    session = _session(tmp_path, unexpected, unexpected)
    session.sensitive_session_poisoned = True
    try:
        with pytest.raises(AgentRuntimeError) as rejected:
            session.run_turn("Use the fixture button.")
    finally:
        session.close()
    diagnostic = getattr(rejected.value, DIAGNOSTIC_ATTRIBUTE)
    assert diagnostic.site == "agent_session"
    assert 1 <= diagnostic.line <= 99999
    assert protected_provider_diagnostic_marker(rejected.value) == (
        "[computer_use_provider_diagnostic class=AgentRuntimeError category=implementation_failed "
        f"status=0 code=poisoned_session parameter=none site=agent_session line={diagnostic.line}]"
    )


def test_chrome_surrogate_text_completes_real_subscription_http_tool_loop(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []
    host_calls: list[tuple[str, dict[str, Any]]] = []
    raw_label = "invoice \ud835 / \udc00 Ελληνικά 🧾"
    normalized_label = "invoice � / � Ελληνικά 🧾"
    snapshot = "snapshot-Δ-🧾"
    element = "element-Δ-🧾"
    cursor = "opaque-cursor-Δ-🧾"
    host_state = {
        "source": "chrome-dom",
        "snapshot_id": snapshot,
        "target": {"name": "Chrome", "window_title": raw_label},
        "title": raw_label,
        "elements": [
            {"id": element, "label": raw_label, "value": "\udc00 value 😀", "role": "AXButton"}
        ],
        "page": {"next_cursor": cursor, "complete": False},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request.content.decode("utf-8", errors="strict")
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        if index == 1:
            return _stream_response(index, [("computer_state", {})])
        if index == 2:
            outputs = [
                item["output"]
                for item in body["input"]
                if item.get("type") == "function_call_output"
            ]
            observation = json.loads(outputs[-1])
            assert observation["title"] == normalized_label
            assert observation["target"]["window_title"] == normalized_label
            assert observation["elements"][0]["label"] == normalized_label
            assert observation["elements"][0]["value"] == "� value 😀"
            assert observation["snapshot_id"] == snapshot
            assert observation["elements"][0]["id"] == element
            assert observation["page"]["next_cursor"] == cursor
            return _stream_response(
                index, [("computer_action", {"action": "press", "element_id": element})]
            )
        return _stream_response(index, [])

    def host(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, dict(arguments)))
        if name == "computer_state":
            return host_state
        return {
            "receipt": {"action": "press"},
            "state": {"snapshot_id": "post-action", "title": raw_label},
        }

    session = _session(tmp_path, handler, host)
    try:
        assert session.run_turn("Use the fixture invoice button. Ελληνικά 🧾") == 0
    finally:
        session.close()
    assert len(requests) == 3
    assert [name for name, _ in host_calls] == ["computer_state", "computer_action"]
    assert host_calls[-1][1]["snapshot_id"] == snapshot
    assert host_calls[-1][1]["element_id"] == element
    assert host_state["title"] == raw_label
    assert host_state["elements"][0]["label"] == raw_label


def test_computer_use_does_not_retry_stream_rejection_from_internal_subscription_sse(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "stream not supported"}})

    def host(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("A rejected provider request must not dispatch host actions")

    session = _session(tmp_path, handler, host)
    session.stream = True
    try:
        with pytest.raises(AgentRuntimeError) as rejected:
            session.run_turn("Inspect the desktop fixture.")
        diagnostic = getattr(rejected.value, DIAGNOSTIC_ATTRIBUTE)
        assert diagnostic.error_class == "LLMError"
        assert diagnostic.status == 400
        assert len(requests) == 1
        # Subscription authentication requires SSE on the wire even though the
        # protected host disables public streaming. It cannot fall back by
        # replaying that protected request after a streaming rejection.
        assert requests[0]["stream"] is True
    finally:
        session.close()
