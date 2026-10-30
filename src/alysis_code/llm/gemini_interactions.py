from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from ..branding import env_get
from ..error_text import sanitize_error_text_for_output
from ..provider_telemetry import ProviderCallTelemetryRecorder
from .base import input_token_count_request, scoped_input_token_counter
from .cancellable_http import provider_httpx_client
from .http_cancellation import cancellable_httpx_send, raise_if_cancelled
from .metadata import (
    GEMINI_INTERACTIONS_PROVIDER_METADATA_KEY,
    ProviderRouteIdentity,
    build_provider_route_identity,
    canonicalize_extra_headers,
    credential_scope_fingerprint,
    gate_messages_for_provider_route,
    merge_canonical_headers,
    stamp_response_for_route,
)
from .protocols import reasoning_trace_callback
from .provider_limits import (
    DEFAULT_PROVIDER_CONCURRENCY_CAPS,
    ProviderRetrySettings,
    best_effort_provider_key,
    run_provider_limited_call,
)
from .request_plan import LLMRequestPlan
from .request_shape import build_request_shape_report
from .temperature_compat import documented_temperature_omit_reason
from .types import (
    InputTokenCount,
    LLMError,
    LLMResponse,
    LLMUsage,
    ReasoningOutput,
    ReasoningOutputKind,
    UsageConfidence,
    UsageContract,
)

if TYPE_CHECKING:
    from ..config import AppConfig

GEMINI_INTERACTIONS_EXPERIMENT_ENV = "ALYSIS_EXPERIMENTAL_GEMINI_INTERACTIONS"
GEMINI_INTERACTIONS_CONFIG_FLAG = "experimental_gemini_interactions_enabled"
_INTERACTIONS_API_REVISION = "2026-05-20"
GEMINI_INTERACTIONS_ROUTE_REVISION = _INTERACTIONS_API_REVISION


def gemini_interactions_enabled(cfg: AppConfig | None = None) -> bool:
    """Return whether the experimental Gemini Interactions protocol is explicitly enabled."""
    env_value = str(env_get(GEMINI_INTERACTIONS_EXPERIMENT_ENV, "") or "").strip().lower()
    if env_value in {"1", "true", "yes", "on"}:
        return True
    if env_value in {"0", "false", "no", "off"}:
        return False
    return bool(getattr(cfg, GEMINI_INTERACTIONS_CONFIG_FLAG, False))


def gemini_interactions_disabled_message() -> str:
    return (
        "protocol='gemini_interactions' is experimental and disabled by default. "
        f"Set {GEMINI_INTERACTIONS_EXPERIMENT_ENV}=1 or run "
        f"`alysis config set {GEMINI_INTERACTIONS_CONFIG_FLAG} true` to enable the "
        "text-only prototype. Gemini GenerateContent remains the stable native Gemini protocol."
    )


class GeminiInteractionsClient:
    """Minimal text-only prototype for Google's beta Gemini Interactions API."""

    usage_contract = UsageContract(
        response_usage_confidence=UsageConfidence.AUTHORITATIVE,
        input_token_count_strategy="gemini_count_tokens_projection",
    )
    usage_counts_authoritative = usage_contract.response_usage_authoritative
    supports_tool_calling = False
    supports_forced_tool_choice = False

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
        temperature: float = 0.2,
        prompt_cache_key: str | None = None,
        prompt_cache_retention: str | None = None,
        prompt_cache_policy_metadata: Mapping[str, Any] | None = None,
        enable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
        transport: httpx.BaseTransport | None = None,
        extra_headers: dict[str, str] | None = None,
        provider_key: str | None = None,
        provider_concurrency_caps: dict[str, int] | None = None,
        provider_retry_settings: ProviderRetrySettings | None = None,
        provider_sleep_fn: Callable[[float], None] | None = None,
        provider_random_fn: Callable[[], float] | None = None,
        usage_contract: UsageContract | None = None,
        route_identity: ProviderRouteIdentity | None = None,
    ) -> None:
        self.base_url = _gemini_interactions_base_url(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.prompt_cache_key = str(prompt_cache_key or "").strip() or None
        self.prompt_cache_retention = str(prompt_cache_retention or "").strip() or None
        self.prompt_cache_policy_metadata = (
            copy.deepcopy(dict(prompt_cache_policy_metadata))
            if isinstance(prompt_cache_policy_metadata, Mapping)
            else None
        )
        self.enable_thinking = enable_thinking
        self.reasoning_effort = str(reasoning_effort or "").strip().lower() or None
        self._transport = transport
        self.extra_headers = canonicalize_extra_headers(extra_headers)
        self.provider_key = str(provider_key or "").strip() or None
        self.route_identity = route_identity or build_provider_route_identity(
            protocol="gemini_interactions",
            base_url=self.base_url,
            provider_key=self.provider_key,
            model=self.model,
            credential_scope=credential_scope_fingerprint(self.api_key),
            routing_headers=self.extra_headers,
            protocol_revision=GEMINI_INTERACTIONS_ROUTE_REVISION,
        )
        self.provider_concurrency_caps = dict(
            DEFAULT_PROVIDER_CONCURRENCY_CAPS
            if provider_concurrency_caps is None
            else provider_concurrency_caps
        )
        self.provider_retry_settings = provider_retry_settings or ProviderRetrySettings()
        self._provider_sleep_fn = provider_sleep_fn
        self._provider_random_fn = provider_random_fn
        self.usage_contract = usage_contract or type(self).usage_contract
        self.usage_counts_authoritative = self.usage_contract.response_usage_authoritative
        self._input_token_count_available: bool | None = None
        self._thinking_summaries_supported: bool | None = None

    def _headers(self) -> dict[str, str]:
        return merge_canonical_headers(
            {
                "Api-Revision": _INTERACTIONS_API_REVISION,
                "Content-Type": "application/json",
                "User-Agent": "alysis-code/0.1.0",
                "x-goog-api-key": self.api_key,
            },
            self.extra_headers,
        )

    @staticmethod
    def _llm_error_from_response(response: httpx.Response) -> LLMError:
        try:
            data = response.json()
        except Exception:
            body = response.text
            if len(body) > 1000:
                body = body[:1000] + "...(truncated)"
            return LLMError(
                sanitize_error_text_for_output(
                    f"Gemini Interactions error {response.status_code}: {body}"
                )
            )
        message = _extract_error_message(data)
        if message:
            return LLMError(
                sanitize_error_text_for_output(
                    f"Gemini Interactions error {response.status_code}: {message}"
                )
            )
        return LLMError(
            sanitize_error_text_for_output(
                f"Gemini Interactions error {response.status_code}: {data!r}"
            )
        )

    @scoped_input_token_counter
    def count_input_tokens(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
    ) -> InputTokenCount | None:
        if self._input_token_count_available is False or tools or tool_choice is not None:
            return None
        messages = gate_messages_for_provider_route(messages, self.route_identity)
        prompt = _last_user_text(messages)
        if not prompt:
            return None
        _validate_text_only_single_turn_history(messages)
        encoded_model = quote(self.model, safe="")
        url = f"{self.base_url}/models/{encoded_model}:countTokens"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }

        def _send_request() -> InputTokenCount | None:
            try:
                with provider_httpx_client(
                    timeout=self.timeout_s, transport=self._transport
                ) as client:
                    response = client.post(url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                raise LLMError(
                    "Gemini input token count request failed: "
                    f"{sanitize_error_text_for_output(exc)}"
                ) from exc
            if response.status_code in {404, 405, 501}:
                self._input_token_count_available = False
                return None
            if response.status_code >= 400:
                raise self._llm_error_from_response(response)
            try:
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                raise LLMError("Gemini input token count returned non-JSON response") from exc
            count = _int_or_none(data.get("totalTokens") if isinstance(data, dict) else None)
            if count is None or count < 0:
                raise LLMError("Gemini input token count response omitted totalTokens")
            self._input_token_count_available = True
            return InputTokenCount(
                input_tokens=count,
                confidence=UsageConfidence.REPORTED,
                raw_provider_usage=copy.deepcopy(data),
            )

        with input_token_count_request():
            return run_provider_limited_call(
                call=_send_request,
                provider_key=self.provider_key,
                provider_concurrency_caps=self.provider_concurrency_caps,
                retry_settings=self.provider_retry_settings,
                operation="gemini_interactions_count_input_tokens",
                sleep_fn=self._provider_sleep_fn,
                random_fn=self._provider_random_fn,
                retry_deadline_allows=getattr(self, "_provider_retry_deadline_allows", None),
            )

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        response_format: dict[str, Any] | None = None,
        stream: bool = False,
        on_text_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cancellation_token: Any | None = None,
    ) -> LLMResponse:
        del on_text_delta
        on_reasoning_delta = reasoning_trace_callback(
            on_reasoning_delta,
            capability=getattr(self, "reasoning_trace_capability", None),
            stream=stream,
        )
        messages = gate_messages_for_provider_route(messages, self.route_identity)
        if stream:
            raise LLMError("Gemini Interactions prototype does not support streaming yet")
        if tools:
            raise LLMError("Gemini Interactions prototype does not support tools yet")
        if tool_choice is not None:
            raise LLMError("Gemini Interactions prototype does not support tool_choice yet")
        if response_format is not None:
            raise LLMError("Gemini Interactions prototype does not support response_format yet")
        if self.prompt_cache_key or self.prompt_cache_retention:
            raise LLMError("Gemini Interactions does not support prompt_cache_key settings")
        if self.enable_thinking is not None or self.reasoning_effort:
            raise LLMError("Gemini Interactions prototype does not support thinking settings yet")

        prompt = _last_user_text(messages)
        if not prompt:
            raise LLMError("Gemini Interactions prototype requires a non-empty user message")
        _validate_text_only_single_turn_history(messages)

        payload: dict[str, Any] = {
            "input": prompt,
            "model": self.model,
        }
        temperature_omit_reason = documented_temperature_omit_reason(self.model)
        generation_config: dict[str, Any] = {}
        if temperature_omit_reason is None:
            if temperature is not None:
                generation_config["temperature"] = float(temperature)
            elif self.temperature is not None:
                generation_config["temperature"] = self.temperature
        if max_tokens is not None:
            generation_config["max_output_tokens"] = int(max_tokens)
        if on_reasoning_delta is not None and self._thinking_summaries_supported is not False:
            generation_config["thinking_summaries"] = "auto"
        if generation_config:
            payload["generation_config"] = generation_config
        original_payload = copy.deepcopy(payload)
        request_plan = LLMRequestPlan.from_chat_args(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        def _request_plan_metadata(
            current_payload: dict[str, Any],
            *,
            input_mode: str,
            fallback_used: bool = False,
        ) -> dict[str, Any]:
            extra: dict[str, Any] = {}
            if temperature_omit_reason is not None:
                extra.update(
                    {
                        "temperature_omitted": True,
                        "temperature_omit_reason": temperature_omit_reason,
                    }
                )
            if fallback_used:
                extra["fallback_used"] = True
            return request_plan.request_plan_metadata(
                input_mode=input_mode,
                continuation_strategy="full_replay",
                provider_payload=original_payload,
                sent_provider_payload=current_payload,
                cache_policy_metadata=self.prompt_cache_policy_metadata,
                extra=extra or None,
            )

        request_plan_metadata = _request_plan_metadata(payload, input_mode="full")

        provider_key = self.provider_key or best_effort_provider_key(
            base_url=self.base_url,
            model=self.model,
        )
        telemetry = ProviderCallTelemetryRecorder(
            provider_key=provider_key,
            protocol="gemini_interactions",
            model=self.model,
            base_url=self.base_url,
            stream=False,
            tools=tools,
            web_search_mode="off",
            web_search_adapter="gemini_grounding",
            native_web_search=False,
            cache_policy=self.prompt_cache_policy_metadata,
            request_plan=request_plan_metadata,
            request_shape=build_request_shape_report(
                messages=messages,
                tools=tools,
                cache_policy=self.prompt_cache_policy_metadata,
                provider_payload=payload,
            ),
            operation="gemini_interactions_chat",
        )
        telemetry_on_reasoning_delta = telemetry.wrap_reasoning_delta(on_reasoning_delta)

        def _send_request() -> LLMResponse:
            nonlocal request_plan_metadata
            thinking_summaries_fallback_used = False
            try:
                with provider_httpx_client(
                    timeout=self.timeout_s, transport=self._transport
                ) as client:
                    while True:
                        response = cancellable_httpx_send(
                            client=client,
                            cancellation_token=cancellation_token,
                            method="POST",
                            url=f"{self.base_url}/interactions",
                            headers=self._headers(),
                            json=payload,
                        )
                        if response.status_code < 400:
                            break
                        if (
                            not thinking_summaries_fallback_used
                            and _thinking_summaries_request_rejected(response)
                            and _remove_thinking_summaries_request(payload)
                        ):
                            thinking_summaries_fallback_used = True
                            self._thinking_summaries_supported = False
                            request_plan_metadata = _request_plan_metadata(
                                payload,
                                input_mode="retry_without_thinking_summaries",
                                fallback_used=True,
                            )
                            telemetry.set_request_plan(request_plan_metadata)
                            telemetry.set_request_shape(
                                build_request_shape_report(
                                    messages=messages,
                                    tools=tools,
                                    cache_policy=self.prompt_cache_policy_metadata,
                                    provider_payload=payload,
                                )
                            )
                            continue
                        raise self._llm_error_from_response(response)
            except Exception as exc:  # noqa: BLE001
                raise_if_cancelled(cancellation_token)
                if isinstance(exc, LLMError):
                    raise
                raise LLMError(
                    f"Gemini Interactions request failed: {sanitize_error_text_for_output(exc)}"
                ) from exc
            return self._parse_chat_response(
                response,
                request_plan_metadata=request_plan_metadata,
                on_reasoning_delta=telemetry_on_reasoning_delta,
            )

        return stamp_response_for_route(
            telemetry.run(
                lambda: run_provider_limited_call(
                    call=_send_request,
                    provider_key=provider_key,
                    provider_concurrency_caps=self.provider_concurrency_caps,
                    retry_settings=self.provider_retry_settings,
                    operation="gemini_interactions_chat",
                    sleep_fn=self._provider_sleep_fn,
                    random_fn=self._provider_random_fn,
                    on_retry=telemetry.on_retry,
                    on_retry_event=getattr(
                        self,
                        "_provider_retry_event_observer",
                        None,
                    ),
                    retry_deadline_allows=getattr(self, "_provider_retry_deadline_allows", None),
                    cancellation_token=cancellation_token,
                )
            ),
            self.route_identity,
        )

    def _parse_chat_response(
        self,
        response: httpx.Response,
        request_plan_metadata: dict[str, Any] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        try:
            data = response.json()
        except Exception as exc:
            raise LLMError(f"Gemini Interactions returned invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMError("Gemini Interactions returned non-object JSON")
        text = _interaction_output_text(data).strip()
        reasoning = _interaction_thought_summaries(data)
        actions = data.get("actions")
        if isinstance(actions, list) and actions:
            raise LLMError(
                "Gemini Interactions returned actions, but this prototype only supports text output"
            )
        metadata: dict[str, Any] = {}
        interaction_id = data.get("id") or data.get("interaction_id") or data.get("interactionId")
        if isinstance(interaction_id, str) and interaction_id:
            metadata["interaction_id"] = interaction_id
        response_payload = data.get("response")
        if isinstance(response_payload, dict):
            metadata["response"] = copy.deepcopy(response_payload)
        if request_plan_metadata:
            metadata["request_plan"] = copy.deepcopy(request_plan_metadata)

        parsed_response = LLMResponse(
            content=text,
            tool_calls=[],
            raw=data,
            response_model=_response_model(data) or self.model,
            usage=_usage_from_response(data),
            provider_metadata=(
                {GEMINI_INTERACTIONS_PROVIDER_METADATA_KEY: metadata} if metadata else None
            ),
            reasoning=reasoning,
        )
        if on_reasoning_delta is not None:
            for item in reasoning:
                on_reasoning_delta(item.text)
        return parsed_response


def _thinking_summaries_request_rejected(response: httpx.Response) -> bool:
    if response.status_code not in {400, 422}:
        return False
    detail = response.text.casefold()
    names_summary_field = any(
        marker in detail for marker in ("thinking_summaries", "thinking summaries")
    )
    rejects_field = any(
        marker in detail
        for marker in (
            "unsupported",
            "not supported",
            "unknown field",
            "unknown name",
            "unrecognized",
            "invalid field",
            "invalid argument",
        )
    )
    return names_summary_field and rejects_field


def _remove_thinking_summaries_request(payload: dict[str, Any]) -> bool:
    generation_config = payload.get("generation_config")
    if not isinstance(generation_config, dict) or "thinking_summaries" not in generation_config:
        return False
    generation_config.pop("thinking_summaries", None)
    if not generation_config:
        payload.pop("generation_config", None)
    return True


def _gemini_interactions_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if normalized.endswith("/openai"):
        normalized = normalized.removesuffix("/openai")
    return normalized or "https://generativelanguage.googleapis.com/v1beta"


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip() != "user":
            continue
        text = _content_text(message.get("content"))
        if text.strip():
            return text.strip()
    return ""


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_content_text(item) for item in value)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        return text if isinstance(text, str) else ""
    return str(value)


def _interaction_output_text(data: dict[str, Any]) -> str:
    step_text = _interaction_model_output_text(data)
    if step_text:
        return step_text
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text
    output = data.get("output")
    if isinstance(output, str):
        return output
    text = _collect_text(output)
    if text:
        return text
    text = _collect_text(data.get("outputs"))
    if text:
        return text
    return _collect_text(data.get("response"))


def _interaction_model_output_text(data: dict[str, Any]) -> str:
    steps = data.get("steps")
    if not isinstance(steps, list):
        return ""
    parts: list[str] = []
    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _interaction_thought_summaries(data: dict[str, Any]) -> tuple[ReasoningOutput, ...]:
    steps = data.get("steps")
    if not isinstance(steps, list):
        return ()
    summaries: list[ReasoningOutput] = []
    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "thought":
            continue
        summary = step.get("summary")
        if not isinstance(summary, list):
            continue
        for block in summary:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                summaries.append(
                    ReasoningOutput(
                        text=text,
                        kind=ReasoningOutputKind.SUMMARY,
                        provider="gemini",
                    )
                )
    return tuple(summaries)


def _collect_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_collect_text(item) for item in value)
    if isinstance(value, dict):
        item_type = str(value.get("type") or "").strip().lower()
        if item_type in {"thought", "thought_summary", "thought_signature"}:
            return ""
        parts: list[str] = []
        text = value.get("text")
        if isinstance(text, str):
            parts.append(text)
        for key in ("content", "parts", "candidates", "output", "response"):
            child = value.get(key)
            if child is not None:
                parts.append(_collect_text(child))
        return "".join(parts)
    return ""


def _usage_from_response(data: dict[str, Any]) -> LLMUsage | None:
    usage = data.get("usageMetadata") or data.get("usage_metadata") or data.get("usage")
    if not isinstance(usage, dict):
        return None

    def first_count(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    prompt_tokens = first_count(
        "promptTokenCount", "prompt_tokens", "input_tokens", "total_input_tokens"
    )
    completion_tokens = first_count(
        "candidatesTokenCount", "completion_tokens", "output_tokens", "total_output_tokens"
    )
    tool_use_prompt_tokens = first_count(
        "toolUsePromptTokenCount",
        "tool_use_prompt_token_count",
        "tool_use_prompt_tokens",
        "total_tool_use_tokens",
    )
    thoughts_tokens = first_count(
        "thoughtsTokenCount", "thoughts_token_count", "thinking_tokens", "total_thought_tokens"
    )
    total_tokens = first_count("totalTokenCount", "total_tokens")
    cached_tokens = first_count(
        "cachedContentTokenCount",
        "cached_content_token_count",
        "cached_prompt_tokens",
        "total_cached_tokens",
    )
    # Gemini reports tool-use prompts separately from the ordinary prompt and
    # candidates separately from thinking. Fold them into their billing sides;
    # the untouched provider payload retains the original breakdown.
    if tool_use_prompt_tokens:
        prompt_tokens = (prompt_tokens or 0) + tool_use_prompt_tokens
    if thoughts_tokens:
        completion_tokens = (completion_tokens or 0) + thoughts_tokens
    if (
        prompt_tokens is None
        and completion_tokens is None
        and total_tokens is None
        and cached_tokens is None
    ):
        return None
    input_tokens_uncached = None
    if prompt_tokens is not None and cached_tokens is not None and prompt_tokens >= cached_tokens:
        input_tokens_uncached = prompt_tokens - cached_tokens
    return LLMUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cached_prompt_tokens=cached_tokens,
        input_tokens_uncached=input_tokens_uncached,
        input_tokens_uncached_derived=input_tokens_uncached is not None,
        input_tokens_scope="interaction",
        cache_read_input_tokens=cached_tokens,
        reasoning_tokens=thoughts_tokens,
        raw_provider_usage=copy.deepcopy(usage),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _response_model(data: dict[str, Any]) -> str | None:
    for key in ("modelVersion", "model_version", "model"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    response = data.get("response")
    if isinstance(response, dict):
        for key in ("modelVersion", "model_version", "model"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _validate_text_only_single_turn_history(messages: list[dict[str, Any]]) -> None:
    user_message_count = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        if role in {"system", "developer"}:
            raise LLMError(
                "Gemini Interactions prototype does not support system/developer instructions yet"
            )
        if role == "user":
            user_message_count += 1
            continue
        raise LLMError(
            "Gemini Interactions prototype supports only a single text-only user turn; "
            "server-side previous_interaction_id continuation is not implemented yet"
        )
    if user_message_count != 1:
        raise LLMError(
            "Gemini Interactions prototype supports exactly one user message until "
            "previous_interaction_id continuation is implemented"
        )


def _extract_error_message(data: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return ""
