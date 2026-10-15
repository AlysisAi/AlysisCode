from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .error_text import sanitize_error_text_for_output

_NETWORK_MODEL_SIGNALS = (
    "llm request failed",
    "llm error",
    "network",
    "timeout",
    "timed out",
    "connection",
    "dns",
    "tls",
    "certificate",
    "api key",
    "unauthorized",
    "forbidden",
    "rate limit",
    "too many requests",
    "base_url",
    "can't reach",
    "chat/completions",
    "model provider",
    "provider rejected",
)

_TOOL_TRANSCRIPT_SIGNAL_GROUPS = (
    (
        "assistant message with 'tool_calls' must be followed by tool messages",
        'assistant message with "tool_calls" must be followed by tool messages',
    ),
    (
        "tool_call_id",
        "did not have response messages",
    ),
    (
        "tool transcript",
        "missing",
    ),
    (
        "tool transcript",
        "malformed",
    ),
    (
        "tool transcript",
        "incomplete",
    ),
)


@dataclass(frozen=True)
class LLMErrorDisplay:
    title: str
    guidance_lines: tuple[str, ...]


def is_tool_transcript_error(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered:
        return False
    return any(all(token in lowered for token in group) for group in _TOOL_TRANSCRIPT_SIGNAL_GROUPS)


def is_network_or_model_error(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered or is_tool_transcript_error(lowered):
        return False
    return any(token in lowered for token in _NETWORK_MODEL_SIGNALS)


def friendly_llm_error_message(error: Any) -> str:
    """Return user-facing provider error text without raw exception wrappers."""
    try:
        from .llm.openai_compat import alysis_trial_error_message

        trial_message = alysis_trial_error_message(error)
    except Exception:  # noqa: BLE001
        trial_message = None
    if trial_message:
        return sanitize_error_text_for_output(trial_message)

    text = sanitize_error_text_for_output(error).strip()
    if not text:
        return "The model provider returned an empty error."
    if is_tool_transcript_error(text):
        return text

    status = _llm_error_status_code(text)
    detail = _provider_error_detail(text)
    lowered = f"{text}\n{detail}".casefold()
    url = _first_url(detail)
    url_suffix = f" See: {url}" if url else ""

    if status in {401, 403} or _looks_like_auth_error(lowered):
        status_suffix = f" ({status})" if status is not None else ""
        return f"Your API key was rejected{status_suffix}. Update it with /config.{url_suffix}"

    if _looks_like_connection_error(error, lowered):
        host = _provider_host_from_error(error) or _provider_host_from_text(text)
        host_suffix = f" at {host}" if host else ""
        return (
            f"Can't reach the model provider{host_suffix}. "
            "Check the profile base URL in /config and try again."
        )

    if _looks_like_timeout_error(lowered):
        return (
            "The model provider did not respond before the request timed out. "
            "Check the base URL/network, or increase the model timeout in /config."
        )

    if "tool_choice" in lowered:
        status_suffix = f" ({status})" if status is not None else ""
        return (
            f"The provider rejected the tool_choice parameter{status_suffix}. "
            "Use a tool-compatible model/provider, or retry with provider compatibility enabled."
        )

    unsupported_model = _unsupported_model_name(detail) or _unsupported_model_name(text)
    if unsupported_model:
        status_suffix = f" ({status})" if status is not None else ""
        return (
            f"The provider rejected model '{unsupported_model}' for this request"
            f"{status_suffix}. Check the model id or provider profile in /config."
        )

    if status == 429 or "rate limit" in lowered or "too many requests" in lowered:
        return "The model provider is rate-limiting this session (429). Wait a moment and retry."

    if status is not None and detail and detail != text:
        return f"The model provider rejected the request ({status}): {_single_line(detail)}"
    return text


def classify_llm_error_display(text: str) -> LLMErrorDisplay:
    # Transcript integrity errors often arrive inside a generic "LLM error 400" wrapper,
    # so classify them before broader network/model guidance.
    if is_tool_transcript_error(text):
        return LLMErrorDisplay(
            title="Tool Transcript Error",
            guidance_lines=(
                "This session's tool transcript is incomplete or malformed.",
                "Retry in a new session. If it repeats, inspect resume/compaction around missing tool responses.",
            ),
        )
    if is_network_or_model_error(text):
        return LLMErrorDisplay(
            title="Network/Model Error",
            guidance_lines=(
                "Check base URL, API key, and network connectivity.",
                "Retry when ready. Use /status for current session settings.",
            ),
        )
    return LLMErrorDisplay(title="LLM Error", guidance_lines=())


def _llm_error_status_code(text: str) -> int | None:
    match = re.search(r"\bLLM\s+error\s+(\d{3})\b", str(text or ""), re.IGNORECASE)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _llm_error_body(text: str) -> str:
    raw = str(text or "").strip()
    match = re.search(r"\bLLM\s+error\s+\d{3}\s*:\s*", raw, re.IGNORECASE)
    if match is None:
        return raw
    return raw[match.end() :].strip()


def _json_error_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(text or "").strip())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _provider_error_detail(text: str) -> str:
    body = _llm_error_body(text)
    payload = _json_error_payload(body)
    if not payload:
        return _single_line(body)
    raw_error = payload.get("error")
    error = raw_error if isinstance(raw_error, dict) else payload
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str) and message.strip():
        return _single_line(message)
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and code.strip():
        return _single_line(code)
    return _single_line(body)


def _single_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s)>\]}\"']+", str(text or ""))
    if match is None:
        return None
    return match.group(0).rstrip(".,;:")


def _looks_like_auth_error(lowered: str) -> bool:
    return any(
        token in lowered
        for token in (
            "invalid api key",
            "invalid_api_key",
            "incorrect api key",
            "missing api key",
            "api key is invalid",
            "api key is missing",
            "api key was rejected",
            "unauthorized",
            "authentication",
            "forbidden",
        )
    )


def _looks_like_connection_error(error: Any, lowered: str) -> bool:
    for exc in _exception_chain(error):
        type_name = type(exc).__name__.casefold()
        if type_name in {"connecterror", "connecttimeout"}:
            return True
    return any(
        token in lowered
        for token in (
            "connection refused",
            "connect error",
            "connect timeout",
            "connecttimeout",
            "dns",
            "name resolution",
            "network is unreachable",
            "temporary failure in name resolution",
        )
    )


def _looks_like_timeout_error(lowered: str) -> bool:
    return any(
        token in lowered
        for token in (
            "read timeout",
            "readtimeout",
            "timed out",
            "timeout",
        )
    )


def _provider_host_from_error(error: Any) -> str | None:
    for exc in _exception_chain(error):
        request = getattr(exc, "request", None)
        url = getattr(request, "url", None)
        host = getattr(url, "host", None)
        if isinstance(host, str) and host:
            port = getattr(url, "port", None)
            return f"{host}:{port}" if port is not None else host
    return None


def _provider_host_from_text(text: str) -> str | None:
    safe_label = re.search(
        r"(?P<host>(?:\[[0-9a-f:]+\]|[A-Za-z0-9.-]+)(?::\d+)?)\s+"
        r"\(endpoint\s+[0-9a-f]{12}\)",
        str(text or ""),
        re.IGNORECASE,
    )
    if safe_label is not None:
        return safe_label.group("host")
    url = _first_url(text)
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    return f"{parsed.hostname}:{parsed.port}" if parsed.port is not None else parsed.hostname


def _exception_chain(error: Any) -> list[Any]:
    chain: list[Any] = []
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        current = cause if cause is not None else context
    return chain


def _unsupported_model_name(text: str) -> str | None:
    match = re.search(
        r"(?:not\s+supported\s+model|unsupported\s+model|model\s+not\s+supported)\s+['\"]?([^'\"\s,.;}]+)",
        str(text or ""),
        re.IGNORECASE,
    )
    if match is None:
        return None
    return match.group(1).strip()
