from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from alysis_code.agent.sensitive_output import (
    canonicalize_computer_use_response_for_dispatch,
)
from alysis_code.agent.session import (
    _protected_computer_use_failure,
    _scrub_sensitive_exception_chain,
)
from alysis_code.cancellation import CooperativeCancellationError
from alysis_code.computer_use_diagnostics import (
    DIAGNOSTIC_ATTRIBUTE,
    DIAGNOSTIC_CATEGORIES,
    ComputerUseProviderDiagnostic,
    computer_use_provider_diagnostic,
    computer_use_terminal_failure_code,
    protected_provider_diagnostic_marker,
)
from alysis_code.ide.stdio_bridge import _redact_bridge_error
from alysis_code.llm.metadata import OPENAI_RESPONSES_PROVIDER_METADATA_KEY
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.types import LLMError, LLMResponse, ToolCall


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("computer_use_no_trusted_host_call", "computer_use_no_trusted_host_call"),
        ("computer_use_transition_unobserved", "computer_use_transition_unobserved"),
        ("computer_use_itinerary_incomplete", "computer_use_itinerary_incomplete"),
        ("max_steps_exceeded", "computer_use_step_limit"),
        ("computer_use_host_failure", None),
        ("approval_declined", None),
        ("private page data sk-test-secret", "computer_use_turn_failed"),
        ({"secret": "private page data"}, "computer_use_turn_failed"),
    ],
)
def test_terminal_failure_reasons_are_closed_and_never_payloads(reason, expected):
    assert computer_use_terminal_failure_code(1, reason) == expected
    assert computer_use_terminal_failure_code(0, reason) is None
    assert computer_use_terminal_failure_code(130, reason) is None


def test_provider_metadata_survives_protected_replacement_without_exception_payload() -> None:
    response = httpx.Response(
        400,
        json={
            "error": {
                "message": "Private page content /Users/private/file and sk-private-canary",
                "code": "unsupported_parameter",
                "param": "include",
            }
        },
    )
    original = OpenAIResponsesClient._llm_error_from_response(response)
    protected = _protected_computer_use_failure(original)
    _scrub_sensitive_exception_chain(original)

    assert protected.args == (
        "Computer Use stopped because the protected provider or host request failed.",
    )
    assert protected.__cause__ is None
    assert protected.__context__ is None
    marker = protected_provider_diagnostic_marker(protected)
    assert marker == (
        "[computer_use_provider_diagnostic class=LLMError category=provider_error "
        "status=400 code=unsupported_parameter parameter=include site=none line=0]"
    )
    assert _redact_bridge_error(None, protected) == f"{protected} {marker}"
    assert "Private" not in repr(vars(protected))
    assert "sk-private-canary" not in _redact_bridge_error(None, protected)
    assert original.args == ("[redacted: protected Computer Use failure]",)


def test_diagnostic_follows_bounded_typed_causal_metadata_without_retaining_causes() -> None:
    cause = httpx.HTTPStatusError(
        "private HTTP body",
        request=httpx.Request("GET", "https://private.invalid"),
        response=httpx.Response(429),
    )
    cause.code = "rate_limit_exceeded"
    cause.param = "tools"
    outer = LLMError("private outer prose")
    outer.__cause__ = cause
    cause.__context__ = outer
    diagnostic = computer_use_provider_diagnostic(outer)
    assert diagnostic == ComputerUseProviderDiagnostic(
        "LLMError", "provider_throttled", 429, "rate_limit_exceeded", "tools"
    )


@pytest.mark.parametrize("status", [True, "400", 399, 600, None])
def test_unknown_or_noninteger_http_status_and_provider_fields_are_omitted(status: object) -> None:
    private_error_type = type("PrivateErrorClassCanary", (Exception,), {})
    error = private_error_type("private prose")
    error.status_code = status
    error.code = "private-code-canary"
    error.param = "input[42].private-field-canary"
    error.body = {"secret": "private-body-canary"}
    diagnostic = computer_use_provider_diagnostic(error)
    assert diagnostic.error_class == "UnknownError"
    assert diagnostic.category in DIAGNOSTIC_CATEGORIES
    assert diagnostic.status == 0
    assert diagnostic.code == diagnostic.parameter == "none"


@pytest.mark.parametrize(
    "value",
    [
        "[computer_use_provider_diagnostic class=Private]",
        {"code": "private-canary"},
        ComputerUseProviderDiagnostic("LLMError", "provider_error", True, "none", "none"),
        ComputerUseProviderDiagnostic("LLMError", "provider_error", 400, ["private"], "none"),
        ComputerUseProviderDiagnostic(
            "LLMError", "provider_error", 400, "none", "none", "private-site", 5
        ),
        ComputerUseProviderDiagnostic("LLMError", "provider_error", 400, "none", "none", "none", 1),
        ComputerUseProviderDiagnostic(
            "LLMError", "provider_error", 400, "none", "none", "turn_core", 0
        ),
        ComputerUseProviderDiagnostic(
            "LLMError", "provider_error", 400, "none", "none", "turn_core", True
        ),
        ComputerUseProviderDiagnostic(
            "LLMError", "provider_error", 400, "none", "none", "turn_core", 100000
        ),
    ],
)
def test_bridge_rejects_forged_or_malformed_diagnostic_attributes(value: object) -> None:
    error = RuntimeError("public fixed error")
    setattr(error, DIAGNOSTIC_ATTRIBUTE, value)
    assert protected_provider_diagnostic_marker(error) is None
    assert _redact_bridge_error(None, error) == "public fixed error"


@pytest.mark.parametrize(
    "error", [CooperativeCancellationError("stop"), KeyboardInterrupt(), SystemExit(130)]
)
def test_control_flow_replacements_remain_unclassified(error: BaseException) -> None:
    protected = _protected_computer_use_failure(error)
    assert isinstance(protected, type(error))
    assert protected_provider_diagnostic_marker(protected) is None


@pytest.mark.parametrize(
    ("reasoning", "expected_code"),
    [
        ({"type": "reasoning", "id": "rs-fixture", "summary": []}, "reasoning_encryption_missing"),
        (
            {
                "type": "reasoning",
                "id": "rs-fixture",
                "encrypted_content": "fixture",
                "summary": [{"type": "summary_text", "text": "private-reasoning-canary"}],
            },
            "reasoning_summary_refused",
        ),
    ],
)
def test_computer_use_contract_rejection_retains_a_closed_precise_code(
    reasoning: dict, expected_code: str
) -> None:
    response = LLMResponse(
        content="",
        raw={},
        tool_calls=[ToolCall(id="call-fixture", name="computer_state", arguments={})],
        provider_metadata={
            OPENAI_RESPONSES_PROVIDER_METADATA_KEY: {
                "output_items": [
                    reasoning,
                    {
                        "type": "function_call",
                        "id": "fc-fixture",
                        "call_id": "call-fixture",
                        "name": "computer_state",
                        "arguments": json.dumps({}),
                    },
                ]
            }
        },
    )
    with pytest.raises(ValueError) as rejected:
        canonicalize_computer_use_response_for_dispatch(response)
    diagnostic = computer_use_provider_diagnostic(rejected.value)
    assert diagnostic.category == "provider_error"
    assert diagnostic.code == expected_code
    assert diagnostic.site == "sensitive_output"
    assert 1 <= diagnostic.line <= 99999
    assert "private-reasoning-canary" not in repr(diagnostic)


@pytest.mark.parametrize("error_type", [NameError, UnboundLocalError, IndexError, ImportError])
def test_standard_error_class_survives_without_name_or_prose_payload(
    error_type: type[Exception],
) -> None:
    original = error_type("private undefined variable and page content")
    original.name = "private-variable-canary"
    protected = _protected_computer_use_failure(original)
    _scrub_sensitive_exception_chain(original)
    diagnostic = getattr(protected, DIAGNOSTIC_ATTRIBUTE)
    assert diagnostic.error_class == error_type.__name__
    assert diagnostic.site == "none"
    assert diagnostic.line == 0
    assert "private" not in protected_provider_diagnostic_marker(protected)


def test_reprotecting_scrubbed_exception_preserves_original_closed_diagnostic() -> None:
    original = NameError("private-name-canary")
    first = _protected_computer_use_failure(original)
    _scrub_sensitive_exception_chain(original)
    second = _protected_computer_use_failure(first)
    _scrub_sensitive_exception_chain(first)
    assert protected_provider_diagnostic_marker(second) == (
        "[computer_use_provider_diagnostic class=NameError category=implementation_failed "
        "status=0 code=none parameter=none site=none line=0]"
    )
    assert second.__cause__ is None
    assert second.__context__ is None


def test_source_site_rejects_unowned_path_even_when_basename_matches() -> None:
    try:
        exec(
            compile(
                "raise NameError('private name and page content')",
                "/private-canary/agent/turn/core.py",
                "exec",
            )
        )
    except NameError as original:
        protected = _protected_computer_use_failure(original)
        _scrub_sensitive_exception_chain(original)
    diagnostic = getattr(protected, DIAGNOSTIC_ATTRIBUTE)
    assert diagnostic.site == "none"
    assert diagnostic.line == 0
    assert "private" not in repr(diagnostic)


def test_owned_source_site_survives_full_session_failure_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alysis_code.agent import session as session_module
    from alysis_code.agent_loop import create_session
    from alysis_code.config import AppConfig
    from alysis_code.surface.noop_surface import NoopSurface

    def failing_turn(*_args: object, **_kwargs: object) -> int:
        raise NameError("private undefined name and page content")

    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="synthetic-fixture",
        surface=NoopSurface(),
        verification_enabled=False,
        sensitive_media=True,
        computer_use_host_request=lambda *_args: {"snapshot_id": "synthetic"},
    )
    monkeypatch.setattr(session_module, "_run_turn", failing_turn)
    try:
        with pytest.raises(Exception) as rejected:
            session.run_turn("synthetic private prompt")
    finally:
        session.close()
    diagnostic = getattr(rejected.value, DIAGNOSTIC_ATTRIBUTE)
    assert diagnostic.error_class == "NameError"
    assert diagnostic.site == "agent_session"
    assert (
        "_run_turn(" in Path(session_module.__file__).read_text().splitlines()[diagnostic.line - 1]
    )
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert "private" not in protected_provider_diagnostic_marker(rejected.value)
