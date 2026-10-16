from __future__ import annotations

import copy
import io
import json
import re
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from rich.console import Console

from alysis_code.agent import session as agent_session_module
from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent.sensitive_output import (
    canonicalize_computer_use_response_for_dispatch,
    collect_computer_use_private_identifiers,
    collect_sensitive_response_taints,
    computer_use_ephemeral_provider_replay_state,
    inject_ephemeral_computer_use_provider_state,
    inject_ephemeral_sensitive_tool_messages,
    redact_assistant_tool_call_message,
    redact_consumed_sensitive_tool_messages,
    redact_sensitive_response_for_persistence,
    redact_sensitive_response_taints,
    redact_sensitive_tool_arguments,
    redact_sensitive_tool_result,
    sensitive_tool_boundary,
    strip_computer_use_provider_state_for_persistence,
)
from alysis_code.agent_loop import AgentSession, ToolDef
from alysis_code.compaction.tool_output_offload import ToolOutputOffloader
from alysis_code.config import AppConfig
from alysis_code.hooks.models import HookDispatchResult
from alysis_code.llm.base import (
    require_serial_tool_calls,
    require_stateless_responses,
    require_tool_calling,
)
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.metadata import (
    ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY,
    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
    OPENAI_RESPONSES_PROVIDER_METADATA_KEY,
    PROVIDER_METADATA_KEY,
    ROUTE_IDENTITY_PROVIDER_METADATA_KEY,
    assistant_message_from_response,
)
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError, ReasoningOutput, ReasoningOutputKind
from alysis_code.model_registry import ModelMeta
from alysis_code.session_artifacts import SessionArtifactLayout
from alysis_code.session_store import SessionStore
from alysis_code.surface.events import (
    Event,
    ToolCallCompleted,
    ToolCallProgress,
    ToolCallStarted,
)
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.usage_tracker import UsageSummary

_CANARY = "ALYSIS_SECRET_CANARY_7dc6f758"
_ERROR_CANARY = "ALYSIS_EXCEPTION_CANARY_c18afe42"
_PRIVATE_TYPED_TEXT = "TypedPhraseOrchid"
_PROTECTED_FAILURE_MESSAGE = (
    "Computer Use stopped because the protected provider or host request failed."
)
_SCRUBBED_FAILURE_MESSAGE = "[redacted: protected Computer Use failure]"


class _FakeRegistry:
    def get(self, model_name: str) -> ModelMeta:
        return ModelMeta(
            model_name=model_name,
            context_window_tokens=8192,
            max_output_tokens=2048,
            input_cost_per_token=None,
            output_cost_per_token=None,
            raw_metadata={},
            source="fallback",
        )


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(
        self,
        responses: list[LLMResponse | BaseException],
        *,
        reasoning_delta: str | None = None,
    ) -> None:
        self.responses = list(responses)
        self.calls = 0
        # Keep the host's request objects by reference. The runtime must scrub
        # the one-call-only sensitive material after the provider consumes it.
        self.request_references: list[list[dict[str, Any]]] = []
        self.saw_canary_during_call: list[bool] = []
        self.request_snapshots: list[str] = []
        self.stream_flags: list[bool] = []
        self.reasoning_callback_flags: list[bool] = []
        self.reasoning_delta = reasoning_delta
        if reasoning_delta is not None:
            self.reasoning_trace_capability = SimpleNamespace(
                has_safe_summary=True,
                supports_buffered=True,
                supports_streaming=True,
            )

    def chat(self, *, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self.request_references.append(messages)
        request_snapshot = _serialized(messages)
        self.request_snapshots.append(request_snapshot)
        self.saw_canary_during_call.append(_CANARY in request_snapshot)
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        stream = bool(kwargs.get("stream"))
        self.stream_flags.append(stream)
        on_reasoning_delta = kwargs.get("on_reasoning_delta")
        self.reasoning_callback_flags.append(callable(on_reasoning_delta))
        if callable(on_reasoning_delta) and self.reasoning_delta:
            on_reasoning_delta(self.reasoning_delta)
        if isinstance(response, BaseException):
            raise response
        on_text_delta = kwargs.get("on_text_delta")
        if stream and callable(on_text_delta) and response.content:
            on_text_delta(response.content)
        return response


class _RecordingSurface(NoopSurface):
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.legacy_events: list[Any] = []
        self.message_events: list[dict[str, Any]] = []
        self.assistant_messages: list[str] = []
        self.reasoning_events: list[dict[str, str]] = []

    def on_reasoning_start(self, block_id: str) -> None:
        self.reasoning_events.append({"event": "start", "value": block_id})

    def on_reasoning_token(self, delta: str) -> None:
        self.reasoning_events.append({"event": "token", "value": delta})

    def on_reasoning_end(self, block_id: str) -> None:
        self.reasoning_events.append({"event": "end", "value": block_id})

    def emit_message_delta(
        self,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.message_events.append(
            {"event": "delta", "text": text, "worker_id": worker_id, "role": role}
        )

    def emit_message_end(
        self,
        text: str = "",
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.message_events.append(
            {"event": "end", "text": text, "worker_id": worker_id, "role": role}
        )

    def on_assistant_message_done(self, text: str) -> None:
        self.assistant_messages.append(text)

    def emit_tool_call_started(
        self,
        call_id: str,
        name: str,
        arguments_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(ToolCallStarted(call_id, name, arguments_preview, worker_id, role))

    def emit_tool_call_progress(
        self,
        call_id: str,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(ToolCallProgress(call_id, text, worker_id, role))

    def emit_tool_call_completed(
        self,
        call_id: str,
        success: bool,
        result_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self.events.append(ToolCallCompleted(call_id, success, result_preview, worker_id, role))

    def on_tool_start(self, event: Any) -> None:
        self.legacy_events.append(event)

    def on_tool_output(self, event: Any) -> None:
        self.legacy_events.append(event)

    def on_tool_end(self, event: Any) -> None:
        self.legacy_events.append(event)


class _RecordingHooks:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def fire_user_prompt_submit(self, **payload: Any) -> HookDispatchResult:
        self.payloads.append(copy.deepcopy(payload))
        return HookDispatchResult()

    def fire_pre_tool_use(self, **payload: Any) -> HookDispatchResult:
        self.payloads.append(copy.deepcopy(payload))
        return HookDispatchResult()

    def fire_post_tool_use(self, **payload: Any) -> HookDispatchResult:
        self.payloads.append(copy.deepcopy(payload))
        return HookDispatchResult()

    def fire_turn_complete(self, **payload: Any) -> HookDispatchResult:
        self.payloads.append(copy.deepcopy(payload))
        return HookDispatchResult()

    def fire_session_end(self, **payload: Any) -> HookDispatchResult:
        self.payloads.append(copy.deepcopy(payload))
        return HookDispatchResult()


class _RecordingDiagnostics:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def event(self, event_type: str, payload: dict[str, Any], *, durable: bool = False) -> None:
        self.events.append(
            {"event_type": event_type, "payload": copy.deepcopy(payload), "durable": durable}
        )


def _serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _capture_protected_turn_failure(
    session: AgentSession,
) -> tuple[BaseException, traceback.TracebackException]:
    """Capture the outward traceback before the test frame can retain canaries."""

    try:
        session.run_turn("Observe the approved control, then continue the protected workflow.")
    except BaseException as error:
        return error, traceback.TracebackException.from_exception(error, capture_locals=True)
    raise AssertionError("protected Computer Use turn unexpectedly succeeded")


def test_computer_use_provider_native_arguments_follow_the_one_call_boundary() -> None:
    call_id = "call-computer-use-sensitive"
    provider_snapshot = "snapshotProviderZulu"
    semantic_element = "semanticElementCedar"
    arguments = {
        "snapshot_id": provider_snapshot,
        "action": "set_value",
        "element_id": semantic_element,
        "text": _CANARY,
    }
    raw_arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))

    global_taints = collect_sensitive_response_taints("computer_action", arguments, {})
    assert provider_snapshot not in global_taints
    assert semantic_element not in global_taints
    assert set(collect_computer_use_private_identifiers("computer_action", arguments)) == {
        provider_snapshot,
        semantic_element,
    }

    for provider_key, provider_payload in (
        (
            OPENAI_RESPONSES_PROVIDER_METADATA_KEY,
            {
                "response_id": "resp-sensitive",
                "output_items": [
                    {
                        "type": "function_call",
                        "id": "fc-sensitive",
                        "call_id": call_id,
                        "name": "computer_action",
                        "arguments": raw_arguments,
                    }
                ],
            },
        ),
        (
            ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY,
            {
                "message_id": "msg-sensitive",
                "content_blocks": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": "computer_action",
                        "input": copy.deepcopy(arguments),
                    }
                ],
            },
        ),
        (
            GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
            {
                "response_id": "gemini-response-sensitive",
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "thoughtSignature": "gemini-opaque-signature",
                            "functionCall": {
                                "id": call_id,
                                "name": "computer_action",
                                "args": copy.deepcopy(arguments),
                            },
                        }
                    ],
                },
            },
        ),
    ):
        response = LLMResponse(
            content=f"Setting {_CANARY}",
            tool_calls=[
                ToolCall(
                    id=call_id,
                    name="computer_action",
                    arguments=copy.deepcopy(arguments),
                )
            ],
            raw={"arguments": raw_arguments},
            provider_metadata={provider_key: provider_payload},
        )
        projected = assistant_message_from_response(response)
        assert _CANARY in _serialized(projected)

        durable = redact_assistant_tool_call_message(projected)
        serialized_durable = _serialized(durable)
        assert _CANARY not in serialized_durable
        assert provider_snapshot not in serialized_durable
        assert semantic_element not in serialized_durable
        assert "[redacted: transient computer use identifier]" in serialized_durable
        if provider_key == OPENAI_RESPONSES_PROVIDER_METADATA_KEY:
            assert durable[PROVIDER_METADATA_KEY][provider_key]["response_id"] == "resp-sensitive"
        if provider_key == ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY:
            assert durable[PROVIDER_METADATA_KEY][provider_key]["message_id"] == "msg-sensitive"
        if provider_key == GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY:
            gemini_part = durable[PROVIDER_METADATA_KEY][provider_key]["content"]["parts"][0]
            assert gemini_part["thoughtSignature"] == "gemini-opaque-signature"
            assert gemini_part["functionCall"]["id"] == call_id
            assert gemini_part["functionCall"]["args"] == {
                "snapshot_id": "[redacted: transient computer use identifier]",
                "action": "set_value",
                "element_id": "[redacted: transient computer use identifier]",
                "text": "[redacted: sensitive file content]",
            }

        injected = inject_ephemeral_sensitive_tool_messages(
            [durable],
            result_content={},
            arguments_content={call_id: raw_arguments},
        )
        assert _CANARY in _serialized(injected)
        assert provider_snapshot in _serialized(injected)
        assert semantic_element in _serialized(injected)
        if provider_key == GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY:
            injected_gemini_part = injected[0][PROVIDER_METADATA_KEY][provider_key]["content"][
                "parts"
            ][0]
            assert injected_gemini_part["thoughtSignature"] == "gemini-opaque-signature"
            assert injected_gemini_part["functionCall"]["args"] == arguments
        redact_consumed_sensitive_tool_messages(injected, {call_id: "redacted-result"})
        serialized_consumed = _serialized(injected)
        assert _CANARY not in serialized_consumed
        assert provider_snapshot not in serialized_consumed
        assert semantic_element not in serialized_consumed

        persistence_safe = redact_sensitive_response_for_persistence(response)
        serialized_persistence = _serialized(persistence_safe)
        assert _CANARY not in serialized_persistence
        assert provider_snapshot not in serialized_persistence
        assert semantic_element not in serialized_persistence


def test_computer_use_owns_provider_call_ids_and_canonicalizes_known_names() -> None:
    provider_call_id = _CANARY
    provider_item_id = f"item-{_CANARY}"
    provider_signature = "opaque-canonical-signature"
    arguments = {
        "snapshot_id": "snapshotCanonicalPrivate",
        "action": "set_value",
        "element_id": "elementCanonicalPrivate",
        "text": _PRIVATE_TYPED_TEXT,
    }
    response = LLMResponse(
        content="Calling the approved Computer Use action.",
        tool_calls=[
            ToolCall(
                id=provider_call_id,
                name=" COMPUTER_ACTION ",
                arguments=arguments,
                provider_metadata={
                    OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                        "item_id": provider_item_id,
                        "output_index": 0,
                    },
                    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                        "part_index": 0,
                        "thoughtSignature": provider_signature,
                    },
                },
            )
        ],
        raw={"id": provider_item_id, "call_id": provider_call_id},
        provider_metadata={
            OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                "response_id": f"response-{_CANARY}",
                "output_items": [
                    {
                        "type": "function_call",
                        "id": provider_item_id,
                        "call_id": provider_call_id,
                        "name": " COMPUTER_ACTION ",
                        "arguments": json.dumps(arguments),
                    }
                ],
            },
            ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY: {
                "message_id": f"message-{_CANARY}",
                "content_blocks": [
                    {
                        "type": "tool_use",
                        "id": provider_call_id,
                        "name": " COMPUTER_ACTION ",
                        "input": copy.deepcopy(arguments),
                    }
                ],
            },
            GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                "response_id": f"gemini-{_CANARY}",
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "thoughtSignature": provider_signature,
                            "functionCall": {
                                "id": provider_call_id,
                                "name": "computer_action",
                                "args": copy.deepcopy(arguments),
                            },
                        }
                    ],
                },
            },
        },
    )

    canonical = canonicalize_computer_use_response_for_dispatch(response)

    call = canonical.tool_calls[0]
    assert re.fullmatch(r"alysis_cu_call_[0-9a-f]{40}", call.id)
    assert call.name == "computer_action"
    assert call.arguments is arguments
    assert canonical.raw == {}

    # Alysis Code owns canonical public IDs, while the minimum provider-native
    # linkage remains available only on this transient response for exact replay.
    openai = canonical.provider_metadata[OPENAI_RESPONSES_PROVIDER_METADATA_KEY]
    assert "response_id" not in openai
    assert openai["output_items"][0]["call_id"] == provider_call_id
    assert openai["output_items"][0]["id"] == provider_item_id
    assert openai["output_items"][0]["name"] == "computer_action"
    assert openai["call_ids_rewritten"] is True

    anthropic = canonical.provider_metadata[ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY]
    assert "message_id" not in anthropic
    assert anthropic["content_blocks"][0]["id"] == provider_call_id
    assert anthropic["content_blocks"][0]["name"] == "computer_action"

    gemini = canonical.provider_metadata[GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY]
    assert "response_id" not in gemini
    gemini_part = gemini["content"]["parts"][0]
    assert gemini_part["thoughtSignature"] == provider_signature
    assert gemini_part["functionCall"]["id"] == provider_call_id
    assert gemini_part["functionCall"]["name"] == "computer_action"

    replay_state = computer_use_ephemeral_provider_replay_state(canonical)
    assert set(replay_state) == {call.id}
    replay_entry = replay_state[call.id]
    assert replay_entry["provider_call_id"] == provider_call_id
    assert "provider_call_id_omitted" not in replay_entry
    replay_metadata = replay_entry["provider_metadata"]
    assert replay_metadata[ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY] == anthropic
    assert replay_metadata[GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY] == gemini
    replay_openai_call = replay_metadata[OPENAI_RESPONSES_PROVIDER_METADATA_KEY]["output_items"][0]
    assert replay_openai_call["call_id"] == provider_call_id
    assert json.loads(replay_openai_call["arguments"]) == arguments

    persistence_safe = strip_computer_use_provider_state_for_persistence(
        redact_sensitive_response_for_persistence(canonical)
    )
    assert persistence_safe.provider_metadata is None
    assert persistence_safe.tool_calls[0].provider_metadata is None
    durable_serialized = _serialized(persistence_safe)
    assert call.id in durable_serialized
    for provider_value in (provider_call_id, provider_item_id, provider_signature):
        assert provider_value not in durable_serialized


def test_computer_use_rejects_provider_native_call_id_reuse_across_responses() -> None:
    provider_call_id = "gemini-native-reused-call"

    def response(signature: str) -> LLMResponse:
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id=provider_call_id, name="computer_state", arguments={})],
            raw={},
            provider_metadata={
                GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "id": provider_call_id,
                                    "name": "computer_state",
                                    "args": {},
                                },
                                "thoughtSignature": signature,
                            }
                        ],
                    }
                }
            },
        )

    used_protocol_ids: set[str] = set()
    first = canonicalize_computer_use_response_for_dispatch(
        response("gemini-signature-first"),
        used_protocol_ids=used_protocol_ids,
    )
    assert re.fullmatch(r"alysis_cu_call_[0-9a-f]{40}", first.tool_calls[0].id)
    assert provider_call_id in used_protocol_ids

    with pytest.raises(ValueError, match="malformed Computer Use tool linkage"):
        canonicalize_computer_use_response_for_dispatch(
            response("gemini-signature-second"),
            used_protocol_ids=used_protocol_ids,
        )


def test_computer_use_rejects_unknown_provider_tool_name_before_projection() -> None:
    response = LLMResponse(
        content=f"Trying {_CANARY}",
        tool_calls=[ToolCall(id=_CANARY, name=_CANARY, arguments={"text": _CANARY})],
        raw={"id": _CANARY, "name": _CANARY},
    )

    with pytest.raises(ValueError, match="outside the fixed Computer Use contract"):
        canonicalize_computer_use_response_for_dispatch(response)


def test_computer_use_rejects_padded_signed_gemini_name_before_dispatch() -> None:
    provider_call_id = "gemini-native-padded-name"
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id=provider_call_id,
                name="computer_action",
                arguments={"action": "click", "element_id": "approved-element"},
            )
        ],
        raw={},
        provider_metadata={
            GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "thoughtSignature": "opaque-padded-name-signature",
                            "functionCall": {
                                "id": provider_call_id,
                                "name": " computer_action ",
                                "args": {
                                    "action": "click",
                                    "element_id": "approved-element",
                                },
                            },
                        }
                    ],
                }
            }
        },
    )

    with pytest.raises(ValueError, match="malformed Computer Use tool linkage"):
        canonicalize_computer_use_response_for_dispatch(response)


@pytest.mark.parametrize(
    ("provider_key", "provider_payload"),
    (
        (
            OPENAI_RESPONSES_PROVIDER_METADATA_KEY,
            {
                "response_id": "resp-without-native-call",
                "output_items": [
                    {
                        "type": "reasoning",
                        "id": "reasoning-without-native-call",
                        "encrypted_content": "opaque-reasoning-without-native-call",
                    }
                ],
            },
        ),
        (
            ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY,
            {
                "message_id": "message-without-native-call",
                "content_blocks": [
                    {
                        "type": "thinking",
                        "thinking": "No native tool block was returned.",
                        "signature": "opaque-thinking-without-native-call",
                    }
                ],
            },
        ),
        (
            GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
            {
                "response_id": "gemini-without-native-call",
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "text": "No native function call was returned.",
                            "thoughtSignature": "opaque-gemini-without-native-call",
                        }
                    ],
                },
            },
        ),
    ),
)
def test_computer_use_rejects_public_call_without_matching_native_replay_call(
    provider_key: str,
    provider_payload: dict[str, Any],
) -> None:
    response = LLMResponse(
        content="Observe the approved UI.",
        tool_calls=[ToolCall(id="provider-public-call", name="computer_state", arguments={})],
        raw={},
        provider_metadata={provider_key: provider_payload},
    )

    with pytest.raises(ValueError, match="malformed Computer Use tool linkage"):
        canonicalize_computer_use_response_for_dispatch(response)


def test_computer_use_projects_matched_native_replay_to_required_schema() -> None:
    metadata_canary = "CU_PROVIDER_METADATA_CANARY_81f7d9"
    provider_call_id = "provider-call-must-not-persist"
    provider_item_id = "provider-item-must-not-persist"
    raw_arguments = json.dumps(
        {
            "snapshot_id": "snapshotProjectionPrivate",
            "action": "set_value",
            "element_id": "elementProjectionPrivate",
            "text": _PRIVATE_TYPED_TEXT,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    arguments = json.loads(raw_arguments)
    openai_reasoning_signature = "opaque-openai-reasoning-signature"
    anthropic_reasoning_signature = "opaque-anthropic-reasoning-signature"
    gemini_reasoning_signature = "opaque-gemini-reasoning-signature"
    response = LLMResponse(
        content="Setting the approved field.",
        tool_calls=[
            ToolCall(
                id=provider_call_id,
                name="computer_action",
                arguments=arguments,
                provider_metadata={
                    OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                        "item_id": provider_item_id,
                        "output_index": 2,
                        "status": metadata_canary,
                        metadata_canary: {"provider_extra": metadata_canary},
                    },
                    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                        "part_index": 1,
                        "thoughtSignature": gemini_reasoning_signature,
                        metadata_canary: metadata_canary,
                    },
                    metadata_canary: {"provider_extra": metadata_canary},
                },
            )
        ],
        raw={metadata_canary: metadata_canary, "id": provider_item_id},
        provider_metadata={
            ROUTE_IDENTITY_PROVIDER_METADATA_KEY: {
                "version": 1,
                "fingerprint": "f" * 64,
            },
            OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                "response_id": f"response-{metadata_canary}",
                "status": metadata_canary,
                metadata_canary: {"provider_extra": metadata_canary},
                "output_items": [
                    {
                        "type": "reasoning",
                        "id": "provider-reasoning-id",
                        "status": metadata_canary,
                        "encrypted_content": openai_reasoning_signature,
                        "summary": [],
                        metadata_canary: metadata_canary,
                    },
                    {
                        "type": "message",
                        "id": "provider-message-id",
                        "role": "assistant",
                        "status": metadata_canary,
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Visible assistant preface.",
                                "annotations": [],
                            }
                        ],
                    },
                    {
                        "type": "function_call",
                        "id": provider_item_id,
                        "call_id": provider_call_id,
                        "name": "computer_action",
                        "status": metadata_canary,
                        "arguments": raw_arguments,
                        metadata_canary: metadata_canary,
                    },
                ],
            },
            ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY: {
                "message_id": f"message-{metadata_canary}",
                "stop_reason": metadata_canary,
                "status": metadata_canary,
                metadata_canary: {"provider_extra": metadata_canary},
                "content_blocks": [
                    {
                        "type": "thinking",
                        "thinking": "",
                        "signature": anthropic_reasoning_signature,
                    },
                    {
                        "type": "text",
                        "text": "Visible assistant preface.",
                        "id": f"text-{metadata_canary}",
                    },
                    {
                        "type": "tool_use",
                        "id": provider_call_id,
                        "name": "computer_action",
                        "input": copy.deepcopy(arguments),
                        "status": metadata_canary,
                        metadata_canary: metadata_canary,
                    },
                ],
            },
            GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                "response_id": f"response-{metadata_canary}",
                "status": metadata_canary,
                metadata_canary: {"provider_extra": metadata_canary},
                "content": {
                    "role": "model",
                    "status": metadata_canary,
                    metadata_canary: metadata_canary,
                    "parts": [
                        {
                            "text": "Visible assistant preface.",
                            "thoughtSignature": "opaque-gemini-text-signature",
                        },
                        {
                            "thoughtSignature": gemini_reasoning_signature,
                            "functionCall": {
                                "id": provider_call_id,
                                "name": "computer_action",
                                "args": copy.deepcopy(arguments),
                            },
                        },
                    ],
                },
            },
            metadata_canary: {metadata_canary: metadata_canary},
        },
    )

    canonical = canonicalize_computer_use_response_for_dispatch(response)

    call = canonical.tool_calls[0]
    assert re.fullmatch(r"alysis_cu_call_[0-9a-f]{40}", call.id)
    assert call.arguments is arguments
    assert canonical.raw == {}
    assert metadata_canary not in _serialized(canonical)
    transient_serialized = _serialized(canonical)
    assert provider_call_id in transient_serialized
    assert provider_item_id in transient_serialized
    assert set(canonical.provider_metadata) == {
        ROUTE_IDENTITY_PROVIDER_METADATA_KEY,
        OPENAI_RESPONSES_PROVIDER_METADATA_KEY,
        ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY,
        GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
    }

    openai = canonical.provider_metadata[OPENAI_RESPONSES_PROVIDER_METADATA_KEY]
    assert "response_id" not in openai
    assert "status" not in openai
    assert [item["type"] for item in openai["output_items"]] == [
        "reasoning",
        "message",
        "function_call",
    ]
    openai_reasoning, openai_message, openai_call = openai["output_items"]
    assert openai_reasoning == {
        "type": "reasoning",
        "encrypted_content": openai_reasoning_signature,
        "summary": [],
        "status": "completed",
        "id": "provider-reasoning-id",
    }
    assert openai_message == {
        "type": "message",
        "id": "provider-message-id",
        "role": "assistant",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": "Visible assistant preface.",
                "annotations": [],
            }
        ],
    }
    assert openai_call["status"] == "completed"
    assert openai_call["id"] == provider_item_id
    assert openai_call["call_id"] == provider_call_id
    assert openai_call["name"] == "computer_action"
    assert openai_call["arguments"] == raw_arguments
    assert openai["call_ids_rewritten"] is True

    anthropic = canonical.provider_metadata[ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY]
    assert "message_id" not in anthropic
    assert "status" not in anthropic
    assert [block["type"] for block in anthropic["content_blocks"]] == [
        "thinking",
        "text",
        "tool_use",
    ]
    anthropic_thinking, anthropic_text, anthropic_call = anthropic["content_blocks"]
    assert anthropic_thinking == {
        "type": "thinking",
        "thinking": "",
        "signature": anthropic_reasoning_signature,
    }
    assert anthropic_text == {"type": "text", "text": "Visible assistant preface."}
    assert anthropic_call == {
        "type": "tool_use",
        "id": provider_call_id,
        "name": "computer_action",
        "input": arguments,
    }

    gemini = canonical.provider_metadata[GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY]
    assert "response_id" not in gemini
    assert "status" not in gemini
    assert gemini["content"]["role"] == "model"
    assert len(gemini["content"]["parts"]) == 2
    gemini_text_part, gemini_part = gemini["content"]["parts"]
    assert gemini_text_part == {
        "text": "Visible assistant preface.",
        "thoughtSignature": "opaque-gemini-text-signature",
    }
    assert set(gemini_part) == {"thoughtSignature", "functionCall"}
    assert gemini_part["thoughtSignature"] == gemini_reasoning_signature
    assert gemini_part["functionCall"] == {
        "id": provider_call_id,
        "name": "computer_action",
        "args": arguments,
    }

    assert call.provider_metadata == {
        "_alysis_computer_use_ephemeral_linkage": {
            "provider_call_id": provider_call_id,
        }
    }

    replay_state = computer_use_ephemeral_provider_replay_state(canonical)
    assert replay_state == {
        call.id: {
            "provider_call_id": provider_call_id,
            "provider_metadata": canonical.provider_metadata,
        }
    }

    durable_response = strip_computer_use_provider_state_for_persistence(
        redact_sensitive_response_for_persistence(canonical)
    )
    durable_assistant = redact_assistant_tool_call_message(
        assistant_message_from_response(durable_response)
    )
    durable_tool = {
        "role": "tool",
        "tool_call_id": call.id,
        "name": "computer_action",
        "content": "{}",
    }
    durable_messages = [durable_assistant, durable_tool]
    durable_serialized = _serialized(durable_messages)
    assert PROVIDER_METADATA_KEY not in durable_assistant
    assert durable_assistant["tool_calls"][0]["id"] == call.id
    assert durable_tool["tool_call_id"] == call.id
    for provider_value in (
        provider_call_id,
        provider_item_id,
        "provider-reasoning-id",
        "provider-message-id",
        openai_reasoning_signature,
        anthropic_reasoning_signature,
        gemini_reasoning_signature,
        "opaque-gemini-text-signature",
    ):
        assert provider_value not in durable_serialized

    injected_assistant, injected_tool = inject_ephemeral_computer_use_provider_state(
        durable_messages,
        replay_state,
    )
    assert injected_assistant[PROVIDER_METADATA_KEY] == canonical.provider_metadata
    assert injected_tool["tool_call_id"] == provider_call_id
    # Replay uses a disposable deep copy; the durable source stays canonical and clean.
    assert PROVIDER_METADATA_KEY not in durable_assistant
    assert durable_tool["tool_call_id"] == call.id


def test_final_computer_use_response_drops_all_provider_state_before_persistence(
    tmp_path: Path,
) -> None:
    raw_provider_id = "final-raw-provider-id-must-not-persist"
    openai_response_id = "final-openai-response-id-must-not-persist"
    anthropic_message_id = "final-anthropic-message-id-must-not-persist"
    gemini_response_id = "final-gemini-response-id-must-not-persist"
    generic_provider_id = "final-generic-provider-id-must-not-persist"
    state = {
        "snapshot_id": "snapshotFinalProjection",
        "elements": [{"id": "elementFinalProjection", "role": "AXButton", "value": "READY"}],
    }
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="provider-observe-id", name="computer_state", arguments={})
                ],
                raw={},
            ),
            LLMResponse(
                content="done",
                tool_calls=[],
                raw={"id": raw_provider_id},
                provider_metadata={
                    OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                        "response_id": openai_response_id,
                        "output_items": [
                            {
                                "type": "message",
                                "id": "final-openai-message-id-must-not-persist",
                                "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": "done"}],
                            }
                        ],
                    },
                    ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY: {
                        "message_id": anthropic_message_id,
                        "content_blocks": [{"type": "text", "text": "done"}],
                    },
                    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                        "response_id": gemini_response_id,
                        "content": {"role": "model", "parts": [{"text": "done"}]},
                    },
                    "provider_extra": {"id": generic_provider_id},
                },
            ),
        ],
        host_request=lambda name, arguments: copy.deepcopy(state),
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Observe the approved UI, then report completion.") == 0
        durable_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    final_messages = [
        message
        for message in durable_messages
        if message.get("role") == "assistant"
        and not message.get("tool_calls")
        and message.get("content")
        == ("Computer Use completed the authorized workflow through the trusted host boundary.")
    ]
    assert len(final_messages) == 1
    assert PROVIDER_METADATA_KEY not in final_messages[0]
    assert log_path is not None
    durable_bundle = _computer_use_durable_boundary_bundle(
        client=client,
        surface=surface,
        hooks=hooks,
        diagnostics=diagnostics,
        messages=durable_messages,
        log_path=log_path,
    )
    for provider_value in (
        raw_provider_id,
        openai_response_id,
        "final-openai-message-id-must-not-persist",
        anthropic_message_id,
        gemini_response_id,
        generic_provider_id,
    ):
        assert provider_value not in durable_bundle


def test_agent_session_removes_raw_provider_call_id_from_every_durable_boundary(
    tmp_path: Path,
) -> None:
    state = {
        "snapshot_id": "snapshotOpaqueProviderCall",
        "elements": [{"id": "elementOpaqueProviderCall", "role": "AXButton", "value": "READY"}],
    }
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[
            LLMResponse(
                content=f"Calling {_CANARY}",
                tool_calls=[ToolCall(id=_CANARY, name="computer_state", arguments={})],
                raw={"id": _CANARY},
                provider_metadata={"provider_echo": _CANARY},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ],
        host_request=lambda name, arguments: copy.deepcopy(state),
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Observe the approved control safely.") == 0
        durable_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert log_path is not None
    durable_bundle = _computer_use_durable_boundary_bundle(
        client=client,
        surface=surface,
        hooks=hooks,
        diagnostics=diagnostics,
        messages=durable_messages,
        log_path=log_path,
    )
    assert _CANARY not in durable_bundle
    assistant_call_ids = [
        call["id"]
        for message in durable_messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
    ]
    assert len(assistant_call_ids) == 1
    assert re.fullmatch(r"alysis_cu_call_[0-9a-f]{40}", assistant_call_ids[0])


def test_agent_session_fails_closed_before_unknown_computer_use_linkage_persists(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[
            LLMResponse(
                content=f"Trying {_CANARY}",
                tool_calls=[
                    ToolCall(
                        id=_CANARY,
                        name=_CANARY,
                        arguments={"text": _CANARY},
                    )
                ],
                raw={"id": _CANARY, "name": _CANARY},
                provider_metadata={"provider_echo": _CANARY},
            )
        ],
        host_request=lambda name, arguments: (
            host_calls.append((name, copy.deepcopy(arguments))) or {}
        ),
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        with pytest.raises(
            AgentRuntimeError, match=re.escape(_PROTECTED_FAILURE_MESSAGE)
        ) as raised:
            session.run_turn("Reject a provider call outside the fixed host contract.")
        durable_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert host_calls == []
    assert str(raised.value) == _PROTECTED_FAILURE_MESSAGE
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert _CANARY not in str(raised.value)
    assert any(
        event["event_type"] == "computer_use_provider_contract_failure"
        for event in diagnostics.events
    )
    assert log_path is not None
    durable_bundle = _computer_use_durable_boundary_bundle(
        client=client,
        surface=surface,
        hooks=hooks,
        diagnostics=diagnostics,
        messages=durable_messages,
        log_path=log_path,
    )
    assert _CANARY not in durable_bundle


def _make_computer_use_session(
    tmp_path: Path,
    *,
    responses: list[LLMResponse | BaseException] | None = None,
    host_request: Any,
    reasoning_delta: str | None = None,
    client: Any = None,
    max_steps: int = 5,
) -> tuple[AgentSession, Any]:
    tools = build_computer_use_tools(host_request)
    if client is None:
        client = _ScriptedClient(responses or [], reasoning_delta=reasoning_delta)
    surface = _RecordingSurface()
    hooks = _RecordingHooks()
    diagnostics = _RecordingDiagnostics()
    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path / "sessions",
        session_id="computer-use-private-projection",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    session = AgentSession(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=True),
        root=tmp_path,
        mode="review",
        yes=False,
        stream=True,
        routing_mode="code_only",
        max_steps=max_steps,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,
        store=store,
        client=client,  # type: ignore[arg-type]
        model_registry=_FakeRegistry(),  # type: ignore[arg-type]
        usage_summary=UsageSummary(),
        usage_role="main",
        tool_output_offloader=None,
        conversation_compactor=None,
        tool_output_offload_enabled=False,
        conversation_summarization_enabled=False,
        compaction_profile="chat",
        tools=tools,
        tool_list=[tool.as_openai_tool() for tool in tools.values()],
        messages=[{"role": "system", "content": "Computer Use test system prompt."}],
        hook_dispatcher=hooks,  # type: ignore[arg-type]
        crash_diagnostics=diagnostics,  # type: ignore[arg-type]
        verification_enabled=False,
        skills_enabled=False,
        sensitive_media=True,
    )
    return session, (client, surface, hooks, diagnostics, store)


def _computer_use_durable_boundary_bundle(
    *,
    client: _ScriptedClient,
    surface: _RecordingSurface,
    hooks: _RecordingHooks,
    diagnostics: _RecordingDiagnostics,
    messages: list[dict[str, Any]],
    log_path: Path,
) -> str:
    return _serialized(
        {
            "store": log_path.read_text(encoding="utf-8"),
            "messages": messages,
            "provider_replay": client.request_references,
            "surface": {
                "tool_events": surface.events,
                "legacy_tool_events": surface.legacy_events,
                "message_events": surface.message_events,
                "assistant_messages": surface.assistant_messages,
                "reasoning_events": surface.reasoning_events,
            },
            "hooks": hooks.payloads,
            "diagnostics": diagnostics.events,
        }
    )


def test_gemini_four_request_computer_use_replays_all_older_signed_parts_exactly(
    tmp_path: Path,
) -> None:
    native_parts = [
        {
            "functionCall": {
                "id": "gemini-native-state-1",
                "name": "computer_state",
                "args": {},
            },
            "thoughtSignature": "gemini-signature-state-1",
        },
        {
            "functionCall": {
                "id": "gemini-native-action-1",
                "name": "computer_action",
                "args": {
                    "action": "set_value",
                    "element_id": "private-field-1",
                    "text": "Private typed value 1",
                },
            },
            "thoughtSignature": "gemini-signature-action-1",
        },
        {
            "functionCall": {
                "id": "gemini-native-state-2",
                "name": "computer_state",
                "args": {},
            },
            "thoughtSignature": "gemini-signature-state-2",
        },
    ]
    native_ids = [part["functionCall"]["id"] for part in native_parts]
    native_names = [part["functionCall"]["name"] for part in native_parts]
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 128})
        assert request.url.path.endswith(":generateContent")
        request_index = len(requests)
        assert request_index <= len(native_parts)
        requests.append(json.loads(request.content.decode("utf-8")))
        if request_index == len(native_parts):
            return httpx.Response(
                200,
                json={
                    "responseId": "gemini-response-4",
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": "The approved UI sequence is complete."}],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "responseId": f"gemini-response-{request_index + 1}",
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [copy.deepcopy(native_parts[request_index])],
                        },
                        "finishReason": "STOP",
                    }
                ],
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        transport=httpx.MockTransport(handler),
    )
    host_calls: list[tuple[str, dict[str, Any]]] = []
    state_calls = 0

    def state(snapshot_id: str, result: str) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot_id,
            "elements": [
                {
                    "id": "private-field-1",
                    "role": "AXTextField",
                    "label": "Private field",
                },
                {
                    "id": "private-button-2",
                    "role": "AXButton",
                    "title": "Private button",
                },
                {"id": "private-result", "role": "AXStaticText", "value": result},
            ],
        }

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal state_calls
        host_calls.append((name, copy.deepcopy(arguments)))
        if name == "computer_state":
            state_calls += 1
            return state(f"private-snapshot-state-{state_calls}", "READY")
        if arguments["action"] == "set_value":
            return {
                "receipt": {"action": "set_value"},
                "state": state("private-snapshot-after-action-1", "READY"),
            }
        return {
            "receipt": {"action": "press"},
            "state": state("private-snapshot-after-action-2", "PASS"),
        }

    session, captures = _make_computer_use_session(
        tmp_path,
        host_request=host_request,
        client=client,
        max_steps=4,
    )
    _client, _surface, _hooks, _diagnostics, store = captures
    log_path = store.path
    try:
        # The fourth request is terminal but still replays all three older signed
        # Parts, including the request-two action on the N+2 boundary.
        assert session.run_turn("Run the approved Computer Use sequence.") == 0
        durable_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == [
        "computer_state",
        "computer_action",
        "computer_state",
    ]
    assert host_calls[1][1] == {
        "action": "set_value",
        "element_id": "private-field-1",
        "text": "Private typed value 1",
        "snapshot_id": "private-snapshot-state-1",
    }
    assert len(requests) == 4
    assert "alysis_cu_call_" not in _serialized(requests)

    def signed_call_parts(body: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            part
            for content in body["contents"]
            if content.get("role") == "model"
            for part in content.get("parts", [])
            if isinstance(part.get("functionCall"), dict)
        ]

    def function_responses(body: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            part["functionResponse"]
            for content in body["contents"]
            if content.get("role") == "user"
            for part in content.get("parts", [])
            if isinstance(part.get("functionResponse"), dict)
        ]

    def exact_json_bytes(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    for request_index, request in enumerate(requests):
        expected_older_parts = native_parts[:request_index]
        actual_older_parts = signed_call_parts(request)
        assert [exact_json_bytes(part) for part in actual_older_parts] == [
            exact_json_bytes(part) for part in expected_older_parts
        ]
        assert [part["functionCall"]["args"] for part in actual_older_parts] == [
            part["functionCall"]["args"] for part in expected_older_parts
        ]
        actual_responses = function_responses(request)
        assert [response["id"] for response in actual_responses] == native_ids[:request_index]
        assert [response["name"] for response in actual_responses] == native_names[:request_index]

    durable_assistant_messages = [
        message
        for message in durable_messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    durable_call_ids = [
        call["id"] for message in durable_assistant_messages for call in message["tool_calls"]
    ]
    durable_tool_ids = [
        message["tool_call_id"] for message in durable_messages if message.get("role") == "tool"
    ]
    assert len(durable_call_ids) == 3
    assert len(set(durable_call_ids)) == 3
    assert all(re.fullmatch(r"alysis_cu_call_[0-9a-f]{40}", value) for value in durable_call_ids)
    assert durable_tool_ids == durable_call_ids
    assert all(PROVIDER_METADATA_KEY not in message for message in durable_assistant_messages)

    assert log_path is not None
    durable_bundle = _serialized(
        {"messages": durable_messages, "store": log_path.read_text(encoding="utf-8")}
    )
    for provider_value in (
        *native_ids,
        *(part["thoughtSignature"] for part in native_parts),
        *(f"gemini-response-{index}" for index in range(1, 5)),
        "private-field-1",
        "Private typed value 1",
        "private-snapshot-state-1",
        "private-snapshot-after-action-1",
        "private-snapshot-state-2",
    ):
        assert provider_value not in durable_bundle


def test_gemini_idless_multi_step_computer_use_replays_signed_parts_and_omits_wire_ids(
    tmp_path: Path,
) -> None:
    native_text_part = {
        "text": "",
        "thought": True,
        "thoughtSignature": "gemini-idless-signature-text-context",
        "partMetadata": {"trace": "gemini-idless-part-metadata-text"},
    }
    native_parts = [
        {
            "functionCall": {"name": "computer_state", "args": {}},
            "thoughtSignature": "gemini-idless-signature-state-1",
            "partMetadata": {"trace": "gemini-idless-part-metadata-function"},
        },
        {
            "functionCall": {
                "name": "computer_action",
                "args": {
                    "action": "set_value",
                    "element_id": "idless-private-field",
                    "text": "ID-less private typed value",
                },
            },
            "thoughtSignature": "gemini-idless-signature-action-1",
        },
        {
            "functionCall": {"name": "computer_state", "args": {}},
            "thoughtSignature": "gemini-idless-signature-state-2",
        },
    ]
    native_names = [part["functionCall"]["name"] for part in native_parts]
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 128})
        assert request.url.path.endswith(":generateContent")
        request_index = len(requests)
        assert request_index <= len(native_parts)
        requests.append(json.loads(request.content.decode("utf-8")))
        if request_index == len(native_parts):
            return httpx.Response(
                200,
                json={
                    "responseId": "gemini-idless-response-4",
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": "The ID-less sequence is complete."}],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "responseId": f"gemini-idless-response-{request_index + 1}",
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": (
                                [copy.deepcopy(native_text_part)] if request_index == 0 else []
                            )
                            + [copy.deepcopy(native_parts[request_index])],
                        },
                        "finishReason": "STOP",
                    }
                ],
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        transport=httpx.MockTransport(handler),
    )
    host_calls: list[tuple[str, dict[str, Any]]] = []
    state_calls = 0

    def state(snapshot_id: str) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot_id,
            "elements": [
                {
                    "id": "idless-private-field",
                    "role": "AXTextField",
                    "label": "ID-less private field",
                }
            ],
        }

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal state_calls
        host_calls.append((name, copy.deepcopy(arguments)))
        if name == "computer_state":
            state_calls += 1
            return state(f"idless-private-snapshot-{state_calls}")
        return {
            "receipt": {"action": "set_value"},
            "state": state("idless-private-snapshot-after-action"),
        }

    session, captures = _make_computer_use_session(
        tmp_path,
        host_request=host_request,
        client=client,
        max_steps=4,
    )
    _client, _surface, _hooks, _diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Run the approved ID-less Gemini sequence.") == 0
        durable_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == [
        "computer_state",
        "computer_action",
        "computer_state",
    ]
    assert host_calls[1][1] == {
        "action": "set_value",
        "element_id": "idless-private-field",
        "text": "ID-less private typed value",
        "snapshot_id": "idless-private-snapshot-1",
    }
    assert len(requests) == 4

    def signed_call_parts(body: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            part
            for content in body["contents"]
            if content.get("role") == "model"
            for part in content.get("parts", [])
            if isinstance(part.get("functionCall"), dict)
        ]

    def function_responses(body: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            part["functionResponse"]
            for content in body["contents"]
            if content.get("role") == "user"
            for part in content.get("parts", [])
            if isinstance(part.get("functionResponse"), dict)
        ]

    def metadata_text_parts(body: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            part
            for content in body["contents"]
            if content.get("role") == "model"
            for part in content.get("parts", [])
            if "partMetadata" in part and not isinstance(part.get("functionCall"), dict)
        ]

    def exact_json_bytes(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    for request_index, request in enumerate(requests):
        expected_older_parts = native_parts[:request_index]
        actual_older_parts = signed_call_parts(request)
        assert [exact_json_bytes(part) for part in actual_older_parts] == [
            exact_json_bytes(part) for part in expected_older_parts
        ]
        assert [exact_json_bytes(part) for part in metadata_text_parts(request)] == (
            [] if request_index == 0 else [exact_json_bytes(native_text_part)]
        )
        assert all("id" not in part["functionCall"] for part in actual_older_parts)
        actual_responses = function_responses(request)
        assert [response["name"] for response in actual_responses] == native_names[:request_index]
        assert all("id" not in response for response in actual_responses)

    serialized_wire = _serialized(requests)
    assert "alysis_cu_call_" not in serialized_wire
    assert "alysis_gemini_idless_" not in serialized_wire

    durable_assistant_messages = [
        message
        for message in durable_messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    durable_call_ids = [
        call["id"] for message in durable_assistant_messages for call in message["tool_calls"]
    ]
    durable_tool_ids = [
        message["tool_call_id"] for message in durable_messages if message.get("role") == "tool"
    ]
    assert len(durable_call_ids) == 3
    assert len(set(durable_call_ids)) == 3
    assert all(re.fullmatch(r"alysis_cu_call_[0-9a-f]{40}", value) for value in durable_call_ids)
    assert durable_tool_ids == durable_call_ids
    assert all(PROVIDER_METADATA_KEY not in message for message in durable_assistant_messages)

    assert log_path is not None
    durable_bundle = _serialized(
        {"messages": durable_messages, "store": log_path.read_text(encoding="utf-8")}
    )
    assert "alysis_gemini_idless_" not in durable_bundle
    assert "partMetadata" not in durable_bundle
    for private_value in (
        *(part["thoughtSignature"] for part in native_parts),
        native_text_part["thoughtSignature"],
        native_text_part["partMetadata"]["trace"],
        native_parts[0]["partMetadata"]["trace"],
        *(f"gemini-idless-response-{index}" for index in range(1, 5)),
        "idless-private-field",
        "ID-less private typed value",
        "idless-private-snapshot-1",
        "idless-private-snapshot-after-action",
        "idless-private-snapshot-2",
    ):
        assert private_value not in durable_bundle


def test_computer_use_zero_free_ids_are_private_but_live_dispatch_is_exact(
    tmp_path: Path,
) -> None:
    provider_snapshot = "snapshotProviderZulu"
    trusted_snapshot = "snapshotTrustedAlpha"
    post_action_snapshot = "snapshotTrustedBravo"
    semantic_element = "semanticElementCedar"
    post_action_element = "semanticElementWillow"
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def state(snapshot_id: str, element_id: str) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot_id,
            "elements": [
                {
                    "id": element_id,
                    "role": "AXTextField",
                    "value": "READY",
                }
            ],
        }

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, copy.deepcopy(arguments)))
        if name == "computer_state":
            return state(trusted_snapshot, semantic_element)
        assert name == "computer_action"
        return {
            "receipt": {
                "action": arguments["action"],
                "previous_snapshot_id": trusted_snapshot,
            },
            "state": state(post_action_snapshot, post_action_element),
        }

    action_arguments = {
        "snapshot_id": provider_snapshot,
        "action": "set_value",
        "element_id": semantic_element,
        "text": _PRIVATE_TYPED_TEXT,
    }
    serialized_action_arguments = json.dumps(
        action_arguments,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    final_echo = (
        f"Finished with {provider_snapshot}, {trusted_snapshot}, "
        f"{post_action_snapshot}, {semantic_element}, {post_action_element}, "
        f"and {_PRIVATE_TYPED_TEXT}."
    )
    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="state-call", name="computer_state", arguments={})],
            raw={},
        ),
        LLMResponse(
            content=f"Acting on {provider_snapshot} and {semantic_element}.",
            tool_calls=[
                ToolCall(
                    id="action-call",
                    name="computer_action",
                    arguments=copy.deepcopy(action_arguments),
                    provider_metadata={"opaque_echo": _PRIVATE_TYPED_TEXT},
                )
            ],
            raw={"provider_arguments": serialized_action_arguments},
            provider_metadata={
                OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                    "response_id": "responseActionPrivate",
                    "output_items": [
                        {
                            "type": "function_call",
                            "id": "functionActionPrivate",
                            "call_id": "action-call",
                            "name": "computer_action",
                            "arguments": serialized_action_arguments,
                        },
                    ],
                }
            },
            reasoning=(
                ReasoningOutput(
                    text=f"Reasoned over {_PRIVATE_TYPED_TEXT}.",
                    kind=ReasoningOutputKind.SUMMARY,
                    provider="test",
                ),
            ),
        ),
        LLMResponse(
            content=final_echo,
            tool_calls=[],
            raw={"provider_echo": final_echo},
            provider_metadata={"provider_echo": final_echo},
        ),
    ]
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=responses,
        host_request=host_request,
        reasoning_delta=f"Reasoned over {provider_snapshot} and {semantic_element}.",
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Use the approved field.") == 0
        messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert host_calls == [
        ("computer_state", {}),
        (
            "computer_action",
            {
                "action": "set_value",
                "element_id": semantic_element,
                "text": _PRIVATE_TYPED_TEXT,
                "snapshot_id": trusted_snapshot,
            },
        ),
    ]
    assert provider_snapshot not in _serialized(host_calls)

    # The provider gets exact transient state and call data for one request.
    assert trusted_snapshot in client.request_snapshots[1]
    assert semantic_element in client.request_snapshots[1]
    assert provider_snapshot in client.request_snapshots[2]
    assert trusted_snapshot in client.request_snapshots[2]
    assert post_action_snapshot in client.request_snapshots[2]
    assert semantic_element in client.request_snapshots[2]
    assert post_action_element in client.request_snapshots[2]
    assert _PRIVATE_TYPED_TEXT in client.request_snapshots[2]
    assert client.stream_flags == [False, False, False]
    assert client.reasoning_callback_flags == [False, False, False]
    assert surface.reasoning_events == []
    private_ids = (
        provider_snapshot,
        trusted_snapshot,
        post_action_snapshot,
        semantic_element,
        post_action_element,
    )
    assert all("0" not in private_id for private_id in private_ids)
    action_response = responses[1]
    assert isinstance(action_response, LLMResponse)
    assert action_response.tool_calls[0].arguments == action_arguments
    assert action_response.raw == {"provider_arguments": serialized_action_arguments}
    assert (
        action_response.provider_metadata[OPENAI_RESPONSES_PROVIDER_METADATA_KEY]["output_items"][
            0
        ]["arguments"]
        == serialized_action_arguments
    )

    assert log_path is not None
    durable_bundle = _computer_use_durable_boundary_bundle(
        client=client,
        surface=surface,
        hooks=hooks,
        diagnostics=diagnostics,
        messages=messages,
        log_path=log_path,
    )
    for private_value in (
        provider_snapshot,
        trusted_snapshot,
        post_action_snapshot,
        semantic_element,
        post_action_element,
        _PRIVATE_TYPED_TEXT,
    ):
        assert private_value not in durable_bundle
    assert "[redacted: transient computer use identifier]" in durable_bundle
    assert (
        "Computer Use completed the authorized workflow through the trusted host boundary."
        in durable_bundle
    )


def test_short_ui_taint_preserves_openai_responses_tool_pairing_and_route(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []
    state_call_id = "call_state_0"
    action_call_id = "call_action_0"
    trusted_snapshot = "trusted_snapshot_0"
    semantic_element = "field_0"
    typed_value = "typed-value-private"
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def function_call_response(
        *, response_id: str, item_id: str, call_id: str, name: str, arguments: str
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": response_id,
                "model": "computer-use-short-taint-model",
                "output": [
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                        "status": "completed",
                    }
                ],
            },
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/responses/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 32})
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        replay_call_ids = [
            item["call_id"] for item in body["input"] if item.get("type") == "function_call"
        ]
        replay_output_ids = [
            item["call_id"] for item in body["input"] if item.get("type") == "function_call_output"
        ]
        if replay_call_ids != replay_output_ids:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "No tool output found for function call "
                            f"{next(iter(set(replay_call_ids) - set(replay_output_ids)), '')}"
                        )
                    }
                },
            )
        if len(requests) == 1:
            return function_call_response(
                response_id="resp_state_0",
                item_id="fc_state_0",
                call_id=state_call_id,
                name="computer_state",
                arguments="{}",
            )
        if len(requests) == 2:
            return function_call_response(
                response_id="resp_action_0",
                item_id="fc_action_0",
                call_id=action_call_id,
                name="computer_action",
                arguments=json.dumps(
                    {
                        "snapshot_id": "provider_snapshot_0",
                        "action": "set_value",
                        "element_id": semantic_element,
                        "text": typed_value,
                    },
                    separators=(",", ":"),
                ),
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_done_0",
                "model": "computer-use-short-taint-model",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_done_0",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "done",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="computer-use-short-taint-model",
        transport=httpx.MockTransport(handler),
    )
    require_tool_calling(client, ("computer_state", "computer_action", "computer_wait"))
    require_serial_tool_calls(client)
    require_stateless_responses(client)

    def state(snapshot_id: str, element_id: str, value: str) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot_id,
            "elements": [{"id": element_id, "role": "AXTextField", "value": value}],
        }

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, copy.deepcopy(arguments)))
        if name == "computer_state":
            return state(trusted_snapshot, semantic_element, "0")
        assert name == "computer_action"
        return {
            "receipt": {
                "action": arguments["action"],
                "previous_snapshot_id": trusted_snapshot,
            },
            "state": state("post_action_snapshot_0", semantic_element, typed_value),
        }

    session, captures = _make_computer_use_session(
        tmp_path,
        host_request=host_request,
        client=client,
    )
    _client, _surface, _hooks, _diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Use the approved field through the complete tool loop.") == 0
        durable_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == [
        "computer_state",
        "computer_action",
    ], {
        "request_count": len(requests),
        "request_call_ids": [
            [
                item.get("call_id")
                for item in request["input"]
                if item.get("type") in {"function_call", "function_call_output"}
            ]
            for request in requests
        ],
    }
    assert host_calls[1][1] == {
        "snapshot_id": trusted_snapshot,
        "action": "set_value",
        "element_id": semantic_element,
        "text": typed_value,
    }
    assert len(requests) == 3
    for request in requests:
        assert request["store"] is False
        assert "previous_response_id" not in request
    final_call_ids = [
        item["call_id"] for item in requests[-1]["input"] if item.get("type") == "function_call"
    ]
    final_output_ids = [
        item["call_id"]
        for item in requests[-1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert len(final_call_ids) == 2
    assert len(set(final_call_ids)) == 2
    assert final_call_ids == [state_call_id, action_call_id]
    assert final_output_ids == final_call_ids

    assistant_messages = [
        message for message in durable_messages if message.get("role") == "assistant"
    ]
    durable_call_ids = [
        message["tool_calls"][0]["id"]
        for message in assistant_messages
        if message.get("tool_calls")
    ]
    assert len(durable_call_ids) == 2
    assert len(set(durable_call_ids)) == 2
    assert all(re.fullmatch(r"alysis_cu_call_[0-9a-f]{40}", value) for value in durable_call_ids)
    assert set(durable_call_ids).isdisjoint(final_call_ids)
    durable_tool_ids = [
        message["tool_call_id"] for message in durable_messages if message.get("role") == "tool"
    ]
    assert durable_tool_ids == durable_call_ids
    tool_assistant_messages = [
        message for message in assistant_messages if message.get("tool_calls")
    ]
    assert all(PROVIDER_METADATA_KEY not in message for message in tool_assistant_messages)
    assert log_path is not None
    durable_bundle = _serialized(
        {
            "messages": durable_messages,
            "store": log_path.read_text(encoding="utf-8"),
        }
    )
    for private_value in (
        trusted_snapshot,
        "provider_snapshot_0",
        "post_action_snapshot_0",
        semantic_element,
        typed_value,
        state_call_id,
        action_call_id,
        "fc_state_0",
        "fc_action_0",
        "resp_state_0",
        "resp_action_0",
    ):
        assert private_value not in durable_bundle
    assert "[redacted: transient computer use identifier]" in durable_bundle
    assert "[redacted: sensitive file content]" in durable_bundle


def test_sensitive_openai_transport_failure_ends_exception_and_traceback_lifetime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_canary = "snapshotExceptionLifetimeMaple"
    element_canary = "elementExceptionLifetimeWillow"
    window_canary = "windowExceptionLifetimeCedar"
    header_canary = "headerExceptionLifetimeBirch"
    url_user_canary = "urlUserExceptionLifetimeAsh"
    url_password_canary = "urlPasswordExceptionLifetimeElm"
    url_path_canary = "urlPathExceptionLifetimePine"
    url_query_canary = "urlQueryExceptionLifetimeOak"
    url_fragment_canary = "urlFragmentExceptionLifetimeFir"
    response_canary = "responseExceptionLifetimeSpruce"
    nested_canary = "nestedExceptionLifetimeBeech"
    unicode_canary = "unicodeExceptionLifetimeHemlock"
    provider_requests: list[httpx.Request] = []
    provider_request_bodies: list[str] = []
    retained_requests: list[httpx.Request] = []
    retained_responses: list[httpx.Response] = []
    retained_transport_errors: list[httpx.DecodingError] = []
    retained_unicode_errors: list[UnicodeDecodeError] = []
    retained_original_chain: list[BaseException] = []
    retained_tracebacks: list[Any] = []

    real_scrub = agent_session_module._scrub_sensitive_exception_chain

    def capture_then_scrub(error: BaseException) -> None:
        pending = [error]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            retained_original_chain.append(current)
            current_traceback = current.__traceback__
            while current_traceback is not None:
                retained_tracebacks.append(current_traceback)
                current_traceback = current_traceback.tb_next
            if isinstance(current.__cause__, BaseException):
                pending.append(current.__cause__)
            if isinstance(current.__context__, BaseException):
                pending.append(current.__context__)
        real_scrub(error)

    monkeypatch.setattr(
        agent_session_module,
        "_scrub_sensitive_exception_chain",
        capture_then_scrub,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/responses/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 32})
        provider_requests.append(request)
        body = request.content.decode("utf-8")
        provider_request_bodies.append(body)
        if len(provider_requests) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp-exception-state",
                    "model": "computer-use-exception-lifetime-model",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc-exception-state",
                            "call_id": "call-exception-state",
                            "name": "computer_state",
                            "arguments": "{}",
                            "status": "completed",
                        }
                    ],
                },
            )

        assert snapshot_canary in body
        assert element_canary in body
        assert window_canary in body
        request.url = httpx.URL(
            f"https://{url_user_canary}:{url_password_canary}@api.openai.test/"
            f"{url_path_canary}?token={url_query_canary}#{url_fragment_canary}"
        )
        request.headers["x-protected-canary"] = header_canary
        request.extensions["protected_canary"] = header_canary
        response = httpx.Response(
            599,
            content=f"provider response retained {response_canary}",
            headers={"x-provider-secret": response_canary},
            extensions={"provider_secret": response_canary},
            request=request,
        )
        retained_requests.append(request)
        retained_responses.append(response)
        invalid_body = f'{{"private":"{unicode_canary}"}}'.encode() + b"\xff"
        unicode_error = UnicodeDecodeError(
            "utf-8",
            invalid_body,
            len(invalid_body) - 1,
            len(invalid_body),
            f"provider response reason {unicode_canary}",
        )
        retained_unicode_errors.append(unicode_error)
        try:
            raise unicode_error
        except UnicodeDecodeError as decode_error:
            try:
                raise RuntimeError(f"nested provider failure {nested_canary}") from decode_error
            except RuntimeError as nested_error:
                decoding_error = httpx.DecodingError(
                    f"response decompression exposed {snapshot_canary}",
                    request=request,
                )
                decoding_error.response = response  # type: ignore[attr-defined]
                retained_transport_errors.append(decoding_error)
                raise decoding_error from nested_error

    client = OpenAIResponsesClient(
        base_url="https://api.openai.test/v1",
        api_key="test-key",
        model="computer-use-exception-lifetime-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=0),
    )
    require_tool_calling(client, ("computer_state", "computer_action", "computer_wait"))
    require_serial_tool_calls(client)
    require_stateless_responses(client)

    host_state = {
        "snapshot_id": snapshot_canary,
        "window": {"title": window_canary},
        "elements": [
            {
                "id": element_canary,
                "role": "AXTextField",
                "title": window_canary,
                "value": "READY",
            }
        ],
    }
    session, captures = _make_computer_use_session(
        tmp_path,
        host_request=lambda name, arguments: copy.deepcopy(host_state),
        client=client,
    )
    _client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        outward, outward_trace = _capture_protected_turn_failure(session)
        durable_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert type(outward) is AgentRuntimeError
    assert outward is not retained_original_chain[0]
    assert outward.args == (_PROTECTED_FAILURE_MESSAGE,)
    diagnostic = outward._alysis_computer_use_provider_diagnostic
    assert diagnostic.error_class == "LLMError"
    assert diagnostic.category == "provider_unavailable"
    assert diagnostic.status == 599
    assert diagnostic.code == diagnostic.parameter == "none"
    assert outward.__cause__ is None
    assert outward.__context__ is None
    assert retained_transport_errors
    assert len(provider_requests) == 2
    assert snapshot_canary in provider_request_bodies[1]

    outward_frame_names = [frame.name for frame in outward_trace.stack]
    assert outward_frame_names[-1] == "run_turn"
    assert not {
        "_run_turn",
        "_main_agent_chat",
        "chat",
        "_send_request",
        "handler",
    }.intersection(outward_frame_names)
    outward_trace_bundle = "".join(outward_trace.format(chain=True)) + _serialized(
        [frame.locals for frame in outward_trace.stack]
    )

    all_canaries = {
        snapshot_canary,
        element_canary,
        window_canary,
        header_canary,
        url_user_canary,
        url_password_canary,
        url_path_canary,
        url_query_canary,
        url_fragment_canary,
        response_canary,
        nested_canary,
        unicode_canary,
    }
    for canary in all_canaries:
        assert canary not in outward_trace_bundle

    assert len(retained_original_chain) >= 3
    for original_error in retained_original_chain:
        assert original_error is not outward
        assert original_error.args == (_SCRUBBED_FAILURE_MESSAGE,)
        assert original_error.__traceback__ is None
        assert original_error.__cause__ is None
        assert original_error.__context__ is None
    for retained_traceback in retained_tracebacks:
        retained_locals = _serialized(retained_traceback.tb_frame.f_locals)
        for canary in all_canaries:
            assert canary not in retained_locals

    assert len(retained_requests) == 1
    retained_request = retained_requests[0]
    assert retained_request.content == b""
    assert dict(retained_request.headers) == {}
    assert retained_request.extensions == {}
    assert retained_request.url.username == ""
    assert retained_request.url.password == ""
    assert retained_request.url.path == "/"
    assert retained_request.url.query == b""
    assert retained_request.url.fragment == ""
    assert getattr(retained_request.stream, "_stream", None) == b""

    assert len(retained_responses) == 1
    retained_response = retained_responses[0]
    assert retained_response.content == b""
    assert dict(retained_response.headers) == {}
    assert retained_response.extensions == {}
    assert getattr(retained_response.stream, "_stream", None) == b""
    assert len(retained_unicode_errors) == 1
    retained_unicode_error = retained_unicode_errors[0]
    assert retained_unicode_error.object == b""
    assert retained_unicode_error.encoding == "utf-8"
    assert retained_unicode_error.start == 0
    assert retained_unicode_error.end == 0
    assert retained_unicode_error.reason == _SCRUBBED_FAILURE_MESSAGE

    terminal_events = [
        event for event in diagnostics.events if event["event_type"] == "terminal_error"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["payload"]["error_type"] == "AgentRuntimeError"
    assert terminal_events[0]["payload"]["error_summary"] == _PROTECTED_FAILURE_MESSAGE
    terminal_bundle = _serialized(terminal_events)
    assert "DecodingError" not in terminal_bundle
    assert "LLMError" not in terminal_bundle
    assert log_path is not None
    durable_bundle = _serialized(
        {
            "store": log_path.read_text(encoding="utf-8"),
            "messages": durable_messages,
            "surface": {
                "tool_events": surface.events,
                "legacy_tool_events": surface.legacy_events,
                "message_events": surface.message_events,
                "assistant_messages": surface.assistant_messages,
                "reasoning_events": surface.reasoning_events,
            },
            "hooks": hooks.payloads,
            "diagnostics": diagnostics.events,
        }
    )
    assert _PROTECTED_FAILURE_MESSAGE in durable_bundle
    for canary in all_canaries:
        assert canary not in terminal_bundle
        assert canary not in durable_bundle


def test_sensitive_turn_never_starts_optional_provider_input_token_count(
    tmp_path: Path,
) -> None:
    count_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/responses/input_tokens"):
            count_requests.append(request)
            raise AssertionError("protected usage accounting started a second provider request")
        return httpx.Response(
            200,
            json={
                "id": "resp-sensitive-no-usage",
                "model": "computer-use-no-usage-model",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "The protected workflow is complete.",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.test/v1",
        api_key="test-key",
        model="computer-use-no-usage-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=0),
    )
    require_tool_calling(client, ("computer_state", "computer_action", "computer_wait"))
    require_serial_tool_calls(client)
    require_stateless_responses(client)
    session, _captures = _make_computer_use_session(
        tmp_path,
        host_request=lambda name, arguments: {},
        client=client,
    )
    try:
        assert session.run_turn("Complete the protected workflow.") == 1
    finally:
        session.close()

    assert count_requests == []


def test_sensitive_keyboard_interrupt_preserves_control_flow_and_scrubs_original_locals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_canary = "snapshotInterruptLifetimeMaple"
    element_canary = "elementInterruptLifetimeWillow"
    interrupt_canary = "interruptLifetimeCedar"
    original_interrupt = KeyboardInterrupt(f"provider interrupt exposed {interrupt_canary}")
    retained_tracebacks: list[Any] = []
    real_scrub = agent_session_module._scrub_sensitive_exception_chain

    def capture_then_scrub(error: BaseException) -> None:
        current_traceback = error.__traceback__
        while current_traceback is not None:
            retained_tracebacks.append(current_traceback)
            current_traceback = current_traceback.tb_next
        real_scrub(error)

    monkeypatch.setattr(
        agent_session_module,
        "_scrub_sensitive_exception_chain",
        capture_then_scrub,
    )

    host_state = {
        "snapshot_id": snapshot_canary,
        "elements": [
            {
                "id": element_canary,
                "role": "AXButton",
                "title": "Continue",
            }
        ],
    }
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="interrupt-state-call", name="computer_state", arguments={})
                ],
                raw={},
            ),
            original_interrupt,
        ],
        host_request=lambda name, arguments: copy.deepcopy(host_state),
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        outward, outward_trace = _capture_protected_turn_failure(session)
        durable_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert type(outward) is KeyboardInterrupt
    assert outward is not original_interrupt
    assert outward.args == ()
    assert outward.__cause__ is None
    assert outward.__context__ is None
    assert original_interrupt.args == (_SCRUBBED_FAILURE_MESSAGE,)
    assert original_interrupt.__traceback__ is None
    assert original_interrupt.__cause__ is None
    assert original_interrupt.__context__ is None
    assert retained_tracebacks

    all_canaries = {snapshot_canary, element_canary, interrupt_canary}
    outward_trace_bundle = "".join(outward_trace.format(chain=True)) + _serialized(
        [frame.locals for frame in outward_trace.stack]
    )
    outward_frame_names = [frame.name for frame in outward_trace.stack]
    assert outward_frame_names[-1] == "run_turn"
    assert "_run_turn" not in outward_frame_names
    assert "chat" not in outward_frame_names
    for canary in all_canaries:
        assert canary not in outward_trace_bundle
    for retained_traceback in retained_tracebacks:
        retained_locals = _serialized(retained_traceback.tb_frame.f_locals)
        for canary in all_canaries:
            assert canary not in retained_locals

    assert client.calls == 2
    assert snapshot_canary in client.request_snapshots[1]
    assert element_canary in client.request_snapshots[1]
    assert snapshot_canary not in _serialized(client.request_references)
    assert element_canary not in _serialized(client.request_references)
    terminal_events = [
        event for event in diagnostics.events if event["event_type"] == "terminal_error"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["payload"]["error_type"] == "KeyboardInterrupt"
    terminal_bundle = _serialized(terminal_events)
    for canary in all_canaries:
        assert canary not in terminal_bundle

    assert log_path is not None
    durable_bundle = _computer_use_durable_boundary_bundle(
        client=client,
        surface=surface,
        hooks=hooks,
        diagnostics=diagnostics,
        messages=durable_messages,
        log_path=log_path,
    )
    for canary in all_canaries:
        assert canary not in durable_bundle


def test_sensitive_exception_scrubber_zeroizes_retained_system_exit_code() -> None:
    canary = "systemExitCodeLifetimeSequoia"
    original = SystemExit({"private": canary})

    agent_session_module._scrub_sensitive_exception_chain(original)

    assert original.code == 1
    assert original.args == (_SCRUBBED_FAILURE_MESSAGE,)
    assert original.__traceback__ is None
    assert original.__cause__ is None
    assert original.__context__ is None
    assert canary not in _serialized(original)


def test_sensitive_primary_failure_cannot_swallow_cleanup_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlottedCleanupInterrupt(KeyboardInterrupt):
        __slots__ = ("payload",)

        def __init__(self, payload: str) -> None:
            super().__init__(payload)
            self.payload = payload

    primary = RuntimeError("primaryFailureLifetimeRedwood")
    cleanup_canary = "cleanupInterruptLifetimeAlder"
    cleanup_interrupt = SlottedCleanupInterrupt(cleanup_canary)

    def fail_cleanup(_session: AgentSession) -> None:
        raise cleanup_interrupt

    monkeypatch.setattr(agent_session_module, "scrub_ephemeral_host_images", fail_cleanup)
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[primary],
        host_request=lambda name, arguments: {},
    )
    _client, _surface, _hooks, diagnostics, _store = captures
    try:
        outward, outward_trace = _capture_protected_turn_failure(session)
    finally:
        session.close()

    assert type(outward) is KeyboardInterrupt
    assert outward.args == ()
    assert primary.args == (_SCRUBBED_FAILURE_MESSAGE,)
    assert cleanup_interrupt.args == (_SCRUBBED_FAILURE_MESSAGE,)
    assert cleanup_interrupt.payload is None
    assert cleanup_canary not in _serialized([frame.locals for frame in outward_trace.stack])
    terminal_events = [
        event for event in diagnostics.events if event["event_type"] == "terminal_error"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["payload"]["error_type"] == "KeyboardInterrupt"


def test_sensitive_earlier_cleanup_error_cannot_swallow_later_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_error = RuntimeError("resetCleanupLifetimeLarch")
    cleanup_interrupt = KeyboardInterrupt("laterCleanupInterruptLifetimeAspen")
    reset_calls = 0

    def fail_final_reset(_tools: dict[str, ToolDef]) -> None:
        nonlocal reset_calls
        reset_calls += 1
        if reset_calls > 1:
            raise reset_error

    def fail_image_cleanup(_session: AgentSession) -> None:
        raise cleanup_interrupt

    monkeypatch.setattr(
        agent_session_module,
        "reset_computer_use_snapshot_binding",
        fail_final_reset,
    )
    monkeypatch.setattr(
        agent_session_module,
        "scrub_ephemeral_host_images",
        fail_image_cleanup,
    )
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[LLMResponse(content="Finished.", tool_calls=[], raw={})],
        host_request=lambda name, arguments: {},
    )
    _client, _surface, _hooks, diagnostics, _store = captures
    try:
        outward, _outward_trace = _capture_protected_turn_failure(session)
    finally:
        session.close()

    assert type(outward) is KeyboardInterrupt
    assert outward.args == ()
    assert reset_error.args == (_SCRUBBED_FAILURE_MESSAGE,)
    assert cleanup_interrupt.args == (_SCRUBBED_FAILURE_MESSAGE,)
    terminal_events = [
        event for event in diagnostics.events if event["event_type"] == "terminal_error"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["payload"]["error_type"] == "KeyboardInterrupt"


def test_sensitive_cleanup_failure_is_replaced_and_transport_state_is_scrubbed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_canary = "snapshotCleanupLifetimeMaple"
    element_canary = "elementCleanupLifetimeWillow"
    cleanup_canary = "cleanupLifetimeCedar"
    header_canary = "cleanupHeaderLifetimeBirch"
    query_canary = "cleanupQueryLifetimeOak"
    cleanup_request = httpx.Request(
        "POST",
        f"https://cleanup.test/private?token={query_canary}",
        headers={"x-cleanup-secret": header_canary},
        content=f"cleanup request retained {snapshot_canary}",
        extensions={"cleanup_secret": cleanup_canary},
    )
    cleanup_response = httpx.Response(
        599,
        content=f"cleanup response retained {element_canary}",
        headers={"x-cleanup-response-secret": cleanup_canary},
        extensions={"cleanup_secret": cleanup_canary},
        request=cleanup_request,
    )
    cleanup_error = RuntimeError(f"protected cleanup failed {cleanup_canary}")
    cleanup_error.request = cleanup_request  # type: ignore[attr-defined]
    cleanup_error.response = cleanup_response  # type: ignore[attr-defined]
    retained_tracebacks: list[Any] = []
    real_scrub = agent_session_module._scrub_sensitive_exception_chain

    def capture_then_scrub(error: BaseException) -> None:
        current_traceback = error.__traceback__
        while current_traceback is not None:
            retained_tracebacks.append(current_traceback)
            current_traceback = current_traceback.tb_next
        real_scrub(error)

    def fail_cleanup(_session: AgentSession) -> None:
        raise cleanup_error

    monkeypatch.setattr(
        agent_session_module,
        "_scrub_sensitive_exception_chain",
        capture_then_scrub,
    )
    monkeypatch.setattr(agent_session_module, "scrub_ephemeral_host_images", fail_cleanup)

    host_state = {
        "snapshot_id": snapshot_canary,
        "elements": [{"id": element_canary, "role": "AXButton", "title": "Finish"}],
    }
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="cleanup-state-call", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(content="The approved workflow is complete.", tool_calls=[], raw={}),
        ],
        host_request=lambda name, arguments: copy.deepcopy(host_state),
    )
    _client, _surface, _hooks, diagnostics, store = captures
    log_path = store.path
    try:
        outward, outward_trace = _capture_protected_turn_failure(session)
    finally:
        session.close()

    assert type(outward) is AgentRuntimeError
    assert outward.args == (_PROTECTED_FAILURE_MESSAGE,)
    assert outward.__cause__ is None
    assert outward.__context__ is None
    assert cleanup_error.args == (_SCRUBBED_FAILURE_MESSAGE,)
    assert cleanup_error.__traceback__ is None
    assert cleanup_error.__cause__ is None
    assert cleanup_error.__context__ is None
    assert retained_tracebacks

    all_canaries = {
        snapshot_canary,
        element_canary,
        cleanup_canary,
        header_canary,
        query_canary,
    }
    outward_trace_bundle = "".join(outward_trace.format(chain=True)) + _serialized(
        [frame.locals for frame in outward_trace.stack]
    )
    for canary in all_canaries:
        assert canary not in outward_trace_bundle
    for retained_traceback in retained_tracebacks:
        retained_locals = _serialized(retained_traceback.tb_frame.f_locals)
        for canary in all_canaries:
            assert canary not in retained_locals

    assert cleanup_request.content == b""
    assert dict(cleanup_request.headers) == {}
    assert cleanup_request.extensions == {}
    assert cleanup_request.url.path == "/"
    assert cleanup_request.url.query == b""
    assert getattr(cleanup_request.stream, "_stream", None) == b""
    assert cleanup_response.content == b""
    assert dict(cleanup_response.headers) == {}
    assert cleanup_response.extensions == {}
    assert getattr(cleanup_response.stream, "_stream", None) == b""

    terminal_events = [
        event for event in diagnostics.events if event["event_type"] == "terminal_error"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["payload"]["error_type"] == "AgentRuntimeError"
    assert terminal_events[0]["payload"]["error_summary"] == _PROTECTED_FAILURE_MESSAGE
    terminal_bundle = _serialized(terminal_events)
    assert log_path is not None
    durable_log = log_path.read_text(encoding="utf-8")
    for canary in all_canaries:
        assert canary not in terminal_bundle
        assert canary not in durable_log


def test_observed_zero_free_semantic_id_and_snapshot_cannot_leak_in_final_prose(
    tmp_path: Path,
) -> None:
    observed_snapshot = "snapshotObservedMaple"
    observed_element = "semanticElementWillow"
    unrelated_id = "providerMessageIdentity"

    state = {
        "id": unrelated_id,
        "snapshot_id": observed_snapshot,
        "elements": [{"id": observed_element, "role": "AXButton", "value": "READY"}],
    }
    private_identifiers = collect_computer_use_private_identifiers(
        "computer_state",
        {},
        state,
    )
    assert observed_snapshot in private_identifiers
    assert observed_element in private_identifiers
    assert unrelated_id not in private_identifiers

    final_echo = f"Observed {observed_snapshot} and {observed_element}."
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="observe-call", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content=final_echo,
                tool_calls=[],
                raw={"provider_echo": final_echo},
                provider_metadata={"provider_echo": final_echo},
            ),
        ],
        host_request=lambda name, arguments: copy.deepcopy(state),
        reasoning_delta=f"Reasoned over {observed_snapshot} and {observed_element}.",
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Observe the approved control.") == 0
        messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert observed_snapshot in client.request_snapshots[1]
    assert observed_element in client.request_snapshots[1]
    assert client.stream_flags == [False, False]
    assert client.reasoning_callback_flags == [False, False]
    assert surface.reasoning_events == []
    assert log_path is not None
    durable_bundle = _computer_use_durable_boundary_bundle(
        client=client,
        surface=surface,
        hooks=hooks,
        diagnostics=diagnostics,
        messages=messages,
        log_path=log_path,
    )
    assert observed_snapshot not in durable_bundle
    assert observed_element not in durable_bundle
    assert (
        "Computer Use completed the authorized workflow through the trusted host boundary."
        in durable_bundle
    )


def test_known_computer_use_ids_are_projected_from_provider_failure_boundaries(
    tmp_path: Path,
) -> None:
    observed_snapshot = "snapshotFailureMaple"
    observed_element = "semanticFailureWillow"
    state = {
        "snapshot_id": observed_snapshot,
        "elements": [{"id": observed_element, "role": "AXButton", "value": "READY"}],
    }
    error_echo = f"Provider failed after {observed_snapshot}, {observed_element}, and READY."
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="observe-call", name="computer_state", arguments={})],
                raw={},
            ),
            LLMError(error_echo),
        ],
        host_request=lambda name, arguments: copy.deepcopy(state),
        reasoning_delta=f"Reasoned over {observed_snapshot} and {observed_element}.",
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        with pytest.raises(
            AgentRuntimeError, match=re.escape(_PROTECTED_FAILURE_MESSAGE)
        ) as raised:
            session.run_turn("Observe the approved control before the provider fails.")
        messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert str(raised.value) == _PROTECTED_FAILURE_MESSAGE
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert observed_snapshot in client.request_snapshots[1]
    assert observed_element in client.request_snapshots[1]
    assert client.stream_flags == [False, False]
    assert client.reasoning_callback_flags == [False, False]
    assert surface.reasoning_events == []

    assert log_path is not None
    durable_bundle = _computer_use_durable_boundary_bundle(
        client=client,
        surface=surface,
        hooks=hooks,
        diagnostics=diagnostics,
        messages=messages,
        log_path=log_path,
    )
    assert observed_snapshot not in durable_bundle
    assert observed_element not in durable_bundle
    assert "READY" not in durable_bundle
    assert "terminal_error" in durable_bundle
    assert "[redacted: approved sensitive content]" in durable_bundle


def test_malformed_computer_use_arguments_fail_closed_without_identifier_leaks(
    tmp_path: Path,
) -> None:
    malformed_snapshot = "snapshotMalformedMaple"
    malformed_element = "semanticMalformedWillow"
    malformed_arguments = (
        f'{{"action":"press","element_id":"{malformed_element}",'
        f'"snapshot_id":"{malformed_snapshot}" BROKEN'
    )
    parsed_arguments = {"_raw_arguments": malformed_arguments}
    assert set(
        collect_computer_use_private_identifiers(
            "computer_action",
            parsed_arguments,
        )
    ) == {malformed_snapshot, malformed_element}
    projected_arguments = redact_sensitive_tool_arguments(
        "computer_action",
        parsed_arguments,
    )
    assert projected_arguments == {
        "_raw_arguments": "[redacted: transient computer use identifier]"
    }

    host_calls: list[tuple[str, dict[str, Any]]] = []
    session, captures = _make_computer_use_session(
        tmp_path,
        responses=[
            LLMResponse(
                content=f"Trying {malformed_snapshot} and {malformed_element}.",
                tool_calls=[
                    ToolCall(
                        id="malformed-call",
                        name="computer_action",
                        arguments=parsed_arguments,
                    )
                ],
                raw={"arguments": malformed_arguments},
                provider_metadata={
                    OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                        "response_id": "responseMalformedPrivate",
                        "output_items": [
                            {
                                "type": "function_call",
                                "id": "functionMalformedPrivate",
                                "call_id": "malformed-call",
                                "name": "computer_action",
                                "arguments": malformed_arguments,
                            }
                        ],
                    }
                },
            )
        ],
        host_request=lambda name, arguments: (
            host_calls.append((name, copy.deepcopy(arguments))) or {}
        ),
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Reject malformed Computer Use arguments safely.") == 1
        messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert host_calls == []
    assert client.stream_flags == [False]
    assert log_path is not None
    durable_bundle = _computer_use_durable_boundary_bundle(
        client=client,
        surface=surface,
        hooks=hooks,
        diagnostics=diagnostics,
        messages=messages,
        log_path=log_path,
    )
    assert malformed_arguments not in durable_bundle
    assert malformed_snapshot not in durable_bundle
    assert malformed_element not in durable_bundle
    assert "[redacted: transient computer use identifier]" in durable_bundle


def _make_session(
    tmp_path: Path,
    *,
    tool_result: dict[str, Any] | None = None,
    tool_error: Exception | None = None,
    final_response: str = "Done.",
    final_raw: dict[str, Any] | None = None,
    initial_response: str = f"Preparing {_CANARY}",
    stream: bool = False,
) -> tuple[AgentSession, Any]:
    def run_tool(_args: dict[str, Any]) -> dict[str, Any]:
        if tool_error is not None:
            raise tool_error
        return copy.deepcopy(tool_result or {})

    tool = ToolDef(
        name="fs_read",
        description="test sensitive read",
        parameters={"type": "object", "properties": {}, "required": []},
        run=run_tool,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content=initial_response,
                tool_calls=[
                    ToolCall(
                        id="call-sensitive",
                        name="fs_read",
                        arguments={"path": ".env", "content": _CANARY},
                    )
                ],
                raw={"tool_call": {"arguments": _CANARY}},
            ),
            LLMResponse(content=final_response, tool_calls=[], raw=final_raw or {}),
        ]
    )
    surface = _RecordingSurface()
    hooks = _RecordingHooks()
    diagnostics = _RecordingDiagnostics()
    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path / "sessions",
        session_id="sensitive-boundary",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(tmp_path / "session-artifacts"),
        workspace_root=tmp_path,
        threshold_chars=1,
        preview_chars=1,
    )
    session = AgentSession(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=stream),
        root=tmp_path,
        mode="auto",
        yes=True,
        stream=stream,
        routing_mode="code_only",
        max_steps=4,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,
        store=store,
        client=client,  # type: ignore[arg-type]
        model_registry=_FakeRegistry(),  # type: ignore[arg-type]
        usage_summary=UsageSummary(),
        usage_role="main",
        tool_output_offloader=offloader,
        conversation_compactor=None,
        tool_output_offload_enabled=True,
        conversation_summarization_enabled=False,
        compaction_profile="chat",
        tools={tool.name: tool},
        tool_list=[tool.as_openai_tool()],
        messages=[{"role": "system", "content": "system prompt"}],
        hook_dispatcher=hooks,  # type: ignore[arg-type]
        crash_diagnostics=diagnostics,  # type: ignore[arg-type]
        verification_enabled=False,
        skills_enabled=False,
    )
    return session, (client, surface, hooks, diagnostics, store)


def test_sensitive_policy_redacts_every_durable_and_display_boundary(tmp_path: Path) -> None:
    result = {
        "path": ".env",
        "content": _CANARY,
        "_alysis_output_policy": {
            "sensitive": True,
            "persist": "redact",
            "display": "redact",
            "categories": ["environment_file"],
        },
    }
    session, captures = _make_session(tmp_path, tool_result=result)
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Read the approved environment file.") == 0
        in_memory_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert log_path is not None
    persisted = log_path.read_text(encoding="utf-8")
    displayed = _serialized([*surface.events, *surface.legacy_events])
    hook_payloads = _serialized(hooks.payloads)
    diagnostic_payloads = _serialized(diagnostics.events)
    replay_messages = _serialized(in_memory_messages)
    consumed_provider_requests = _serialized(client.request_references)
    artifacts = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in tmp_path.rglob("*")
        if path.is_file() and path != log_path
    )

    for boundary_copy in (
        persisted,
        displayed,
        hook_payloads,
        diagnostic_payloads,
        replay_messages,
        consumed_provider_requests,
        artifacts,
    ):
        assert _CANARY not in boundary_copy

    combined = "\n".join(
        (persisted, displayed, hook_payloads, replay_messages, consumed_provider_requests)
    )
    assert "Sensitive tool output redacted." in combined
    assert "environment_file" in combined
    assert ".env" in combined
    assert client.saw_canary_during_call == [False, True]


def test_provider_echo_of_approved_secret_is_taint_redacted_before_every_boundary(
    tmp_path: Path,
) -> None:
    similar_non_secret = "ALYSIS_SECRET_CANARY_7dc6f759"
    echoed_reply = f"Approved value: {_CANARY}. Similar value: {similar_non_secret}."
    result = {
        "path": ".env",
        "content": _CANARY,
        "_alysis_output_policy": {
            "sensitive": True,
            "persist": "redact",
            "display": "redact",
            "categories": ["environment_file"],
        },
    }
    session, captures = _make_session(
        tmp_path,
        tool_result=result,
        final_response=echoed_reply,
        final_raw={"provider_echo": echoed_reply},
        initial_response="Working safely.",
        stream=True,
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Read the approved environment file.") == 0
        in_memory_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert log_path is not None
    every_boundary = _serialized(
        {
            "session": log_path.read_text(encoding="utf-8"),
            "surface": [*surface.events, *surface.legacy_events],
            "hooks": hooks.payloads,
            "diagnostics": diagnostics.events,
            "messages": in_memory_messages,
            "provider_requests": client.request_references,
        }
    )
    assert _CANARY not in every_boundary
    assert "[redacted: approved sensitive content]" in every_boundary
    assert similar_non_secret in every_boundary
    assert client.stream_flags == [True, False]


def test_exact_taint_redaction_leaves_similar_and_non_sensitive_values_unchanged() -> None:
    arguments = {"path": ".env"}
    result = {
        "path": ".env",
        "content": _CANARY,
        "_alysis_output_policy": {
            "sensitive": True,
            "persist": "redact",
            "display": "redact",
            "categories": ["environment_file"],
        },
    }
    taints = collect_sensitive_response_taints("fs_read", arguments, result)
    similar = "ALYSIS_SECRET_CANARY_7dc6f759"
    response = LLMResponse(
        content=f"secret={_CANARY}; similar={similar}",
        tool_calls=[],
        raw={"secret": _CANARY, "similar": similar},
    )

    safe = redact_sensitive_response_taints(response, taints)
    ordinary = LLMResponse(content="ordinary", tool_calls=[], raw={"value": "ordinary"})

    assert _CANARY not in _serialized(safe)
    assert similar in safe.content
    assert safe.raw["similar"] == similar
    assert redact_sensitive_response_taints(ordinary, taints) is ordinary


def test_short_taint_preserves_provider_linkage_and_opaque_replay_state() -> None:
    call_id = "call_protocol_0"
    response = LLMResponse(
        content="Visible value 0",
        tool_calls=[
            ToolCall(
                id=call_id,
                name="computer_action",
                arguments={"action": "set_value", "text": "0"},
                provider_metadata={
                    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                        "content": {
                            "role": "malformed_role_0",
                            "parts": [
                                {
                                    "thoughtSignature": "0",
                                    "functionResponse": {"id": "0", "name": "0"},
                                }
                            ],
                        }
                    }
                },
            )
        ],
        raw={"provider_echo": "Visible value 0", "call_id": call_id},
        response_model="model-0",
        provider_metadata={
            ROUTE_IDENTITY_PROVIDER_METADATA_KEY: {
                "version": 1,
                "fingerprint": "route_fingerprint_0",
            },
            OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                "response_id": "response_0",
                "request_plan": {
                    "request_message_count": 2,
                    "request_messages_signature": "signature_0",
                },
                "output_items": [
                    {
                        "type": "function_call",
                        "id": "function_item_0",
                        "call_id": call_id,
                        "name": "computer_action",
                        "status": "completed",
                        "arguments": '{"action":"set_value","text":"0"}',
                        "encrypted_content": "0",
                        "role": "0",
                    },
                    {
                        "type": "reasoning",
                        "id": "reasoning_item_0",
                        "encrypted_content": "opaque_reasoning_0",
                        "call_id": "0",
                        "name": "0",
                        "role": "0",
                        "summary": [{"type": "summary_text", "text": "Saw 0"}],
                    },
                    {
                        "type": "message",
                        "id": "message_item_0",
                        "role": "assistant",
                        "status": "completed",
                        "name": "0",
                        "call_id": "0",
                        "encrypted_content": "0",
                        "content": [{"type": "output_text", "text": "Saw 0"}],
                    },
                    {
                        "type": "malformed_0",
                        "id": "untrusted_0",
                        "status": "untrusted_0",
                    },
                ],
            },
            ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY: {
                "message_id": "message_0",
                "request_plan": {"request_messages_signature": "anthropic_signature_0"},
                "content_blocks": [
                    {
                        "type": "thinking",
                        "thinking": "Saw 0",
                        "signature": "opaque_thinking_0",
                    },
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": "computer_action",
                        "input": {"action": "set_value", "text": "0"},
                        "signature": "0",
                    },
                    {
                        "type": "malformed_0",
                        "id": "untrusted_0",
                        "name": "untrusted_0",
                        "text": "Saw 0",
                    },
                ],
            },
            GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                "request_plan": {"request_messages_signature": "gemini_signature_0"},
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "thoughtSignature": "opaque_gemini_0",
                            "functionCall": {
                                "id": call_id,
                                "name": "computer_action",
                                "args": {
                                    "action": "set_value",
                                    "text": "0",
                                    "thoughtSignature": "0",
                                },
                            },
                        }
                    ],
                },
            },
            "provider_echo": {"id": "0", "thoughtSignature": "0", "text": "Visible 0"},
        },
    )

    safe = redact_sensitive_response_taints(response, {"0"})

    assert safe.tool_calls[0].id == call_id
    assert safe.tool_calls[0].name == "computer_action"
    assert safe.tool_calls[0].arguments["text"] == "[redacted: approved sensitive content]"
    malformed_gemini = safe.tool_calls[0].provider_metadata[
        GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY
    ]["content"]
    assert malformed_gemini["role"] == ("malformed_role_[redacted: approved sensitive content]")
    assert malformed_gemini["parts"][0]["thoughtSignature"] == (
        "[redacted: approved sensitive content]"
    )
    assert malformed_gemini["parts"][0]["functionResponse"] == {
        "id": "[redacted: approved sensitive content]",
        "name": "[redacted: approved sensitive content]",
    }
    assert safe.response_model == "model-0"
    assert safe.provider_metadata[ROUTE_IDENTITY_PROVIDER_METADATA_KEY] == {
        "version": 1,
        "fingerprint": "route_fingerprint_0",
    }

    openai = safe.provider_metadata[OPENAI_RESPONSES_PROVIDER_METADATA_KEY]
    assert openai["response_id"] == "response_0"
    assert openai["request_plan"]["request_messages_signature"] == "signature_0"
    assert openai["output_items"][0]["id"] == "function_item_0"
    assert openai["output_items"][0]["call_id"] == call_id
    assert openai["output_items"][0]["name"] == "computer_action"
    assert openai["output_items"][0]["encrypted_content"] == (
        "[redacted: approved sensitive content]"
    )
    assert openai["output_items"][0]["role"] == "[redacted: approved sensitive content]"
    assert openai["output_items"][1]["encrypted_content"] == "opaque_reasoning_0"
    assert openai["output_items"][1]["call_id"] == "[redacted: approved sensitive content]"
    assert openai["output_items"][1]["name"] == "[redacted: approved sensitive content]"
    assert openai["output_items"][1]["role"] == "[redacted: approved sensitive content]"
    assert openai["output_items"][2]["id"] == "message_item_0"
    assert openai["output_items"][2]["role"] == "assistant"
    assert openai["output_items"][2]["name"] == "[redacted: approved sensitive content]"
    assert openai["output_items"][2]["call_id"] == "[redacted: approved sensitive content]"
    assert openai["output_items"][2]["encrypted_content"] == (
        "[redacted: approved sensitive content]"
    )
    assert openai["output_items"][3]["type"] == ("malformed_[redacted: approved sensitive content]")
    assert openai["output_items"][3]["id"] == ("untrusted_[redacted: approved sensitive content]")
    assert "Saw 0" not in _serialized(openai)

    anthropic = safe.provider_metadata[ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY]
    assert anthropic["message_id"] == "message_0"
    assert anthropic["content_blocks"][0]["signature"] == "opaque_thinking_0"
    assert anthropic["content_blocks"][1]["id"] == call_id
    assert anthropic["content_blocks"][1]["name"] == "computer_action"
    assert anthropic["content_blocks"][1]["signature"] == ("[redacted: approved sensitive content]")
    assert anthropic["content_blocks"][2]["type"] == (
        "malformed_[redacted: approved sensitive content]"
    )
    assert anthropic["content_blocks"][2]["id"] == (
        "untrusted_[redacted: approved sensitive content]"
    )
    assert "Saw 0" not in _serialized(anthropic)

    gemini = safe.provider_metadata[GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY]
    gemini_part = gemini["content"]["parts"][0]
    assert gemini_part["thoughtSignature"] == "opaque_gemini_0"
    assert gemini_part["functionCall"]["id"] == call_id
    assert gemini_part["functionCall"]["name"] == "computer_action"
    assert gemini_part["functionCall"]["args"]["text"] == ("[redacted: approved sensitive content]")
    assert gemini_part["functionCall"]["args"]["thoughtSignature"] == (
        "[redacted: approved sensitive content]"
    )
    assert safe.provider_metadata["provider_echo"] == {
        "id": "[redacted: approved sensitive content]",
        "thoughtSignature": "[redacted: approved sensitive content]",
        "text": "Visible [redacted: approved sensitive content]",
    }
    assert safe.raw["provider_echo"] == "Visible value [redacted: approved sensitive content]"


def test_taint_collection_redacts_bare_assignment_and_json_secret_values() -> None:
    result = {
        "path": ".env",
        "content": f'API_TOKEN={_CANARY}\nJSON={{"token":"{_ERROR_CANARY}"}}\n',
        "_alysis_output_policy": {
            "sensitive": True,
            "persist": "redact",
            "display": "redact",
            "categories": ["environment_file"],
        },
    }
    taints = collect_sensitive_response_taints("fs_read", {"path": ".env"}, result)
    response = LLMResponse(
        content=f"token={_CANARY}; nested={_ERROR_CANARY}",
        tool_calls=[],
        raw={},
    )

    safe = redact_sensitive_response_taints(response, taints)

    assert _CANARY not in safe.content
    assert _ERROR_CANARY not in safe.content


def test_tainted_secret_is_removed_from_follow_up_tool_arguments_before_execution() -> None:
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="echo-attempt",
                name="shell_run",
                arguments={"command": f"printf {_CANARY}"},
            )
        ],
        raw={"command": f"printf {_CANARY}"},
    )

    safe = redact_sensitive_response_taints(response, {_CANARY})

    assert _CANARY not in _serialized(safe)
    assert safe.tool_calls[0].arguments["command"] == (
        "printf [redacted: approved sensitive content]"
    )


def test_sensitive_exception_text_is_never_replayed_or_persisted(tmp_path: Path) -> None:
    session, captures = _make_session(
        tmp_path,
        tool_error=RuntimeError(f"reader repr exposed {_ERROR_CANARY}"),
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Try the approved environment read.") == 0
        in_memory_messages = copy.deepcopy(session.messages)
    finally:
        session.close()

    assert log_path is not None
    everything = _serialized(
        {
            "session": log_path.read_text(encoding="utf-8"),
            "surface": [*surface.events, *surface.legacy_events],
            "hooks": hooks.payloads,
            "diagnostics": diagnostics.events,
            "messages": in_memory_messages,
            "provider_requests": client.request_references,
        }
    )
    assert _ERROR_CANARY not in everything
    assert all(_ERROR_CANARY not in request for request in client.request_snapshots)
    assert (
        "Sensitive path is protected and will not be readable after this failure. "
        "No content was returned. This failure is terminal; do not retry."
    ) in everything


def test_non_sensitive_values_are_returned_byte_for_byte() -> None:
    arguments = {
        "path": "src/example.py",
        "content": "print('ordinary')\n",
        "edits": [{"expected_old": "ordinary", "new": "updated"}],
    }
    result = {"path": "src/example.py", "content": "ordinary\r\nbytes"}
    boundary = sensitive_tool_boundary("fs_write", arguments, result=result)

    assert boundary.sensitive is False
    assert redact_sensitive_tool_arguments("fs_write", arguments) is arguments
    assert redact_sensitive_tool_result("fs_write", arguments, result) is result


def test_sensitive_provider_response_is_sanitized_before_usage_persistence() -> None:
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="call-sensitive",
                name="fs_write",
                arguments={"path": ".env", "content": _CANARY},
            )
        ],
        raw={"output": [{"arguments": _CANARY}]},
    )

    safe = redact_sensitive_response_for_persistence(response)

    assert safe is not response
    assert safe.raw == {}
    assert safe.tool_calls[0].arguments["content"] == "[redacted: sensitive file content]"
    assert response.tool_calls[0].arguments["content"] == _CANARY


class _ImmutableCall:
    __slots__ = ("_arguments", "_id", "_name", "_provider_metadata")

    def __init__(self) -> None:
        object.__setattr__(self, "_id", "immutable-call")
        object.__setattr__(self, "_name", "fs_write")
        object.__setattr__(self, "_arguments", {"path": ".env", "content": _CANARY})
        object.__setattr__(self, "_provider_metadata", None)

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def arguments(self) -> dict[str, Any]:
        return self._arguments

    @property
    def provider_metadata(self) -> None:
        return self._provider_metadata

    def __copy__(self) -> _ImmutableCall:
        return self


class _ImmutableResponse:
    __slots__ = ("_call",)

    def __init__(self) -> None:
        object.__setattr__(self, "_call", _ImmutableCall())

    @property
    def content(self) -> str:
        return f"echoed {_CANARY}"

    @property
    def tool_calls(self) -> list[_ImmutableCall]:
        return [self._call]

    @property
    def raw(self) -> dict[str, Any]:
        return {"arguments": _CANARY}

    @property
    def response_model(self) -> str:
        return "immutable-model"

    @property
    def usage(self) -> None:
        return None

    @property
    def provider_metadata(self) -> None:
        return None

    @property
    def reasoning(self) -> tuple[Any, ...]:
        return ()

    @property
    def assistant_phase(self) -> None:
        return None

    def __copy__(self) -> _ImmutableResponse:
        return self


def test_immutable_sensitive_response_fallback_is_fail_closed() -> None:
    response = _ImmutableResponse()

    safe = redact_sensitive_response_for_persistence(response)

    assert safe is not response
    assert safe.content == ""
    assert safe.raw == {}
    assert safe.tool_calls[0] is not response.tool_calls[0]
    assert safe.tool_calls[0].arguments["content"] == "[redacted: sensitive file content]"
    assert _CANARY not in _serialized(
        {
            "content": safe.content,
            "raw": safe.raw,
            "arguments": safe.tool_calls[0].arguments,
        }
    )


def test_sensitive_error_result_is_never_sent_or_persisted(tmp_path: Path) -> None:
    # A tool that RETURNS an error payload under a sensitive policy (rather
    # than raising) must get the same fail-closed redaction on every boundary.
    result = {
        "error": f"lookup repr exposed {_ERROR_CANARY}",
        "_alysis_output_policy": {
            "sensitive": True,
            "persist": "redact",
            "display": "redact",
            "categories": ["credential_file"],
        },
    }
    session, captures = _make_session(
        tmp_path,
        tool_result=result,
        final_response="Used the approved result.",
        initial_response="Preparing the lookup.",
    )
    client, surface, hooks, diagnostics, store = captures
    log_path = store.path
    try:
        assert session.run_turn("Use the approved lookup.") == 0
    finally:
        session.close()

    assert all(_ERROR_CANARY not in request for request in client.request_snapshots)
    assert log_path is not None
    durable_and_display = _serialized(
        {
            "session": log_path.read_text(encoding="utf-8"),
            "surface": [*surface.events, *surface.legacy_events],
            "hooks": hooks.payloads,
            "diagnostics": diagnostics.events,
            "consumed_requests": client.request_references,
        }
    )
    assert _ERROR_CANARY not in durable_and_display
    assert (
        "Sensitive path is protected and will not be readable after this failure. "
        "No content was returned. This failure is terminal; do not retry."
    ) in durable_and_display
