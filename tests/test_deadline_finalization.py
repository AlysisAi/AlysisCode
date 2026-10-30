from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.session_store import read_session_events


class _FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _CapturingClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = tools, stream, on_text_delta, temperature
        self.calls.append({"messages": list(messages)})
        return LLMResponse(content="Final answer.", tool_calls=[], raw={})


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def test_finalization_window_adds_single_materialization_directive(tmp_path: Path) -> None:
    clock = _FakeClock(10.0)
    deadline = ExecutionDeadline.from_duration(4.0, clock=clock, source="explicit_cli")
    clock.advance(3.2)
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        execution_deadline=deadline,
        enable_compaction=False,
        verification_enabled=False,
    )
    client = _CapturingClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Summarize this repository state.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 1
    system_messages = [
        str(message.get("content") or "")
        for message in client.calls[0]["messages"]
        if message.get("role") == "system"
    ]
    finalization_messages = [
        message
        for message in system_messages
        if "Run deadline finalization window is active." in message
    ]
    assert len(finalization_messages) == 1
    assert "Materialize the best valid result now." in finalization_messages[0]
    assert _event_payloads(log_path, "deadline_finalization_started")
    directive_events = _event_payloads(log_path, "deadline_finalization_directive")
    assert len(directive_events) == 1
    assert directive_events[0]["deadline"]["phase"] == "finalization_window"


def test_local_compaction_check_cannot_inflate_llm_reserve_and_block_first_request(
    tmp_path, monkeypatch
):
    from alysis_code.agent.turn import core

    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(12.75, clock=clock, source="explicit_cli")
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        execution_deadline=deadline,
        enable_compaction=True,
        verification_enabled=False,
    )
    client = _CapturingClient()
    session.client = client
    assert session.conversation_compactor is not None

    def local_check(**kwargs):
        clock.advance(1.14)
        return kwargs["messages"], False

    monkeypatch.setattr(session.conversation_compactor, "maybe_compact", local_check)
    monkeypatch.setattr(core, "perf_counter", clock)
    clock.advance(9.709108)
    try:
        assert session.run_turn("Summarize this repository state.") == 0
        assert len(client.calls) == 1
        assert "compaction_llm" not in deadline.telemetry_snapshot()["duration_observations"]
        assert deadline.finalization_reserve_seconds() == 1.0
    finally:
        session.close()


@pytest.mark.parametrize("measure_local_input", [False, True])
def test_noop_compaction_preserves_eligible_finalization_request(
    tmp_path, monkeypatch, measure_local_input
):
    from alysis_code.compaction import conversation_compactor
    from alysis_code.execution_deadline import DeadlineOperation, DeadlinePhase

    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(20.0, clock=clock, source="explicit_cli")
    deadline.observe_duration(DeadlineOperation.MAIN_LLM, 2.0)
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        execution_deadline=deadline,
        enable_compaction=True,
        verification_enabled=False,
    )
    client = _CapturingClient()
    client.timeout_s = 60.0
    session.client = client
    compactor = session.conversation_compactor
    assert compactor is not None
    if measure_local_input:
        # Media requires counting even below target. The real compatible
        # client's counter estimates locally and must not need network budget.
        monkeypatch.setattr(conversation_compactor, "request_contains_media", lambda messages: True)
    monkeypatch.setattr(
        compactor.compactor_client, "chat", lambda **kwargs: pytest.fail("No compaction needed")
    )
    clock.advance(18.0)
    assert deadline.phase() == DeadlinePhase.FINALIZATION_WINDOW
    assert not deadline.start_decision(
        DeadlineOperation.COMPACTION_LLM, minimum_remaining_seconds=0.25
    ).allowed
    try:
        assert session.run_turn("Summarize this repository state.") == 0
        assert len(client.calls) == 1
        assert _event_payloads(session.store.path, "compaction_check")
        if measure_local_input:
            assert (
                _event_payloads(session.store.path, "compaction_check")[0][
                    "input_measurement_tokens"
                ]
                is not None
            )
        assert not _event_payloads(session.store.path, "deadline_operation_blocked")
        assert client.timeout_s == 60.0
        assert "compaction_llm" not in deadline.telemetry_snapshot()["duration_observations"]
    finally:
        session.close()


@pytest.mark.parametrize("operation", ["summary", "importance"])
@pytest.mark.parametrize("remaining,provider_elapsed", [(2.0, 0.5), (15.0, 0.5), (15.0, 13.0)])
def test_turn_guards_actual_compaction_requests(
    tmp_path, monkeypatch, operation, remaining, provider_elapsed
):
    from alysis_code.execution_deadline import DeadlineOperation

    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(20.0, clock=clock)
    deadline.observe_duration(DeadlineOperation.MAIN_LLM, 2.0)
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=False)
    cfg.extra_fields["compaction"] = {"importance_use_llm": True}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        execution_deadline=deadline,
        enable_compaction=True,
        verification_enabled=False,
    )
    compactor = session.conversation_compactor
    assert compactor is not None
    client = _CapturingClient()
    client.timeout_s = 60.0
    session.client = client
    compactor.compactor_client.timeout_s = 60.0
    missing = object()
    original_hooks = {
        name: getattr(compactor.compactor_client, name, missing)
        for name in (
            "_provider_retry_deadline_allows",
            "_stream_deadline_exhausted",
            "_run_deadline_limited_timeout_elapsed",
        )
    }
    observed_timeouts = []

    def chat(**kwargs):
        observed_timeouts.append(compactor.compactor_client.timeout_s)
        clock.advance(provider_elapsed)
        return LLMResponse(content='{"scores":[],"goal":"retained"}', tool_calls=[], raw={})

    def plan(**kwargs):
        # A planner that requires a request must use the same guarded provider
        # boundary as the real summary/importance implementation.
        for _ in range(2 if provider_elapsed == 13.0 else 1):
            if operation == "summary":
                compactor._call_compactor(prompt_messages=[{"role": "user", "content": "x"}])
            else:
                compactor._apply_llm_importance_overrides([])
        return kwargs["messages"], False

    monkeypatch.setattr(compactor.compactor_client, "chat", chat)
    monkeypatch.setattr(compactor, "maybe_compact", plan)
    clock.advance(20.0 - remaining)
    try:
        session.run_turn("Summarize this repository state.")
        if remaining == 2.0:
            assert observed_timeouts == []
            assert client.calls == []
            blocks = _event_payloads(session.store.path, "deadline_operation_blocked")
            assert [event["operation"] for event in blocks] == ["compaction_llm"]
            assert "compaction_llm" not in deadline.telemetry_snapshot()["duration_observations"]
        else:
            assert len(observed_timeouts) == 1
            assert 0 < observed_timeouts[0] < remaining
            assert len(client.calls) == (0 if provider_elapsed == 13.0 else 1)
            if provider_elapsed == 13.0:
                blocks = _event_payloads(session.store.path, "deadline_operation_blocked")
                assert [event["operation"] for event in blocks] == ["compaction_llm"]
        assert compactor.compactor_client.timeout_s == 60.0
        assert client.timeout_s == 60.0
        assert {
            name: getattr(compactor.compactor_client, name, missing) for name in original_hooks
        } == original_hooks
    finally:
        session.close()


@pytest.mark.parametrize("operation", ["preflight", "overflow_verification", "usage"])
@pytest.mark.parametrize("signal", ["deadline", "cancellation"])
def test_compaction_token_count_cannot_bypass_policy_or_swallow_stop(
    tmp_path, monkeypatch, operation, signal
):
    from contextlib import contextmanager

    from alysis_code.cancellation import CooperativeCancellationError
    from alysis_code.compaction import conversation_compactor
    from alysis_code.compaction.call_policy import compaction_provider_policy
    from alysis_code.execution_deadline import DeadlineExhausted
    from alysis_code.llm.types import UsageContract

    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=True,
        verification_enabled=False,
    )
    compactor = session.conversation_compactor
    assert compactor is not None
    expected_client = compactor.compactor_client if operation == "usage" else session.client
    monkeypatch.setattr(
        expected_client, "usage_contract", UsageContract(input_token_count_strategy="provider")
    )
    monkeypatch.setattr(
        expected_client,
        "count_input_tokens",
        lambda **kwargs: pytest.fail("Denied provider counter must not run"),
        raising=False,
    )
    failure = (
        DeadlineExhausted("synthetic budget stop")
        if signal == "deadline"
        else CooperativeCancellationError("synthetic cancellation")
    )
    guarded_clients = []

    @contextmanager
    def deny(client):
        guarded_clients.append(client)
        raise failure
        yield  # pragma: no cover - context manager protocol

    kwargs = dict(messages=session.messages, tool_list=None, main_model=session.client.model)
    try:
        with pytest.raises(type(failure)) as caught, compaction_provider_policy(deny):
            if operation == "usage":
                compactor._record_compactor_usage(
                    response=LLMResponse(content="x", tool_calls=[], raw={}),
                    messages=session.messages,
                    operation="compactor_llm",
                )
            elif operation == "overflow_verification":
                compactor.request_fits_input_budget(**kwargs)
            else:
                monkeypatch.setattr(
                    conversation_compactor, "request_contains_media", lambda messages: True
                )
                compactor.maybe_compact(**kwargs)
        assert caught.value is failure
        assert guarded_clients == [expected_client]
        assert not _event_payloads(session.store.path, "compaction_warning")
    finally:
        session.close()


def test_compaction_policy_skips_unsupported_count_and_restores_after_cancellation():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from alysis_code.compaction.call_policy import (
        compaction_provider_call,
        compaction_provider_policy,
    )
    from alysis_code.llm.base import count_input_tokens_if_supported
    from alysis_code.llm.types import UsageContract

    calls = []
    client = SimpleNamespace(usage_contract=UsageContract())

    @contextmanager
    def outer(client):
        calls.append("outer")
        yield

    @contextmanager
    def inner(client):
        calls.append("inner")
        raise KeyboardInterrupt
        yield  # pragma: no cover

    with compaction_provider_policy(outer):
        with pytest.raises(KeyboardInterrupt), compaction_provider_policy(inner):
            assert (
                count_input_tokens_if_supported(
                    client=client,
                    messages=[],
                    request_context=lambda: compaction_provider_call(client),
                )
                is None
            )
            with compaction_provider_call(client):
                pytest.fail("Cancelled request must not start")
        with compaction_provider_call(client):
            pass
    with compaction_provider_call(client):
        pass
    assert calls == ["inner", "outer"]


@pytest.mark.parametrize("protocol", ["responses", "anthropic", "gemini", "interactions"])
@pytest.mark.parametrize("mode", ["disabled", "denied", "admitted"])
def test_native_count_guards_network_but_skips_disabled_endpoint(protocol, mode):
    from contextlib import contextmanager

    import httpx

    from alysis_code.execution_deadline import DeadlineExhausted, temporarily_clamp_client_timeout
    from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
    from alysis_code.llm.base import count_input_tokens_if_supported
    from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
    from alysis_code.llm.gemini_interactions import GeminiInteractionsClient
    from alysis_code.llm.openai_responses import OpenAIResponsesClient

    cls = {
        "responses": OpenAIResponsesClient,
        "anthropic": AnthropicMessagesClient,
        "gemini": GeminiGenerateContentClient,
        "interactions": GeminiInteractionsClient,
    }[protocol]
    request_timeouts = []

    def transport(request):
        request_timeouts.append(client.timeout_s)
        return httpx.Response(200, json={"input_tokens": 10, "totalTokens": 10})

    client = cls(
        base_url="https://offline.invalid/v1",
        api_key="synthetic",
        model="test-model",
        transport=httpx.MockTransport(transport),
    )
    client._input_token_count_available = mode != "disabled"
    client.timeout_s = 60.0
    missing = object()
    original_hooks = {
        name: getattr(client, name, missing)
        for name in (
            "_provider_retry_deadline_allows",
            "_stream_deadline_exhausted",
            "_run_deadline_limited_timeout_elapsed",
        )
    }
    calls = []
    deadline = ExecutionDeadline.from_duration(20.0, clock=_FakeClock())

    @contextmanager
    def request_context():
        calls.append("request")
        if mode != "admitted":
            raise DeadlineExhausted("synthetic budget stop")
        with temporarily_clamp_client_timeout(client, deadline, operation="compaction_llm"):
            yield

    kwargs = dict(
        client=client,
        messages=[{"role": "user", "content": "hello"}],
        request_context=request_context,
    )
    if mode == "denied":
        with pytest.raises(DeadlineExhausted, match="synthetic budget stop"):
            count_input_tokens_if_supported(**kwargs)
        assert calls == ["request"]
    elif mode == "admitted":
        assert count_input_tokens_if_supported(**kwargs).input_tokens == 10
        assert calls == ["request"]
        assert len(request_timeouts) == 1
        assert 0 < request_timeouts[0] < 20.0
    else:
        assert count_input_tokens_if_supported(**kwargs) is None
        assert calls == []
    if mode != "admitted":
        assert request_timeouts == []
    assert client.timeout_s == 60.0
    assert {name: getattr(client, name, missing) for name in original_hooks} == original_hooks


def test_token_count_request_scope_restores_after_nested_cancellation():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from alysis_code.llm.base import (
        count_input_tokens_if_supported,
        input_token_count_request,
        scoped_input_token_counter,
    )
    from alysis_code.llm.types import InputTokenCount, UsageContract

    calls = []

    @contextmanager
    def outer():
        calls.append("outer")
        yield

    @contextmanager
    def inner():
        calls.append("inner")
        raise KeyboardInterrupt
        yield  # pragma: no cover

    @scoped_input_token_counter
    def nested(**kwargs):
        with input_token_count_request():
            return InputTokenCount(input_tokens=1)

    inner_client = SimpleNamespace(
        usage_contract=UsageContract(input_token_count_strategy="synthetic"),
        count_input_tokens=nested,
    )

    @scoped_input_token_counter
    def counter(**kwargs):
        with pytest.raises(KeyboardInterrupt):
            count_input_tokens_if_supported(client=inner_client, messages=[], request_context=inner)
        with input_token_count_request():
            return InputTokenCount(input_tokens=2)

    client = SimpleNamespace(usage_contract=inner_client.usage_contract, count_input_tokens=counter)
    assert count_input_tokens_if_supported(
        client=client, messages=[], request_context=outer
    ) == InputTokenCount(input_tokens=2)
    with input_token_count_request():
        pass
    assert calls == ["inner", "outer"]


def test_deadline_expiring_in_local_compaction_preflight_still_stops_main_request(
    tmp_path, monkeypatch
):
    from alysis_code.compaction import conversation_compactor

    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(20.0, clock=clock)
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        execution_deadline=deadline,
        enable_compaction=True,
        verification_enabled=False,
    )
    original = conversation_compactor.estimate_request_token_breakdown

    def slow_local_estimate(*args, **kwargs):
        clock.advance(21.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        conversation_compactor, "estimate_request_token_breakdown", slow_local_estimate
    )
    client = _CapturingClient()
    client.timeout_s = 60.0
    session.client = client
    try:
        session.run_turn("Summarize this repository state.")
        assert client.calls == []
        assert _event_payloads(session.store.path, "compaction_check")
        assert deadline.is_exhausted()
    finally:
        session.close()


@pytest.mark.parametrize("operation", ["summary", "importance"])
@pytest.mark.parametrize("provider_fails", [False, True])
def test_compactor_measures_actual_chat_even_on_failure(
    tmp_path, monkeypatch, operation, provider_fails
):
    from alysis_code.compaction import call_observation
    from alysis_code.llm.openai_compat import LLMError

    clock = _FakeClock(0.0)
    monkeypatch.setattr(call_observation, "perf_counter", clock)
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=False)
    cfg.extra_fields["compaction"] = {"enabled": True, "importance_use_llm": True}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=True,
        verification_enabled=False,
    )
    compactor = session.conversation_compactor
    assert compactor is not None
    calls = []

    def chat(**kwargs):
        calls.append(kwargs)
        clock.advance(0.5)
        if provider_fails:
            raise LLMError("synthetic provider failure")
        return LLMResponse(content='{"scores":[],"goal":"retained"}', tool_calls=[], raw={})

    monkeypatch.setattr(compactor.compactor_client, "chat", chat)
    monkeypatch.setattr(compactor, "_record_compactor_usage", lambda **kwargs: clock.advance(3.0))
    durations = []
    try:
        with call_observation.observe_compactor_llm_durations(durations.append):
            clock.advance(2.0)  # Local preparation must not be counted as model latency.
            if operation == "summary":
                result = compactor._call_compactor(
                    prompt_messages=[{"role": "user", "content": "x"}]
                )
                assert (result is None) == provider_fails
            else:
                assert compactor._apply_llm_importance_overrides([]) == []
            clock.advance(4.0)
        assert len(calls) == 1
        assert durations == [0.5]
    finally:
        session.close()


def test_compactor_observation_restores_nested_scope_after_cancellation(monkeypatch):
    from alysis_code.compaction import call_observation

    clock = _FakeClock(0.0)
    monkeypatch.setattr(call_observation, "perf_counter", clock)
    outer, inner = [], []
    with call_observation.observe_compactor_llm_durations(outer.append):
        with pytest.raises(KeyboardInterrupt):
            with call_observation.observe_compactor_llm_durations(inner.append):
                with call_observation.measure_compactor_llm_call():
                    clock.advance(0.5)
                    raise KeyboardInterrupt
        with call_observation.measure_compactor_llm_call():
            clock.advance(1.0)
    with call_observation.measure_compactor_llm_call():
        clock.advance(2.0)
    assert inner == [0.5]
    assert outer == [1.0]


def test_compactor_observer_failure_does_not_replace_provider_cancellation():
    from alysis_code.compaction import call_observation

    def broken_observer(duration):
        raise RuntimeError("synthetic telemetry failure")

    cancellation = KeyboardInterrupt("synthetic provider cancellation")
    with pytest.raises(KeyboardInterrupt) as caught:
        with call_observation.observe_compactor_llm_durations(broken_observer):
            with call_observation.measure_compactor_llm_call():
                raise cancellation
    assert caught.value is cancellation


def test_one_shot_without_deadline_emits_unconfigured_deadline_event(tmp_path: Path) -> None:
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        one_shot_execution=True,
        enable_compaction=False,
        verification_enabled=False,
    )
    client = _CapturingClient()
    session.client = client  # type: ignore[assignment]

    try:
        session.run_turn("Summarize this repository state.")
        log_path = session.store.path
    finally:
        session.close()

    events = _event_payloads(log_path, "run_deadline_unconfigured")
    assert len(events) == 1
    assert events[0]["deadline_config_source"] == "absent"
    assert events[0]["runtime_kind"] == "one_shot"
