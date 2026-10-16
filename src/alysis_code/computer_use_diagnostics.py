"""Closed, payload-free diagnostics retained after protected exception scrubbing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

from .failure_category import FailureCategory, classify_failure_category

DIAGNOSTIC_ATTRIBUTE = "_alysis_computer_use_provider_diagnostic"


def computer_use_local_result_failure_code(tool_name: object, result: object) -> str | None:
    """Read only closed result codes before protected tool results are scrubbed.

    This is diagnostic classification, never execution or proof authority. Unknown
    errors retain the generic host failure; messages and arguments are not read.
    """
    if (
        type(tool_name) is not str
        or tool_name
        not in (
            "computer_state",
            "computer_action",
            "computer_wait",
            "computer_verify",
            "computer_download",
            "computer_file_status",
            "computer_upload",
            "computer_upload_status",
        )
        or type(result) is not dict
    ):
        return None
    error = result.get("error")
    if type(error) is not str:
        return None
    if (
        tool_name in {"computer_action", "computer_download", "computer_upload"}
        and error == "computer_use_approval_expired"
    ):
        return "computer_use_approval_expired"
    if tool_name == "computer_verify":
        return {
            "invalid_verification_proposal": "computer_use_verification_arguments_invalid",
            "invalid_host_response": "computer_use_verification_response_invalid",
        }.get(error)
    return {
        "invalid_host_response": "computer_use_observation_response_invalid",
        "invalid_host_workflow": "computer_use_workflow_response_invalid",
        "computer_use_fresh_observation_required": "computer_use_fresh_observation_required",
    }.get(error)


def computer_use_terminal_failure_code(exit_code: int, reason: object) -> str | None:
    """Project a failed turn onto closed codes; never retain a payload as a reason."""
    if exit_code in (0, 130) or reason in ("approval_declined", "computer_use_host_failure"):
        # Host failure already emits its own event. Cancellation/denial is not a
        # runtime failure, and successful turns must never inherit one.
        return None
    if type(reason) is str and reason in (
        "computer_use_no_trusted_host_call",
        "computer_use_transition_unobserved",
        "computer_use_itinerary_incomplete",
        "computer_use_file_setup_required",
        "computer_use_file_delivery_unverified",
    ):
        return reason
    if reason == "max_steps_exceeded":
        return "computer_use_step_limit"
    return "computer_use_turn_failed"


_ExceptionT = TypeVar("_ExceptionT", bound=BaseException)
DIAGNOSTIC_CLASSES = frozenset(
    {
        "UnknownError",
        "AgentRuntimeError",
        "LLMError",
        "ResponsesError",
        "RequiredToolCallingUnsupportedError",
        "ProviderAuthError",
        "ProviderCredentialScopeMismatchError",
        "ProtocolError",
        "TypeError",
        "ValueError",
        "KeyError",
        "AttributeError",
        "RuntimeError",
        "AssertionError",
        "TimeoutError",
        "ConnectionError",
        "OSError",
        "UnicodeDecodeError",
        "JSONDecodeError",
        "HTTPStatusError",
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "ReadTimeout",
        "WriteError",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "DecodingError",
        "NameError",
        "UnboundLocalError",
        "IndexError",
        "LookupError",
        "ImportError",
        "ModuleNotFoundError",
        "NotImplementedError",
        "RecursionError",
        "MemoryError",
        "OverflowError",
        "ZeroDivisionError",
        "ArithmeticError",
        "StopIteration",
        "StopAsyncIteration",
        "ReferenceError",
        "EOFError",
        "FileNotFoundError",
        "FileExistsError",
        "PermissionError",
        "IsADirectoryError",
        "NotADirectoryError",
        "BrokenPipeError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "ProcessLookupError",
        "ChildProcessError",
        "InterruptedError",
        "BlockingIOError",
        "UnicodeError",
        "UnicodeEncodeError",
        "UnicodeTranslateError",
        "SyntaxError",
        "IndentationError",
        "TabError",
        "ExceptionGroup",
        "ValidationError",
        "PydanticUserError",
        "PydanticSerializationError",
        "CancelledError",
        "Empty",
        "Full",
        "ConfigError",
        "HostImageValidationError",
        "ProviderLoginRequiredError",
        "CancellationTransportUnsupportedError",
        "UnsupportedProtocolError",
        "SessionWorkdirError",
        "ApprovalDeclinedError",
        "WorkspaceContextError",
        "WorkspaceBindingError",
        "ModelMetadataPolicyError",
        "ProviderModelCatalogError",
        "DeadlineExhausted",
        "ManagedHostDeadlineError",
        "ManagedSessionError",
        "PermissionPolicyError",
        "PermissionPolicyValidationError",
        "PermissionPolicyCorruptError",
    }
)
DIAGNOSTIC_CATEGORIES = frozenset(item.value for item in FailureCategory)
CONTRACT_ERROR_CODES = frozenset(
    {
        "invalid_tool_linkage",
        "missing_tool_call_id",
        "unknown_computer_use_tool",
        "reused_tool_call_id",
        "reasoning_encryption_missing",
        "reasoning_summary_refused",
        "provider_tool_metadata_mismatch",
        "multiple_calls_refused",
    }
)
DIAGNOSTIC_CODES = CONTRACT_ERROR_CODES | frozenset(
    {
        "none",
        "poisoned_session",
        "invalid_request_error",
        "invalid_api_key",
        "authentication_error",
        "permission_denied",
        "model_not_found",
        "context_length_exceeded",
        "rate_limit_exceeded",
        "insufficient_quota",
        "usage_limit_reached",
        "billing_hard_limit_reached",
        "server_error",
        "internal_error",
        "overloaded_error",
        "unsupported_parameter",
        "unsupported_value",
        "invalid_value",
        "invalid_type",
        "missing_required_parameter",
        "invalid_function_parameters",
        "tool_call_mismatch",
    }
)
DIAGNOSTIC_PARAMETERS = frozenset(
    {
        "none",
        "input",
        "messages",
        "tools",
        "tool_choice",
        "model",
        "reasoning",
        "include",
        "previous_response_id",
        "max_output_tokens",
        "max_tokens",
        "temperature",
        "store",
        "parallel_tool_calls",
        "stream",
        "response_format",
    }
)

# Only identities of these shipped source files can leave a traceback. Neither
# provider files nor user/plugin paths are accepted, even if their basename matches.
_PACKAGE_ROOT = Path(__file__).resolve().parent
_SOURCE_FILES = {
    "agent/session.py": "agent_session",
    "agent/turn/core.py": "turn_core",
    "agent/computer_use_tools.py": "computer_use_tools",
    "agent/sensitive_output.py": "sensitive_output",
    "ide/stdio_bridge.py": "ide_bridge",
    "llm/openai_responses.py": "responses_client",
    "llm/openai_compat.py": "compat_client",
    "llm/anthropic_messages.py": "anthropic_client",
    "llm/gemini_generate_content.py": "gemini_content_client",
    "llm/gemini_interactions.py": "gemini_interactions_client",
    "provider_auth/openai_codex.py": "subscription_auth",
    "host_images.py": "host_images",
    "host_browser.py": "host_browser",
}
_SOURCE_SITES_BY_PATH = {
    (_PACKAGE_ROOT / relative_path).resolve(): site for relative_path, site in _SOURCE_FILES.items()
}
DIAGNOSTIC_SITES = frozenset({"none", *_SOURCE_FILES.values()})


@dataclass(frozen=True, slots=True)
class ComputerUseProviderDiagnostic:
    error_class: str
    category: str
    status: int
    code: str
    parameter: str
    site: str = "none"
    line: int = 0
    retry_after: int = 0
    reset_at: int = 0


def _attribute(value: Any, name: str) -> Any:
    try:
        return getattr(value, name, None)
    except BaseException:  # noqa: BLE001 - diagnostic properties must not break cleanup
        return None


def _allowed_text(value: Any, allowed: frozenset[str], fallback: str = "none") -> str:
    return value if type(value) is str and value in allowed else fallback


def _status(value: Any) -> int:
    return value if type(value) is int and 400 <= value <= 599 else 0


def _source_site(error: BaseException) -> tuple[str, int]:
    """Retain only a closed owned-module identity and bounded source line."""
    current = _attribute(error, "__traceback__")
    site, line = "none", 0
    for _ in range(64):
        if not isinstance(current, TracebackType):
            break
        try:
            candidate = _SOURCE_SITES_BY_PATH.get(
                Path(current.tb_frame.f_code.co_filename).resolve()
            )
            if candidate is not None and 1 <= current.tb_lineno <= 99999:
                site, line = candidate, current.tb_lineno
        except BaseException:  # noqa: BLE001 - diagnostic lookup must not break cleanup
            pass
        current = current.tb_next
    return site, line


def set_safe_provider_error_fields(
    error: _ExceptionT,
    *,
    status: Any = None,
    code: Any = None,
    parameter: Any = None,
    retry_after: Any = 0,
    reset_at: Any = 0,
) -> _ExceptionT:
    """Copy only closed scalar metadata while the provider response is still available."""
    safe_status = _status(status)
    if safe_status:
        error.status_code = safe_status  # type: ignore[attr-defined]
    error.provider_error_code = _allowed_text(code, DIAGNOSTIC_CODES)  # type: ignore[attr-defined]
    error.provider_error_parameter = _allowed_text(parameter, DIAGNOSTIC_PARAMETERS)  # type: ignore[attr-defined]
    if safe_status == 429:
        error.provider_retry_after = _bounded_timing(retry_after, 2_678_400)  # type: ignore[attr-defined]
        error.provider_reset_at = _bounded_timing(reset_at, 4_102_444_800)  # type: ignore[attr-defined]
    return error


def _bounded_timing(value: Any, maximum: int) -> int:
    return value if type(value) is int and 0 <= value <= maximum else 0


def computer_use_provider_diagnostic(error: BaseException) -> ComputerUseProviderDiagnostic:
    existing = _validated_diagnostic(_attribute(error, DIAGNOSTIC_ATTRIBUTE))
    if existing is not None:
        # Reapplying the protected boundary must not replace an already scrubbed
        # original diagnosis with the replacement's AgentRuntimeError identity.
        return existing
    pending = [error]
    seen: set[int] = set()
    error_class = "UnknownError"
    category = FailureCategory.IMPLEMENTATION_FAILED.value
    status = 0
    code = parameter = "none"
    site, line = "none", 0
    retry_after = reset_at = 0
    while pending and len(seen) < 16:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if error_class == "UnknownError":
            error_class = _allowed_text(type(current).__name__, DIAGNOSTIC_CLASSES, error_class)
        candidate_site, candidate_line = _source_site(current)
        if candidate_site != "none":
            site, line = candidate_site, candidate_line
        if category == FailureCategory.IMPLEMENTATION_FAILED.value:
            try:
                candidate = classify_failure_category(current).value
            except BaseException:  # noqa: BLE001 - retain only the closed fallback
                candidate = category
            category = _allowed_text(candidate, DIAGNOSTIC_CATEGORIES, category)
        status = status or _status(_attribute(current, "status_code"))
        status = status or _status(_attribute(_attribute(current, "response"), "status_code"))
        retry_after = retry_after or _bounded_timing(
            _attribute(current, "provider_retry_after"), 2_678_400
        )
        reset_at = reset_at or _bounded_timing(
            _attribute(current, "provider_reset_at"), 4_102_444_800
        )
        if code == "none":
            for name in ("computer_use_error_code", "provider_error_code", "code"):
                code = _allowed_text(_attribute(current, name), DIAGNOSTIC_CODES)
                if code != "none":
                    break
        if parameter == "none":
            for name in ("provider_error_parameter", "param", "parameter"):
                parameter = _allowed_text(_attribute(current, name), DIAGNOSTIC_PARAMETERS)
                if parameter != "none":
                    break
        pending.extend((_attribute(current, "__cause__"), _attribute(current, "__context__")))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions[:16])
    if code in CONTRACT_ERROR_CODES:
        category = FailureCategory.PROVIDER_ERROR.value
    elif status == 429:
        category = FailureCategory.PROVIDER_THROTTLED.value
    elif status >= 500:
        category = FailureCategory.PROVIDER_UNAVAILABLE.value
    elif status:
        category = FailureCategory.PROVIDER_ERROR.value
    return ComputerUseProviderDiagnostic(
        error_class,
        category,
        status,
        code,
        parameter,
        site,
        line,
        retry_after if status == 429 else 0,
        reset_at if status == 429 else 0,
    )


def _validated_diagnostic(value: Any) -> ComputerUseProviderDiagnostic | None:
    if type(value) is not ComputerUseProviderDiagnostic:
        return None
    if (
        type(value.error_class) is not str
        or value.error_class not in DIAGNOSTIC_CLASSES
        or type(value.category) is not str
        or value.category not in DIAGNOSTIC_CATEGORIES
        or type(value.status) is not int
        or value.status != 0
        and not 400 <= value.status <= 599
        or type(value.code) is not str
        or value.code not in DIAGNOSTIC_CODES
        or type(value.parameter) is not str
        or value.parameter not in DIAGNOSTIC_PARAMETERS
        or type(value.site) is not str
        or value.site not in DIAGNOSTIC_SITES
        or type(value.line) is not int
        or (value.site == "none" and value.line != 0)
        or (value.site != "none" and not 1 <= value.line <= 99999)
        or type(value.retry_after) is not int
        or not 0 <= value.retry_after <= 2_678_400
        or type(value.reset_at) is not int
        or not 0 <= value.reset_at <= 4_102_444_800
        or (value.status != 429 and (value.retry_after != 0 or value.reset_at != 0))
    ):
        return None
    return value


def protected_provider_diagnostic_marker(error: Any) -> str | None:
    value = _validated_diagnostic(_attribute(error, DIAGNOSTIC_ATTRIBUTE))
    if value is None:
        return None
    return (
        f"[computer_use_provider_diagnostic class={value.error_class} category={value.category} "
        f"status={value.status} code={value.code} parameter={value.parameter} "
        f"site={value.site} line={value.line}"
        + (
            f" retry_after={value.retry_after} reset_at={value.reset_at}"
            if value.retry_after or value.reset_at
            else ""
        )
        + "]"
    )
