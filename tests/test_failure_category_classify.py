"""Regression tests for the shared failure-category classifier.

These lock the observability-spine contract: an arbitrary exception is mapped onto
exactly one real :class:`FailureCategory` (never the old ``"llm_error"`` literal), so
the diagnostic vocabulary joins across the chat/run path, Forge workers, and delegated
agents. A readable-but-permanent provider status (auth / bad-request / model) is
reported as ``PROVIDER_ERROR`` and kept distinct from transient outages and from
genuine agent-implementation failures.
"""

from __future__ import annotations

import pytest

from alysis_code.failure_category import (
    FailureCategory,
    classify_failure_category,
    exit_code_for_failure,
    extract_status_code,
    is_context_window_exceeded_error,
)
from alysis_code.run_outcome import (
    AGENT_FAILURE_EXIT_CODE,
    INFRASTRUCTURE_FAILURE_EXIT_CODE,
    RunOutcome,
    extract_process_exit_code,
    run_outcome_for_exit_code,
)


class _StatusError(Exception):
    """Exception exposing an HTTP ``status_code`` like the provider client errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        # Throttling: explicit 429 or rate-limit language.
        (_StatusError("Too Many Requests", status_code=429), FailureCategory.PROVIDER_THROTTLED),
        (Exception("rate limit exceeded, slow down"), FailureCategory.PROVIDER_THROTTLED),
        # Infra: sandbox/docker backend unavailable.
        (
            Exception("Cannot connect to the Docker daemon at /var/run/docker.sock"),
            FailureCategory.INFRA_UNAVAILABLE,
        ),
        # Transient provider outage: retryable status or network language.
        (
            _StatusError("Service Unavailable", status_code=503),
            FailureCategory.PROVIDER_UNAVAILABLE,
        ),
        (
            _StatusError("Internal Server Error", status_code=500),
            FailureCategory.PROVIDER_UNAVAILABLE,
        ),
        (
            _StatusError("Provider extension error", status_code=599),
            FailureCategory.PROVIDER_UNAVAILABLE,
        ),
        (Exception("connection refused"), FailureCategory.PROVIDER_UNAVAILABLE),
        (Exception("read operation timed out"), FailureCategory.PROVIDER_UNAVAILABLE),
        # Permanent provider rejection: auth / bad request / unknown model.
        (_StatusError("Unauthorized", status_code=401), FailureCategory.PROVIDER_ERROR),
        (
            _StatusError("Param Incorrect: Not supported model MiMo-V2-Pro", status_code=400),
            FailureCategory.PROVIDER_ERROR,
        ),
        # The reproducible DeepSeek thinking-mode tool_choice 400 (status read from the message).
        (
            Exception(
                'LLM error 400: {"error":{"message":"Thinking mode does not support this tool_choice"}}'
            ),
            FailureCategory.PROVIDER_ERROR,
        ),
        # No provider signal at all -> a genuine agent-side failure.
        (ValueError("boom"), FailureCategory.IMPLEMENTATION_FAILED),
    ],
)
def test_classify_failure_category(error: Exception, expected: FailureCategory) -> None:
    assert classify_failure_category(error) is expected


def test_classify_missing_or_blank_signal_defaults_to_implementation_failed() -> None:
    # The swarm worker resolves its generic failure default through this
    # classifier; absent or content-free signals must stay implementation-side.
    assert classify_failure_category(None) is FailureCategory.IMPLEMENTATION_FAILED
    assert classify_failure_category("") is FailureCategory.IMPLEMENTATION_FAILED
    assert classify_failure_category("   ") is FailureCategory.IMPLEMENTATION_FAILED


def test_classify_never_returns_legacy_literal() -> None:
    # The old hardcoded "llm_error" string is not a real category and must never appear.
    category = classify_failure_category(Exception("LLM error 400: bad request"))
    assert category.value != "llm_error"
    assert category.value in {member.value for member in FailureCategory}


def test_extract_status_code_from_message_and_attribute() -> None:
    assert extract_status_code(_StatusError("nope", status_code=404)) == 404
    assert extract_status_code(Exception("LLM error 404: not found")) == 404
    assert extract_status_code(ValueError("no status here")) is None


def test_extract_status_code_walks_exception_chain() -> None:
    inner = _StatusError("Unauthorized", status_code=401)
    try:
        try:
            raise inner
        except _StatusError as exc:
            raise RuntimeError("wrapped provider failure") from exc
    except RuntimeError as outer:
        assert extract_status_code(outer) == 401
        assert classify_failure_category(outer) is FailureCategory.PROVIDER_ERROR


@pytest.mark.parametrize(
    "message",
    [
        "LLM error 400: context_length_exceeded",
        "maximum context length is 128000 tokens",
        "prompt is too long: 200000 tokens > 180000 maximum",
        "input token count exceeds the model context window",
    ],
)
def test_context_window_classifier_recognizes_provider_variants(message: str) -> None:
    assert is_context_window_exceeded_error(Exception(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "LLM error 400: unsupported tool choice",
        "rate limit exceeded",
        "output token limit exceeded",
    ],
)
def test_context_window_classifier_rejects_unrelated_failures(message: str) -> None:
    assert is_context_window_exceeded_error(Exception(message)) is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Exception("LLM error 500: server unavailable"), INFRASTRUCTURE_FAILURE_EXIT_CODE),
        (
            Exception("LLM request failed: Name or service not known"),
            INFRASTRUCTURE_FAILURE_EXIT_CODE,
        ),
        (_StatusError("Unauthorized", status_code=401), AGENT_FAILURE_EXIT_CODE),
        (ValueError("agent bug"), AGENT_FAILURE_EXIT_CODE),
    ],
)
def test_exit_code_separates_infrastructure_from_real_failures(
    error: Exception,
    expected: int,
) -> None:
    assert exit_code_for_failure(error) == expected


def test_runner_outcome_recovers_harbor_nonzero_exit_code() -> None:
    error = RuntimeError("Command failed (exit 75): transient provider outage")

    assert extract_process_exit_code(error) == INFRASTRUCTURE_FAILURE_EXIT_CODE
    assert (
        extract_process_exit_code(RuntimeError("Agent failed with exit code 75"))
        == INFRASTRUCTURE_FAILURE_EXIT_CODE
    )
    assert run_outcome_for_exit_code(INFRASTRUCTURE_FAILURE_EXIT_CODE) is RunOutcome.INFRA_FAIL
    assert run_outcome_for_exit_code(AGENT_FAILURE_EXIT_CODE) is RunOutcome.FAIL
