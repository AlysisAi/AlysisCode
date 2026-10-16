from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from ..error_text import sanitize_error_text_for_output


class LLMError(RuntimeError):
    """Provider/model error whose public message is safe to persist and display."""

    def __init__(self, message: object = "") -> None:
        super().__init__(sanitize_error_text_for_output(message))


class LLMStreamNoProgressError(LLMError):
    """A streaming attempt produced no meaningful provider payload in time."""


class LLMOutputLimitError(LLMError):
    """A terminal token limit with accounted usage and safe continuation state.

    Keep this an error for callers that require a complete response. The agent
    turn can explicitly recover using ``response`` instead of replaying the
    same provider request or treating partial output as a finished answer.
    """

    def __init__(self, response: LLMResponse) -> None:
        super().__init__(
            "The model reached its output token limit before finishing (max_output_tokens). "
            "Increase the response allowance, lower reasoning effort, or compact the conversation."
        )
        self.response = response


class UsageConfidence(StrEnum):
    AUTHORITATIVE = "authoritative"
    REPORTED = "reported"
    ESTIMATED = "estimated"


class UsageSource(StrEnum):
    PROVIDER_RESPONSE = "provider_response"
    PROVIDER_COUNT = "provider_count"
    LOCAL_ESTIMATE = "local_estimate"
    MIXED = "mixed"


class BillingMode(StrEnum):
    """How the selected route is paid for from the user's perspective."""

    METERED_API = "metered_api"
    SUBSCRIPTION = "subscription"
    INCLUDED = "included"
    LOCAL = "local"
    UNKNOWN = "unknown"


class CostSource(StrEnum):
    """Provenance of a per-call dollar amount."""

    PROVIDER_REPORTED = "provider_reported"
    CATALOG_ESTIMATE = "catalog_estimate"
    SUBSCRIPTION = "subscription"
    INCLUDED = "included"
    LOCAL = "local"
    UNKNOWN = "unknown"


class ReasoningOutputKind(StrEnum):
    """Visibility class for provider-produced reasoning output.

    ``SUMMARY`` is provider-generated explanatory text. ``PROVIDER_REASONING``
    is text returned through a provider's explicit reasoning channel. Both can
    be displayed by trace; encrypted state and signatures are metadata only.
    """

    SUMMARY = "summary"
    PROVIDER_REASONING = "provider_reasoning"


class AssistantResponsePhase(StrEnum):
    """Provider-declared lifecycle phase for assistant-visible output."""

    COMMENTARY = "commentary"
    FINAL_ANSWER = "final_answer"


@dataclass(frozen=True)
class ReasoningOutput:
    """Normalized reasoning material returned alongside an assistant answer."""

    text: str
    kind: ReasoningOutputKind
    provider: str | None = None


@dataclass(frozen=True)
class UsageContract:
    """Protocol-level guarantees after provider usage has been normalized."""

    response_usage_confidence: UsageConfidence = UsageConfidence.REPORTED
    normalized_output_includes_reasoning: bool = True
    input_token_count_strategy: str = "none"
    billing_mode: BillingMode = BillingMode.METERED_API

    @property
    def response_usage_authoritative(self) -> bool:
        return self.response_usage_confidence == UsageConfidence.AUTHORITATIVE

    @property
    def supports_input_token_count(self) -> bool:
        return self.input_token_count_strategy != "none"


@dataclass(frozen=True)
class InputTokenCount:
    input_tokens: int
    source: UsageSource = UsageSource.PROVIDER_COUNT
    confidence: UsageConfidence = UsageConfidence.AUTHORITATIVE
    raw_provider_usage: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.input_tokens, bool)
            or not isinstance(self.input_tokens, int)
            or self.input_tokens < 0
        ):
            raise ValueError("input token count must be a non-negative integer")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    provider_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_prompt_tokens: int | None = None
    input_tokens_uncached: int | None = None
    # True when the uncached figure was computed from other reported counts
    # rather than sent by the provider, so display can avoid claiming it as
    # provider-reported.
    input_tokens_uncached_derived: bool = False
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_creation_5m_input_tokens: int | None = None
    cache_creation_1h_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    provider_cost_usd: float | None = None
    provider_cost_currency: str | None = None
    raw_provider_usage: dict[str, Any] | None = None
    source: UsageSource = UsageSource.PROVIDER_RESPONSE
    confidence: UsageConfidence = UsageConfidence.REPORTED
    output_includes_reasoning: bool = True
    total_includes_reasoning: bool = True

    def normalized(self) -> LLMUsage:
        """Return the provider-independent invariant used by accounting.

        After normalization, completion/output and total include reasoning,
        while ``reasoning_tokens`` remains an informational subset.
        """
        reasoning = self.reasoning_tokens
        if reasoning is None or reasoning <= 0:
            return replace(
                self,
                output_includes_reasoning=True,
                total_includes_reasoning=True,
            )
        completion = self.completion_tokens
        total = self.total_tokens
        if completion is not None and not self.output_includes_reasoning:
            completion += reasoning
        if total is not None and not self.total_includes_reasoning:
            total += reasoning
        return replace(
            self,
            completion_tokens=completion,
            total_tokens=total,
            output_includes_reasoning=True,
            total_includes_reasoning=True,
        )

    @property
    def cache_read_tokens(self) -> int | None:
        return (
            self.cache_read_input_tokens
            if self.cache_read_input_tokens is not None
            else self.cached_prompt_tokens
        )


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: list[ToolCall]
    raw: dict[str, Any]
    response_model: str | None = None
    usage: LLMUsage | None = None
    provider_metadata: dict[str, Any] | None = None
    reasoning: tuple[ReasoningOutput, ...] = ()
    assistant_phase: AssistantResponsePhase | None = None
