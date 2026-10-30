"""Provider/runtime behaviors salvaged from ``tests/test_agent_loop_small_talk.py``.

That suite exercised the legacy pre-turn semantic router and is deleted with
it. The tests here preserve the ~20 behaviors from that file that were never
about routing — usage accounting and HUD anchoring, provider-metadata
preservation, streaming/reasoning-trace display capability, LLM error
propagation and sanitization, web-search tool exposure and failure handling,
bounded ``/chat`` history, and session client provisioning — re-expressed on
the unified turn path (``unified_turn_path_enabled=True`` is the default:
``run_turn`` never performs legacy pre-turn routing, and posture derives from
the execution mode). Skill choice stays with the main agent and provisions no
separate selector client.

Every test carries a "Salvages:" comment naming the original test whose
assertion intent it preserves.

Not salvaged (router-only — behavior is deleted with the router):
- test_how_are_you_routes_to_chat_without_repo_agent_call,
  test_explain_recursion_routes_to_general_without_repo_agent_call,
  test_repo_request_routes_to_repo_and_calls_main_agent_client — route
  selection itself; the unified main-loop flow is covered by
  tests/test_unified_turn_path.py.
- test_non_repo_fast_path_uses_router_reply_without_second_llm_call,
  test_non_repo_tool_assisted_path_ignores_router_reply_for_general_turn,
  test_non_repo_chat_follow_up_uses_recent_history_not_router_reply (reply
  reuse half) — router-reply reuse; chat continuity is covered by
  tests/test_ask_and_chat_turns.py.
- test_router_context_lists_exposed_custom_tools,
  test_tool_route_exposes_mcp_tools_in_non_repo_chat — router route-context
  payload and tool-route partition; the unified surface exposes all
  registered tools without a route split.
- test_tool_route_step_budget_* (4 tests) — the non-repo tool loop's private
  step budget/finalize machinery; the unified loop's budget and finalization
  are covered by tests/test_agent_loop_step_budget_runtime.py and
  tests/test_agent_loop_finalization_advisories.py.
- test_non_repo_web_tool_one_shot_does_not_require_material_repo_edits — the
  router's advisory one-shot gate bypass; unified one-shot turns derive
  execute intent and are governed by the one-shot completion gate
  (tests/test_agent_loop_one_shot_follow_through.py).
- test_forge_exec_mcp_write_task_uses_semantic_repo_route — route semantics.
- All router-fallback/arbitration/posture-contract tests
  (test_repo_backed_*, test_failed_router_model_*, test_router_exception_*,
  test_malformed_router_*, test_router_failure_*,
  test_repo_summary_follow_up_preserves_advisory_execution_posture_*,
  test_vague_bugfix_request_stays_on_general_fast_path_outside_repo_session,
  test_first_repo_advisory_question_*) — posture now derives from the
  execution mode (tests/test_unified_turn_path.py).
- Router relation/continuity routing tests (test_local_build_request_*,
  test_plain_dir_*, test_related_anchored_*, test_repo_follow_up_continuity_*,
  test_unrelated_*) — the fast-path/repo-path partition no longer exists.
- test_semantic_router_posture_replaces_bugfix_keyword_heuristic,
  test_semantic_router_posture_replaces_improvement_keyword_heuristic — the
  router contract's outcome-to-posture mapping.
- Router language/script classification tests
  (test_non_repo_response_uses_model_selected_language_directive,
  test_repo_turn_honors_explicit_script_override,
  test_non_repo_turn_honors_explicit_language_override,
  test_gibberish_non_repo_turn_defaults_to_english_fallback,
  test_code_only_mode_* language tests) — per-turn language detection is
  deleted; the config-driven reply-language directive is covered by
  tests/test_unified_turn_path.py.
- test_non_repo_empty_completion_logs_breadcrumb_before_fallback — the
  non-repo clarification fallback is deleted; the unified loop's
  empty-response anomaly machinery has its own suite.

Already covered elsewhere (not duplicated here):
- Small talk through the main loop: tests/test_unified_turn_path.py.
- /chat continuity: tests/test_ask_and_chat_turns.py.
- First-repo-turn grounding retry (test_first_repo_turn_*):
  tests/test_agent_loop_interactive_execution_posture.py.
- Custom-tool stdout/stderr session artifacts
  (test_forge_custom_tool_streams_use_session_artifacts):
  tests/test_custom_tools_runtime.py.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent.prompt_context as prompt_context_mod
import alysis_code.agent.session as session_mod
from alysis_code.agent.llm_calls import _is_fatal_non_repo_llm_error
from alysis_code.agent.prompt_context import (
    _NON_REPO_MAX_RECENT_VISIBLE_HISTORY_CHARS,
    _NON_REPO_MAX_RECENT_VISIBLE_HISTORY_TOTAL_CHARS,
)
from alysis_code.agent_loop import ToolDef, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.metadata import (
    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
    PROVIDER_METADATA_KEY,
    endpoint_descriptor,
)
from alysis_code.llm.openai_compat import LLMError, LLMResponse, ToolCall
from alysis_code.llm.protocols import ReasoningTraceCapability
from alysis_code.llm.types import ReasoningOutputKind
from alysis_code.llm_error_display import friendly_llm_error_message
from alysis_code.request_estimation import (
    estimate_message_tokens,
    estimate_request_token_breakdown,
)
from alysis_code.session_store import read_session_events
from alysis_code.skills.models import DiscoveredSkills
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.surface.types import ToolEndEvent
from alysis_code.tools.availability import WEB_UNAVAILABLE_OBSERVATION
from alysis_code.tools.web import WebFetchError
from alysis_code.tools.web_search import WebSearchError


@pytest.fixture(autouse=True)
def _unified_turn_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The unified (router-free) path is the config default; drop any ambient
    # env override so a developer shell pinned to the legacy path cannot flip
    # this suite onto machinery that no longer exists.
    monkeypatch.delenv("ALYSIS_UNIFIED_TURN_PATH", raising=False)


@pytest.fixture(autouse=True)
def _clear_generic_web_search_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _session(
    tmp_path: Path,
    *,
    cfg: AppConfig | None = None,
    mode: str = "review",
    api_key_override: str = "override-key",
    **kwargs: Any,
) -> Any:
    cfg = cfg or AppConfig(model="test-model")
    return create_session(
        cfg=cfg,
        root=tmp_path,
        mode=mode,
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override=api_key_override,
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
        **kwargs,
    )


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def _final_contents(path: Path) -> list[str]:
    return [str(payload.get("content") or "") for payload in _event_payloads(path, "final")]


def _tool_schema_names(tools: list[dict[str, Any]] | None) -> set[str]:
    return {str((tool.get("function") or {}).get("name") or "") for tool in (tools or [])}


class _FinalReplyClient:
    """Main-model fake: one buffered final reply, optionally with metadata/usage."""

    model = "test-model"
    temperature = 0.2

    def __init__(
        self,
        reply: str,
        *,
        provider_metadata: dict[str, Any] | None = None,
        usage: Any | None = None,
    ) -> None:
        self.reply = reply
        self.provider_metadata = provider_metadata
        self.usage = usage
        self.calls = 0
        self.call_tools: list[list[dict[str, Any]] | None] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
        **_kwargs: Any,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        self.calls += 1
        self.call_tools.append(tools)
        return LLMResponse(
            content=self.reply,
            tool_calls=[],
            raw={},
            provider_metadata=self.provider_metadata,
            usage=self.usage,
        )


class _ScriptedClient:
    """Main-model fake replaying scripted responses; records every request."""

    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.call_records: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
        **_kwargs: Any,
    ) -> LLMResponse:
        _ = tool_choice, stream, on_text_delta, temperature
        self.call_records.append({"messages": list(messages), "tools": tools})
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


class _StreamingReplyClient:
    """Main-model fake that streams reasoning-summary and text deltas."""

    model = "test-model"
    temperature = 0.2
    reasoning_trace_capability = ReasoningTraceCapability(
        adapter="test_safe_summary",
        output_kind=ReasoningOutputKind.SUMMARY,
        supports_streaming=True,
        supports_buffered=True,
        requestable=True,
    )

    def __init__(
        self,
        *,
        reply: str,
        text_deltas: list[str] | None = None,
        reasoning_deltas: list[str] | None = None,
        usage: Any | None = None,
    ) -> None:
        self.reply = reply
        self.text_deltas = list(text_deltas or [])
        self.reasoning_deltas = list(reasoning_deltas or [])
        self.usage = usage
        self.calls = 0
        self.summary_callback_supplied: bool | None = None
        self.call_tools: list[list[dict[str, Any]] | None] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        on_reasoning_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
        **_kwargs: Any,
    ) -> LLMResponse:
        _ = messages, temperature
        self.calls += 1
        self.call_tools.append(tools)
        self.summary_callback_supplied = callable(on_reasoning_delta)
        if on_reasoning_delta is not None:
            for delta in self.reasoning_deltas:
                on_reasoning_delta(delta)
        if stream and on_text_delta is not None:
            for delta in self.text_deltas:
                on_text_delta(delta)
        return LLMResponse(content=self.reply, tool_calls=[], raw={}, usage=self.usage)


class _ReasoningCaptureSurface(NoopSurface):
    """Callback-opt-in surface recording reasoning and legacy text deltas."""

    def __init__(self) -> None:
        self.text_deltas: list[str] = []
        self.reasoning_deltas: list[str] = []
        self.reasoning_starts: list[str] = []
        self.reasoning_ends: list[str] = []

    def on_assistant_token(self, delta: str) -> None:
        self.text_deltas.append(delta)

    def on_reasoning_token(self, delta: str) -> None:
        self.reasoning_deltas.append(delta)

    def on_reasoning_start(self, block_id: str) -> None:
        self.reasoning_starts.append(block_id)

    def on_reasoning_end(self, block_id: str) -> None:
        self.reasoning_ends.append(block_id)


class _DeltaCaptureSurface(NoopSurface):
    """Canonical-event surface recording streamed message deltas."""

    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.ends: list[str] = []

    def emit_message_delta(
        self,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = worker_id, role
        self.deltas.append(text)

    def emit_message_end(
        self,
        text: str = "",
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = worker_id, role
        self.ends.append(text)


def _invalid_api_key_error() -> LLMError:
    return LLMError(
        'LLM error 401: {"error":{"message":"Incorrect API key provided.",'
        '"type":"invalid_request_error","param":null,"code":"invalid_api_key"}}'
    )


def _trial_quota_exhausted_error() -> LLMError:
    return LLMError(
        'LLM error 402: {"error":{"message":"Free trial tokens used up.",'
        '"type":"insufficient_quota","code":"quota_exhausted"}}'
    )


def _private_provider_url_error() -> LLMError:
    return LLMError(
        "LLM error 401: provider failed at "
        "https://route-user:route-pa'ssword@api.example.test/"
        "secret-route-segment<PRIVATE_BOUNDARY_SENTINEL"
        "?token=PRIVATE_BOUNDARY_SENTINEL#PRIVATE_BOUNDARY_SENTINEL "
        "api_key='PRIVATE_API_KEY_SENTINEL_123456' "
        "Authorization: Bearer PRIVATE_BEARER_SENTINEL_123456+/="
    )


class _FailingClient:
    """Main-model fake whose only behavior is raising a scripted LLMError."""

    model = "test-model"
    temperature = 0.2

    def __init__(self, error_factory: Any = _invalid_api_key_error) -> None:
        self.error_factory = error_factory
        self.calls = 0

    def chat(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        raise self.error_factory()


def _replace_web_search_run(session: Any, run: Any) -> None:
    web_search_tool = session.tools["web_search"]
    session.tools["web_search"] = ToolDef(
        name=web_search_tool.name,
        description=web_search_tool.description,
        parameters=web_search_tool.parameters,
        metadata=web_search_tool.metadata,
        run=run,
    )
    session.tool_list = [tool.as_openai_tool() for tool in session.tools.values()]


# ---------------------------------------------------------------------------
# Provider metadata preservation
# ---------------------------------------------------------------------------


# Salvages: test_non_repo_text_response_preserves_native_provider_metadata
def test_final_text_reply_preserves_native_provider_metadata(tmp_path: Path) -> None:
    metadata_payload = {
        "response_id": "resp_non_repo_grounded",
        "content": {
            "role": "model",
            "parts": [
                {
                    "text": "Gemini grounding answered the chat turn.",
                    "thoughtSignature": "non-repo-thought",
                }
            ],
        },
        "groundingMetadata": {
            "webSearchQueries": ["Alysis Code native providers"],
        },
    }
    session = _session(tmp_path)
    session.client = _FinalReplyClient(  # type: ignore[assignment]
        "Gemini grounding answered the chat turn.",
        provider_metadata={
            GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: metadata_payload,
        },
    )

    try:
        exit_code = session.run_turn("Can you answer from native search context?")
        log_path = session.store.path
        assistant_message = dict(session.messages[-1])
    finally:
        session.close()

    assert exit_code == 0
    assert (
        assistant_message[PROVIDER_METADATA_KEY][GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY]
        == metadata_payload
    )
    assistant_events = _event_payloads(log_path, "assistant_message")
    final_event = assistant_events[-1]
    assert final_event["content"] == "Gemini grounding answered the chat turn."
    assert (
        final_event["message"][PROVIDER_METADATA_KEY][GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY]
        == metadata_payload
    )
    assert _final_contents(log_path) == ["Gemini grounding answered the chat turn."]


# ---------------------------------------------------------------------------
# Streaming and reasoning-trace display capability
# ---------------------------------------------------------------------------


# Salvages: test_streaming_non_repo_turn_bypasses_buffered_router_reply (the
# live-delta and reasoning-summary bracketing intent; the buffered-router-reply
# half is router-only)
def test_streaming_turn_emits_live_deltas_and_bracketed_reasoning(tmp_path: Path) -> None:
    surface = _ReasoningCaptureSurface()
    session = _session(
        tmp_path,
        cfg=AppConfig(model="test-model", stream=True),
        surface=surface,
    )
    client = _StreamingReplyClient(
        reply="Live response.",
        reasoning_deltas=["Check ", "the request."],
        text_deltas=["Live ", "response."],
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Hi")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 1
    assert client.summary_callback_supplied is True
    assert surface.reasoning_deltas == ["Check ", "the request."]
    assert len(surface.reasoning_starts) == 1
    assert surface.reasoning_ends == surface.reasoning_starts
    assert surface.text_deltas == ["Live ", "response."]
    assert _final_contents(log_path) == ["Live response."]
    assert _event_payloads(log_path, "route_decision") == []


def test_native_reasoning_capability_receives_display_callback(tmp_path: Path) -> None:
    from alysis_code.cli_impl.tui.surface import TuiSurface
    from alysis_code.cli_impl.tui.transcript import TuiTranscript

    class _RawOnlyClient(_StreamingReplyClient):
        reasoning_trace_capability = ReasoningTraceCapability(
            adapter="raw_only",
            output_kind=ReasoningOutputKind.PROVIDER_REASONING,
            supports_streaming=True,
            supports_buffered=True,
        )

    transcript = TuiTranscript()
    surface = TuiSurface(transcript)
    surface.set_trace_level("full")
    session = _session(
        tmp_path,
        cfg=AppConfig(model="test-model", stream=True),
        surface=surface,
    )
    client = _RawOnlyClient(
        reply="Safe answer.",
        reasoning_deltas=["private chain of thought"],
    )
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Hi") == 0
    finally:
        session.close()

    assert client.summary_callback_supplied is True
    assert ("reasoning", "private chain of thought") in transcript.entries


# Salvages: test_trace_level_does_not_change_normal_agent_requests_or_usage
@pytest.mark.parametrize("trace_level", ["off", "compact", "full"])
@pytest.mark.parametrize("stream", [False, True])
def test_trace_level_does_not_change_normal_agent_requests_or_usage(
    tmp_path: Path,
    trace_level: str,
    stream: bool,
) -> None:
    from alysis_code.cli_impl.tui.surface import TuiSurface
    from alysis_code.cli_impl.tui.transcript import TuiTranscript
    from alysis_code.llm.types import LLMUsage

    transcript = TuiTranscript()
    surface = TuiSurface(transcript)
    surface.set_trace_level(trace_level)
    session = _session(
        tmp_path,
        cfg=AppConfig(model="test-model", stream=stream),
        surface=surface,
    )
    client = _StreamingReplyClient(
        reply="Hi there.",
        text_deltas=["Hi ", "there."],
        reasoning_deltas=["I should answer briefly."],
        usage=LLMUsage(
            prompt_tokens=200,
            completion_tokens=20,
            total_tokens=220,
            reasoning_tokens=12,
        ),
    )
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Hi") == 0
        totals = session.usage_summary.totals()
        log_path = session.store.path
    finally:
        session.close()

    # Trace level is a display concern only: the provider request count and
    # the recorded usage are byte-identical across off/compact/full.
    assert client.calls == 1
    assert totals["prompt_tokens"] == 200
    assert totals["completion_tokens"] == 20
    assert totals["total_tokens"] == 220
    assert totals["reasoning_tokens"] == 12
    assert client.summary_callback_supplied is (trace_level != "off")
    reasoning_entries = [entry for entry in transcript.entries if entry[0] == "reasoning"]
    assert bool(reasoning_entries) is (trace_level != "off")
    assert _final_contents(log_path) == ["Hi there."]


# ---------------------------------------------------------------------------
# Usage accounting and HUD anchoring
# ---------------------------------------------------------------------------


# Salvages: test_streaming_non_repo_turn_records_llm_usage (the metering-leak
# guard: a streamed pure-chat turn must emit llm_usage and fold into totals)
def test_streaming_chat_only_turn_records_llm_usage(tmp_path: Path) -> None:
    from alysis_code.llm.types import LLMUsage

    surface = _DeltaCaptureSurface()
    session = _session(
        tmp_path,
        cfg=AppConfig(model="test-model", stream=True),
        surface=surface,
    )
    client = _StreamingReplyClient(
        reply="Hi there.",
        text_deltas=["Hi ", "there."],
        usage=LLMUsage(prompt_tokens=123, completion_tokens=45, total_tokens=168),
    )
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Hi", chat_only=True) == 0
        totals = session.usage_summary.totals()
        log_path = session.store.path
    finally:
        session.close()

    assert surface.deltas == ["Hi ", "there."]
    usage_events = _event_payloads(log_path, "llm_usage")
    assert usage_events, "streamed chat-only turn recorded no llm_usage events"
    operations = {str(payload.get("operation") or "") for payload in usage_events}
    assert operations == {"chat_only_answer"}
    assert "routing_llm" not in operations
    answer = usage_events[-1]
    assert answer["usage_source"] == "api"
    assert answer["prompt_tokens"] == 123
    assert answer["completion_tokens"] == 45
    assert totals["total_tokens"] >= 168
    assert totals["prompt_tokens"] >= 123
    assert _final_contents(log_path) == ["Hi there."]


# Salvages: test_usage_record_uses_provider_count_when_response_omits_input_usage
def test_usage_record_uses_provider_count_when_response_omits_input_usage(
    tmp_path: Path,
) -> None:
    from alysis_code.llm.types import (
        InputTokenCount,
        LLMUsage,
        UsageConfidence,
        UsageContract,
    )

    class _CountCapableClient:
        model = "test-model"
        usage_contract = UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="test_count",
        )

        def count_input_tokens(self, **_kwargs: Any) -> InputTokenCount:
            return InputTokenCount(input_tokens=77)

    session = _session(tmp_path)
    try:
        record = session._record_llm_usage(
            client=_CountCapableClient(),
            response=LLMResponse(
                content="done",
                tool_calls=[],
                raw={},
                usage=LLMUsage(
                    prompt_tokens=None,
                    completion_tokens=5,
                    total_tokens=None,
                ),
            ),
            messages=[{"role": "user", "content": "hello"}],
            tool_list=None,
            operation="test_count_fallback",
        )
    finally:
        session.close()

    assert record is not None
    assert record.prompt_tokens == 77
    assert record.completion_tokens == 5
    assert record.total_tokens == 82
    assert record.usage_source == "api"
    assert record.usage_source_detail == "provider_count"
    assert record.prompt_estimate_error_ratio is not None


# Salvages: test_main_hud_keeps_local_preflight_prompt_provenance
def test_main_hud_keeps_local_preflight_prompt_provenance(tmp_path: Path) -> None:
    from alysis_code.llm.types import (
        InputTokenCount,
        LLMUsage,
        UsageConfidence,
        UsageContract,
        UsageSource,
    )

    class _EstimatedCountClient:
        model = "test-model"
        provider_key = "compat-provider"
        protocol = "openai_compat"
        base_url = "https://compat-provider.invalid/v1"
        usage_contract = UsageContract(
            response_usage_confidence=UsageConfidence.REPORTED,
            input_token_count_strategy="openai_compat_provider_payload",
        )

        def count_input_tokens(self, **_kwargs: Any) -> InputTokenCount:
            return InputTokenCount(
                input_tokens=77,
                source=UsageSource.LOCAL_ESTIMATE,
                confidence=UsageConfidence.ESTIMATED,
            )

    session = _session(tmp_path)
    try:
        client = _EstimatedCountClient()
        record = session._record_llm_usage(
            client=client,
            response=LLMResponse(
                content="done",
                tool_calls=[],
                raw={},
                usage=LLMUsage(
                    prompt_tokens=None,
                    completion_tokens=5,
                    total_tokens=105,
                    confidence=UsageConfidence.REPORTED,
                ),
            ),
            messages=[{"role": "user", "content": "hello"}],
            tool_list=None,
            operation="main_llm",
        )
        session.client.provider_key = client.provider_key
        session.client.base_url = client.base_url
        session.messages.append({"role": "assistant", "content": "done"})
        ctx = session.context_left()
    finally:
        session.close()

    assert record is not None
    assert record.usage_source_detail == "mixed"
    assert session.request_context_measurement is not None
    assert session.request_context_measurement.input_tokens == 77
    assert session.request_context_measurement.source == "local_estimate"
    assert session.request_context_measurement.confidence == "estimated"
    assert ctx.token_count_source == "local_estimate"
    assert ctx.token_count_confidence == "estimated"
    assert ctx.provider_projection_applied is False


# Salvages: test_context_left_omits_tool_schemas_for_unsupported_protocol
def test_context_left_omits_tool_schemas_for_unsupported_protocol(tmp_path: Path) -> None:
    from alysis_code.llm.types import UsageConfidence, UsageContract

    class _NoToolClient:
        model = "test-model"
        provider_key = "gemini"
        protocol = "gemini_interactions"
        base_url = "https://generativelanguage.googleapis.com/v1beta"
        supports_tool_calling = False
        usage_contract = UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="gemini_count_tokens_projection",
        )

    session = _session(tmp_path)
    try:
        session.client = _NoToolClient()  # type: ignore[assignment]
        session.tool_list = [
            {
                "type": "function",
                "function": {
                    "name": "unused_tool",
                    "description": "large unused schema " * 500,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        expected = estimate_request_token_breakdown(
            messages=session.messages,
            tool_list=None,
            pinned_prefix_len=session.pinned_prefix_len,
        ).total_tokens
        with_tools = estimate_request_token_breakdown(
            messages=session.messages,
            tool_list=session.tool_list,
            pinned_prefix_len=session.pinned_prefix_len,
        ).total_tokens

        ctx = session.context_left()
    finally:
        session.close()

    assert with_tools > expected
    assert ctx.local_request_estimate_tokens == expected
    assert ctx.startup_baseline_tokens == expected
    assert ctx.dynamic_context_used_tokens == 0
    assert ctx.dynamic_context_percent_left == 100.0


# Salvages: test_context_left_rebases_startup_tools_after_runtime_tool_disable
def test_context_left_rebases_startup_tools_after_runtime_tool_disable(tmp_path: Path) -> None:
    class _MutableToolClient:
        model = "test-model"
        provider_key = "compat-provider"
        protocol = "openai_compat"
        base_url = "https://compat-provider.invalid/v1"
        supports_tool_calling = True

    session = _session(tmp_path)
    try:
        client = _MutableToolClient()
        session.client = client  # type: ignore[assignment]
        session.tool_list = [
            {
                "type": "function",
                "function": {
                    "name": "provider_rejected_tool",
                    "description": "large schema removed after rejection " * 500,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        with_tools = session.context_left()
        client.supports_tool_calling = False
        without_tools = session.context_left()
    finally:
        session.close()

    assert with_tools.startup_baseline_tokens > without_tools.startup_baseline_tokens
    assert without_tools.local_request_estimate_tokens == without_tools.startup_baseline_tokens
    assert without_tools.dynamic_context_used_tokens == 0
    assert without_tools.dynamic_context_percent_left == 100.0


# Salvages: test_main_usage_anchors_hud_to_provider_visible_request
def test_main_usage_anchors_hud_to_provider_visible_request(tmp_path: Path) -> None:
    from alysis_code.llm.types import LLMUsage, UsageConfidence, UsageContract

    session = _session(tmp_path)
    try:
        session.client.provider_key = "test-provider"
        session.client.base_url = "https://provider.invalid/v1"
        session.client.usage_contract = UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
        )
        persistent_before = list(session.messages)
        provider_messages = [
            *persistent_before,
            {"role": "system", "content": "ephemeral controller context " * 200},
            {"role": "user", "content": "ephemeral current instruction " * 50},
        ]
        record = session._record_llm_usage(
            client=session.client,
            response=LLMResponse(
                content="done",
                tool_calls=[],
                raw={},
                usage=LLMUsage(
                    prompt_tokens=7000,
                    completion_tokens=5,
                    total_tokens=7005,
                ),
            ),
            messages=provider_messages,
            tool_list=session.tool_list,
            operation="main_llm",
        )
        assert record is not None
        session.messages.append({"role": "assistant", "content": "done"})

        ctx = session.context_left()
    finally:
        session.close()

    assert session.request_context_measurement is not None
    assert session.request_context_measurement.input_tokens == 7000
    assert ctx.used_input_tokens == 7000 + math.ceil(
        (
            ctx.local_request_estimate_tokens
            - session.request_context_measurement.persistent_anchor_estimate_tokens
        )
        * (
            session.request_context_measurement.input_tokens
            / session.request_context_measurement.anchor_estimate_tokens
        )
    )
    assert ctx.used_input_tokens > 7000
    assert ctx.token_count_source == "mixed"
    assert ctx.token_count_confidence == "estimated"
    assert ctx.anchor_token_count_source == "provider_response"
    assert ctx.dynamic_context_remaining_tokens == ctx.effective_remaining_tokens
    assert ctx.dynamic_context_used_tokens == ctx.used_input_tokens - ctx.startup_baseline_tokens


# ---------------------------------------------------------------------------
# Fatal LLM errors: classification, propagation, sanitization
# ---------------------------------------------------------------------------


# Salvages: test_is_fatal_non_repo_llm_error_classifies_trial_proxy_errors
# (the helper now lives in alysis_code.agent.llm_calls)
def test_is_fatal_non_repo_llm_error_classifies_trial_proxy_errors() -> None:
    for code in ("trial_expired", "quota_exhausted", "rate_limit_exceeded", "plan_inactive"):
        err = LLMError("LLM error 402: " + json.dumps({"error": {"code": code}}))
        assert _is_fatal_non_repo_llm_error(err) is True, code
    assert (
        _is_fatal_non_repo_llm_error(
            LLMError(
                "LLM error 400: invalid_request_error: Your credit balance is too low; "
                "purchase credits."
            )
        )
        is True
    )
    assert _is_fatal_non_repo_llm_error(LLMError("LLM error 429: rate limit")) is True
    # A generic upstream failure stays non-fatal (handled/retried as before).
    assert _is_fatal_non_repo_llm_error(LLMError("LLM error 500: upstream boom")) is False


# Salvages: test_auto_mode_router_auth_error_is_not_masked_as_clarification_fallback
# and test_auto_mode_non_repo_auth_error_is_not_masked_as_clarification_fallback
# (one main-model call site on the unified path, so the two collapse into one)
def test_auth_error_propagates_with_error_event_and_transcript_rollback(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    client = _FailingClient()
    session.client = client  # type: ignore[assignment]
    baseline_messages = copy.deepcopy(session.messages)

    try:
        with pytest.raises(LLMError, match="invalid_api_key"):
            session.run_turn("How are you?")
        assert client.calls == 1

        # The transcript is rolled back to the pre-turn messages. The one pinned
        # message allowed to differ is the task brief: the turn's instruction
        # was accepted (and persisted) before the request, and acceptance is
        # not withdrawn by a provider failure.
        def _without_brief(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                message
                for message in messages
                if not str(message.get("content") or "").startswith("<task_brief>")
            ]

        assert _without_brief(session.messages) == _without_brief(baseline_messages)
        assert len(session.messages) == len(baseline_messages)
        error_payloads = _event_payloads(session.store.path, "error")
        assert error_payloads
        assert "invalid_api_key" in str(error_payloads[-1].get("error") or "")
    finally:
        session.close()


# Salvages: test_auto_mode_non_repo_trial_quota_error_is_not_masked_as_clarification_fallback
# (chat_only is the minimal conversational path where masking would be tempting)
def test_chat_only_trial_quota_error_propagates_and_rolls_back(tmp_path: Path) -> None:
    session = _session(tmp_path)
    client = _FailingClient(error_factory=_trial_quota_exhausted_error)
    session.client = client  # type: ignore[assignment]
    baseline_messages = copy.deepcopy(session.messages)

    try:
        with pytest.raises(LLMError, match="quota_exhausted"):
            session.run_turn("How are you?", chat_only=True)
        assert client.calls == 1
        assert session.messages == baseline_messages
        error_payloads = _event_payloads(session.store.path, "error")
        assert error_payloads
        assert "quota_exhausted" in str(error_payloads[-1].get("error") or "")
    finally:
        session.close()


# Salvages: test_turn_error_log_and_display_sanitize_unexpected_provider_url
def test_turn_error_log_and_display_sanitize_unexpected_provider_url(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    session.client = _FailingClient(  # type: ignore[assignment]
        error_factory=_private_provider_url_error
    )

    try:
        with pytest.raises(LLMError) as exc_info:
            session.run_turn("How are you?")
        error_payloads = _event_payloads(session.store.path, "error")
        assert error_payloads
        persisted_error = str(error_payloads[-1].get("error") or "")
        displayed_error = friendly_llm_error_message(exc_info.value)
        assert "api.example.test" in persisted_error
        for rendered in (persisted_error, displayed_error):
            assert "PRIVATE_BOUNDARY_SENTINEL" not in rendered
            assert "PRIVATE_BEARER_SENTINEL" not in rendered
            assert "PRIVATE_API_KEY_SENTINEL" not in rendered
            assert "route-user" not in rendered
            assert "route-pa'ssword" not in rendered
            assert "secret-route-segment" not in rendered
            assert "token=" not in rendered
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Web-search tool exposure and failure handling
# ---------------------------------------------------------------------------


# Salvages: test_general_non_repo_turn_exposes_web_search_for_model_selection
# (tool registration/exposure; the router's non-repo prompt-copy assertions
# are dropped with the prompt)
def test_turn_surface_exposes_web_search_for_model_selection(tmp_path: Path) -> None:
    cfg = AppConfig(
        model="qwen3.5-plus",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        web_search_mode="auto",
    )
    session = _session(tmp_path, cfg=cfg, mode="readonly")
    assert "web_search" in session.tools
    assert "web_fetch" in session.tools
    _replace_web_search_run(
        session,
        lambda args: {
            "query": args["query"],
            "answer": "Live web search is available.",
            "sources": [{"title": "Python", "url": "https://www.python.org/"}],
            "backend": "test-search",
        },
    )
    client = _FinalReplyClient("Yes, live web search is available.")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("μπορεις να ψαξεις στο ιντερνετ ;")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 1
    exposed = _tool_schema_names(client.call_tools[0])
    assert {"web_search", "web_fetch"} <= exposed
    assert _final_contents(log_path) == ["Yes, live web search is available."]


# Salvages: test_web_search_decision_is_delegated_to_the_model
def test_web_search_decision_is_delegated_to_the_model(tmp_path: Path) -> None:
    cfg = AppConfig(
        model="qwen3.7-plus",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        web_search_mode="auto",
        web_search_policy="auto",
    )
    session = _session(tmp_path, cfg=cfg, mode="readonly")

    def _must_be_model_selected(_args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("web_search must not run before the model selects it")

    _replace_web_search_run(session, _must_be_model_selected)
    client = _FinalReplyClient("The answer requires external evidence.")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Assess a claim that depends on current external evidence.")
        log_path = session.store.path
    finally:
        session.close()

    events = read_session_events(log_path)
    assert exit_code == 0
    # The tool is offered to the model, and only the model may invoke it:
    # nothing in the host pre-runs the search or injects search context.
    assert "web_search" in _tool_schema_names(client.call_tools[0])
    assert client.calls == 1
    assert not any(
        event.get("type")
        in {
            "web_search_policy_decision",
            "web_search_context_injected",
            "web_search_required_unavailable",
        }
        for event in events
    )
    assert _event_payloads(log_path, "error") == []


# Salvages: test_missing_search_backend_does_not_fail_before_model_execution,
# plus the registration half of
# test_non_repo_tool_prompt_does_not_advertise_search_when_unregistered
# (web_search_mode="off" keeps the tool unregistered; the prompt-copy half is
# router-only)
def test_missing_search_backend_does_not_fail_before_model_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "TAVILY_API_KEY",
        "ALYSIS_WEB_SEARCH_API_KEY",
        "ALYSIS_WEB_SEARCH_ADAPTER",
        "ALYSIS_WEB_SEARCH_BASE_URL",
        "ALYSIS_WEB_SEARCH_MODEL",
        "ALYSIS_WEB_SEARCH_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "0")
    cfg = AppConfig(
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        web_search_mode="auto",
        web_search_policy="auto",
    )
    session = _session(tmp_path, cfg=cfg, mode="readonly", api_key_override="deepseek-key")
    assert "web_search" not in session.tools
    client = _FinalReplyClient("No configured search backend is available.")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Assess a claim that requires current external evidence.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 1
    assert "web_search" not in _tool_schema_names(client.call_tools[0])
    assert not any(
        event.get("type") == "web_search_required_unavailable"
        for event in read_session_events(log_path)
    )
    assert _event_payloads(log_path, "error") == []

    # web_search_mode="off" keeps query-based discovery unregistered entirely,
    # while direct web_fetch remains available.
    off_root = tmp_path / "off"
    off_root.mkdir()
    off_session = _session(
        off_root,
        cfg=AppConfig(
            model="qwen3.5-plus",
            base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
            web_search_mode="off",
        ),
        mode="readonly",
    )
    try:
        assert "web_search" not in off_session.tools
        assert "web_fetch" in off_session.tools
    finally:
        off_session.close()


# Salvages: test_non_repo_tool_prompt_filters_stale_unregistered_web_search_schema
def test_stale_unregistered_web_search_schema_is_filtered_from_requests(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(
        model="qwen3.5-plus",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        web_search_mode="auto",
    )
    session = _session(tmp_path, cfg=cfg, mode="readonly")
    assert "web_search" in session.tools
    session.tools.pop("web_search")
    # Leave session.tool_list intentionally stale to simulate a long-running
    # session whose runtime tool registry changed after startup.
    client = _FinalReplyClient("Query-based web discovery is not available in this session.")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Describe the available tools.")
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 1
    exposed = _tool_schema_names(client.call_tools[0])
    assert "web_search" not in exposed
    assert "web_fetch" in exposed


# Salvages: test_non_repo_web_failure_returns_observation_and_continues_without_failed_tool
def test_web_failure_returns_observation_and_continues_without_failed_tool(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(
        model="qwen3.5-plus",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        web_search_mode="auto",
    )
    session = _session(tmp_path, cfg=cfg, mode="readonly")
    search_attempts = 0

    def _failed_search(_args: dict[str, Any]) -> dict[str, Any]:
        nonlocal search_attempts
        search_attempts += 1
        raise WebSearchError("gateway permission denied")

    _replace_web_search_run(session, _failed_search)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="web_search",
                        arguments={"query": "latest external docs"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc2",
                        name="web_search",
                        arguments={"query": "retry latest external docs"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Continued without web access.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Search the internet, then answer briefly.")
        log_path = session.store.path
    finally:
        session.close()

    results = [
        (payload.get("result") or {})
        for payload in _event_payloads(log_path, "tool_result")
        if payload.get("name") == "web_search"
    ]
    assert exit_code == 0
    assert client.calls == 3
    assert search_attempts == 1
    assert len(results) == 2
    assert all(isinstance(result, dict) for result in results)
    # The failure is surfaced as a non-error observation, never a hard error
    # the model would treat as its own mistake.
    assert all("error" not in result for result in results)
    assert all(
        str(result.get("reason") or "").startswith(WEB_UNAVAILABLE_OBSERVATION)
        for result in results
    )
    # The detached turn availability snapshot preserves the first concrete
    # provider failure for a stale/repeated call without invoking the provider.
    assert results[1] == results[0]
    assert "gateway permission denied" in str(results[1].get("reason") or "")
    # The failed tool is withdrawn from the rest of the turn.
    assert "web_search" not in _tool_schema_names(client.call_records[1]["tools"])
    assert _event_payloads(log_path, "web_tool_unavailable")
    assert _final_contents(log_path) == ["Continued without web access."]


class _ToolEndCaptureSurface(NoopSurface):
    """Legacy-event surface recording tool completions as a transcript sees them."""

    def __init__(self) -> None:
        self.tool_ends: list[ToolEndEvent] = []

    def on_tool_end(self, event: ToolEndEvent) -> None:
        self.tool_ends.append(event)


def _replace_web_fetch_run(session: Any, run: Any) -> None:
    web_fetch_tool = session.tools["web_fetch"]
    session.tools["web_fetch"] = ToolDef(
        name=web_fetch_tool.name,
        description=web_fetch_tool.description,
        parameters=web_fetch_tool.parameters,
        metadata=web_fetch_tool.metadata,
        run=run,
    )
    session.tool_list = [tool.as_openai_tool() for tool in session.tools.values()]


def _two_fetch_client(first_url: str, second_url: str) -> _ScriptedClient:
    return _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="web_fetch", arguments={"url": first_url})],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc2", name="web_fetch", arguments={"url": second_url})],
                raw={},
            ),
            LLMResponse(content="Answered from the pages I could read.", tool_calls=[], raw={}),
        ]
    )


def test_web_fetch_remote_site_outcome_is_named_and_keeps_web_fetch(tmp_path: Path) -> None:
    # A site that stalls or refuses automated clients is that source's outcome:
    # the model gets a recoverable error, web_fetch stays on the tool list, and
    # the tool-end event carries what the site did so transcripts can say so.
    surface = _ToolEndCaptureSurface()
    session = _session(tmp_path, mode="readonly", surface=surface)
    attempts: list[str] = []

    def _fetch(args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "")
        attempts.append(url)
        if "stalling" in url:
            raise WebFetchError(
                f"web_fetch request to '{url}' timed out during response read.",
                recoverable=True,
                remote_site_reason="site didn't respond",
            )
        return {"url": url, "final_url": url, "status_code": 200, "content": "ok"}

    _replace_web_fetch_run(session, _fetch)
    client = _two_fetch_client("https://stalling.example.com/", "https://docs.example.com/")
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Read both pages, then answer briefly.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert attempts == ["https://stalling.example.com/", "https://docs.example.com/"]
    assert "web_fetch" in _tool_schema_names(client.call_records[1]["tools"])
    assert not _event_payloads(log_path, "web_tool_unavailable")
    first_result = next(
        (payload.get("result") or {})
        for payload in _event_payloads(log_path, "tool_result")
        if payload.get("name") == "web_fetch"
    )
    assert first_result["recoverable"] is True
    assert first_result["remote_site_reason"] == "site didn't respond"
    stalled, fetched = surface.tool_ends
    assert stalled.status == "failed"
    assert stalled.meta["remote_site_reason"] == "site didn't respond"
    assert fetched.status == "done"
    assert "remote_site_reason" not in fetched.meta


def test_withdrawn_web_fetch_tool_end_is_flagged_unavailable(tmp_path: Path) -> None:
    # An environment-level failure withdraws web_fetch; the observation stays a
    # non-error "done" for the model, but the tool-end event must say the call
    # never ran instead of looking like a success.
    surface = _ToolEndCaptureSurface()
    session = _session(tmp_path, mode="readonly", surface=surface)

    def _fetch(args: dict[str, Any]) -> dict[str, Any]:
        raise WebFetchError(
            f"HTTP request failed for '{args.get('url')}': [Errno 101] Network is unreachable"
        )

    _replace_web_fetch_run(session, _fetch)
    session.client = _two_fetch_client(  # type: ignore[assignment]
        "https://docs.example.com/a", "https://docs.example.com/b"
    )

    try:
        exit_code = session.run_turn("Read both pages, then answer briefly.")
    finally:
        session.close()

    assert exit_code == 0
    assert len(surface.tool_ends) == 2
    for event in surface.tool_ends:
        assert event.status == "done"
        assert event.meta["tool_unavailable"] is True
        assert "Network is unreachable" in event.meta["unavailable_reason"]
        assert not event.meta["unavailable_reason"].startswith(WEB_UNAVAILABLE_OBSERVATION)


# Salvages: test_web_research_with_local_output_routes_to_repo_and_keeps_web_tools
# (the surviving half: web tools and workspace-write tools coexist on one turn
# surface, so a search-then-save request completes in a single turn; the
# route-decision payload half is router-only)
def test_web_research_with_local_output_keeps_web_and_workspace_tools(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(model="test-model", web_search_mode="auto")
    session = _session(tmp_path, cfg=cfg, mode="auto")
    assert "web_search" in session.tools
    _replace_web_search_run(
        session,
        lambda _args: {
            "answer": "Python 3.14.4",
            "sources": [{"title": "Python", "url": "https://www.python.org/"}],
        },
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="web_search",
                        arguments={"query": "latest stable Python version"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc2",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "Python 3.14.4\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Created answer.txt.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn(
            "Search the web for the latest stable Python version and save it to answer.txt."
        )
    finally:
        session.close()

    first_tools = _tool_schema_names(client.call_records[0]["tools"])
    assert exit_code == 0
    assert {"web_search", "web_fetch", "fs_write"} <= first_tools
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "Python 3.14.4\n"


# ---------------------------------------------------------------------------
# Bounded conversational history (/chat)
# ---------------------------------------------------------------------------


# Salvages: test_non_repo_fast_path_uses_small_visible_history_only (the
# bounded-visible-history contract now guards the /chat minimal-prompt path,
# which reuses the same _recent_visible_non_repo_history shaping)
def test_chat_only_turn_uses_small_visible_history_only(tmp_path: Path) -> None:
    session = _session(tmp_path, mode="auto")
    long_request = (
        "Create src/widget.py and keep the behavior stable. "
        + "Please preserve the interface and document the helper. " * 20
    )
    long_reply = (
        "Created src/widget.py with a small helper and kept the interface stable. "
        + "The file now loads a safe default config and documents the behavior. " * 20
    )
    repo_client = _ScriptedClient(
        [
            LLMResponse(
                content="I will create src/widget.py.",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={
                            "path": "src/widget.py",
                            "content": "def load_widget_config():\n    return {'mode': 'safe'}\n",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(content=long_reply, tool_calls=[], raw={}),
        ]
    )
    session.client = repo_client  # type: ignore[assignment]

    try:
        first_exit = session.run_turn(long_request)
        chat_client = _ScriptedClient(
            [
                LLMResponse(
                    content="Recursion is a function that calls itself.",
                    tool_calls=[],
                    raw={},
                )
            ]
        )
        session.client = chat_client  # type: ignore[assignment]
        second_exit = session.run_turn("Explain recursion in Python in two lines.", chat_only=True)
        log_path = session.store.path
    finally:
        session.close()

    assert first_exit == 0
    assert second_exit == 0
    assert _final_contents(log_path)[-1] == "Recursion is a function that calls itself."
    assert chat_client.calls == 1
    request_messages = chat_client.call_records[0]["messages"]
    assert chat_client.call_records[0]["tools"] is None
    assert request_messages[-1] == {
        "role": "user",
        "content": "Explain recursion in Python in two lines.",
    }

    non_system_history = [
        msg for msg in request_messages[:-1] if msg.get("role") in {"user", "assistant"}
    ]
    assert 1 <= len(non_system_history) <= 2
    assert [msg.get("role") for msg in non_system_history] in (["user"], ["user", "assistant"])
    assert all(msg.get("role") != "tool" for msg in request_messages)
    assert not any(msg.get("tool_calls") for msg in request_messages if isinstance(msg, dict))
    assert not any(
        str(msg.get("content") or "").startswith(
            ("<task_brief>", "<environment_context>", "Repo summary")
        )
        for msg in request_messages
        if isinstance(msg, dict)
    )
    assert all(
        len(str(msg.get("content") or "")) <= _NON_REPO_MAX_RECENT_VISIBLE_HISTORY_CHARS
        for msg in non_system_history
    )
    assert (
        sum(len(str(msg.get("content") or "")) for msg in non_system_history)
        <= _NON_REPO_MAX_RECENT_VISIBLE_HISTORY_TOTAL_CHARS
    )
    assert estimate_message_tokens(non_system_history) < 1200


# ---------------------------------------------------------------------------
# Session provisioning
# ---------------------------------------------------------------------------


# Salvages: test_create_session_uses_role_temperatures_for_clients.
def test_create_session_uses_role_temperatures_without_provisioning_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_base_url = "https://route-user:route-password@api.example.test/private/token"
    cfg = AppConfig(
        model="test-model",
        base_url=secret_base_url,
        coding_temperature=0.23,
        compactor_temperature=0.31,
    )
    cfg.extra_fields = {
        "role_models": {"router": "unavailable-selector-model"},
        "compaction": {
            "enabled": True,
            "summarize_conversation": True,
        },
    }
    resolve_role = session_mod.resolve_model_for_role
    make_client = session_mod._make_session_llm_client
    provisioned_models: list[str] = []

    def resolve_active_role(**kwargs: Any) -> str:
        if kwargs["role"] == "router":
            raise AssertionError("unused selector model must not be resolved")
        return resolve_role(**kwargs)

    def record_client(**kwargs: Any) -> Any:
        provisioned_models.append(kwargs["model"])
        return make_client(**kwargs)

    monkeypatch.setattr(session_mod, "resolve_model_for_role", resolve_active_role)
    monkeypatch.setattr(session_mod, "_make_session_llm_client", record_client)
    session = _session(tmp_path, cfg=cfg)

    try:
        assert session.client.temperature == 0.23
        assert session.client.model == "test-model"
        assert session.router_client is None
        assert provisioned_models == ["test-model", "test-model"]
        assert session.client.provider_retry_settings.disable_retries is False
        session_start_payload = _event_payloads(session.store.path, "session_start")[0]
        assert "router_model" not in session_start_payload
        assert {item["role"] for item in session_start_payload["model_metadata_diagnostics"]} == {
            "coding",
            "compactor",
        }
        assert session_start_payload["base_url_descriptor"] == endpoint_descriptor(secret_base_url)
        assert session_start_payload["provider_base_url_descriptor"] == endpoint_descriptor(
            secret_base_url
        )
        assert "base_url" not in session_start_payload
        assert "provider_base_url" not in session_start_payload
        serialized_session_start = json.dumps(session_start_payload, sort_keys=True)
        assert "route-user" not in serialized_session_start
        assert "route-password" not in serialized_session_start
        assert "private/token" not in serialized_session_start
        assert session.conversation_compactor is not None
        assert session.conversation_compactor.compactor_client.temperature == 0.31
    finally:
        session.close()


@pytest.mark.parametrize(
    "cfg",
    [
        AppConfig(model="test-model"),
        AppConfig(model="test-model", skills_enabled=False),
        AppConfig(model="test-model", skills_auto_invoke=False),
    ],
)
def test_skill_settings_do_not_provision_selector_client(
    tmp_path: Path,
    cfg: AppConfig,
) -> None:
    session = _session(tmp_path, cfg=cfg)

    try:
        assert session.router_client is None
        assert not hasattr(session, "_semantic_router_bound_client")
        assert not hasattr(session, "_provisioned_router_client")
    finally:
        session.close()


def test_empty_skill_registry_does_not_provision_selector_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prompt_context_mod,
        "discover_skills",
        lambda **_kwargs: DiscoveredSkills(skills={}, ordered=(), issues=()),
    )
    session = _session(
        tmp_path,
        cfg=AppConfig(model="test-model", bundled_skills_enabled=False),
    )

    try:
        assert session.skills_ordered == ()
        assert session.router_client is None
    finally:
        session.close()


def test_provider_context_and_accounting_agree_and_ignore_auxiliary_calls(tmp_path):
    from alysis_code.llm.types import LLMUsage
    from alysis_code.provider_usage import restore_provider_usage, session_provider_usage

    session = _session(tmp_path)
    try:
        response = LLMResponse(
            content="done",
            tool_calls=[],
            raw={},
            usage=LLMUsage(4000, 4, 4004, cached_prompt_tokens=3000),
        )
        record = session._record_llm_usage(
            client=session.client,
            response=response,
            messages=[{"role": "user", "content": "x" * 4000}],
            tool_list=None,
            operation="main_llm",
        )
        assert record is not None
        reading = session_provider_usage(session)
        assert reading["input_tokens"] == 4000
        assert reading["cached_tokens"] == 3000
        assert record.prompt_tokens == reading["input_tokens"]
        assert record.cached_prompt_tokens == reading["cached_tokens"]
        assert record.total_tokens == reading["total_tokens"]
        assert record.usage_source == "api"
        for operation in ("compaction", "cache_keepalive", "subagent_llm"):
            session._record_llm_usage(
                client=session.client,
                response=LLMResponse(
                    content="done", tool_calls=[], raw={}, usage=LLMUsage(42, 4, 46)
                ),
                messages=[],
                tool_list=None,
                operation=operation,
            )
            assert session_provider_usage(session) == reading
        session._record_llm_usage(
            client=session.client,
            response=LLMResponse(content="done", tool_calls=[], raw={}),
            messages=[],
            tool_list=None,
            operation="main_llm",
        )
        assert session_provider_usage(session) == reading
        session.last_provider_usage = None
        restore_provider_usage(session, session.store.path)
        assert session_provider_usage(session) == reading
    finally:
        session.close()


def test_aggregate_input_usage_never_anchors_or_calibrates_context_after_resume(tmp_path):
    from dataclasses import replace

    from alysis_code.llm.types import LLMUsage
    from alysis_code.provider_usage import session_provider_usage
    from alysis_code.usage_tracker import UsageSummary

    session = _session(tmp_path)
    try:
        response = LLMResponse(content="done", tool_calls=[], raw={}, usage=LLMUsage(4000, 4, 4004))
        session._record_llm_usage(
            client=session.client,
            response=response,
            messages=list(session.messages),
            tool_list=session.tool_list,
            operation="main_llm",
        )
        assert session.request_context_measurement is not None
        summary = UsageSummary()
        for _ in range(3):
            record = session._record_llm_usage(
                client=session.client,
                response=replace(
                    response, usage=LLMUsage(90000, 400, 90400, input_tokens_scope="interaction")
                ),
                messages=list(session.messages),
                tool_list=session.tool_list,
                operation="main_llm",
            )
            assert record.prompt_tokens == 90000
            assert record.input_tokens_scope == "interaction"
            assert record.prompt_estimate_error_ratio is None
            summary.add_event_payload(record.to_payload())
        assert session.request_context_measurement is None
        assert session.context_left().token_count_source == "local_estimate"
        assert session_provider_usage(session)["input_scope"] == "interaction"
        assert summary.records()[0].input_tokens_scope == "interaction"
        snapshot = summary._calibration_snapshot_from_records(summary.records())
        assert snapshot["calibration_usable_calls"] == 0
        assert snapshot["calibration_excluded_aggregate_calls"] == 3
        assert summary.totals()["prompt_tokens"] == 270000
    finally:
        session.close()
