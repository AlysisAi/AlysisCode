from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, TypeAlias

PROTOCOL_VERSION = "1"
MAX_REQUEST_BYTES = 1024 * 1024

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
RequestId: TypeAlias = str | int | None

_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|token|secret|password|credential|authorization)", re.I
)
_SECRET_VALUE_MIN_LENGTH = 6
_GENERIC_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b([A-Z0-9_.-]*(?:api[_-]?key|token|secret|password|credential)[A-Z0-9_.-]*\s*[:=]\s*)([^\s,;&]+)"
)
_AUTHORIZATION_VALUE_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[A-Za-z0-9._~+/=-]{8,}"
)
_BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_USERINFO_PATTERN = re.compile(
    r"(?i)\b((?:git\+)?https?://)([^/@\s]+)@([A-Za-z0-9.-]+(?::\d+)?)"
)
_PRIVATE_KEY_BLOCK_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^PuTTY-User-Key-File-\d+:[^\r\n]*.*?^Private-MAC:[^\r\n]*",
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    ),
)


class ProtocolError(RuntimeError):
    def __init__(self, code: str, message: str, *, request_id: RequestId = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class ProtocolRequest:
    id: RequestId
    method: str
    params: dict[str, Any]
    protocol_version: str = PROTOCOL_VERSION
    computer_use_attribution: dict[str, Any] | None = None
    resolved_entry_valid: bool = True


def _env_secret_values() -> tuple[str, ...]:
    values: list[str] = []
    for key, value in os.environ.items():
        if not value or len(value) < _SECRET_VALUE_MIN_LENGTH:
            continue
        if _SECRET_KEY_PATTERN.search(key):
            values.append(value)
    return tuple(dict.fromkeys(values))


def redact_secrets(value: Any, *, extra_secrets: tuple[str, ...] = ()) -> Any:
    secrets = tuple(secret for secret in (*_env_secret_values(), *extra_secrets) if secret)

    def _redact_text(text: str) -> str:
        redacted = text
        for secret in secrets:
            redacted = redacted.replace(secret, "<redacted>")
        redacted = _AUTHORIZATION_VALUE_PATTERN.sub(r"\1<redacted>", redacted)
        redacted = _BEARER_TOKEN_PATTERN.sub("Bearer <redacted>", redacted)
        redacted = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1<redacted>", redacted)
        redacted = _URL_USERINFO_PATTERN.sub(r"\1<redacted>@\3", redacted)
        for pattern in _PRIVATE_KEY_BLOCK_PATTERNS:
            redacted = pattern.sub("<redacted-private-key>", redacted)
        for pattern in _GENERIC_SECRET_PATTERNS:
            redacted = pattern.sub("<redacted>", redacted)
        return redacted

    def _walk(item: Any, *, key_hint: str = "") -> Any:
        if isinstance(item, str):
            if _SECRET_KEY_PATTERN.search(key_hint):
                return "<redacted>" if item else item
            return _redact_text(item)
        if isinstance(item, list):
            return [_walk(child, key_hint=key_hint) for child in item]
        if isinstance(item, tuple):
            return [_walk(child, key_hint=key_hint) for child in item]
        if isinstance(item, dict):
            out: dict[str, Any] = {}
            for key, child in item.items():
                key_text = str(key)
                if _SECRET_KEY_PATTERN.search(key_text) and isinstance(child, str):
                    out[key_text] = "<redacted>" if child not in (None, "") else child
                else:
                    out[key_text] = _walk(child, key_hint=key_text)
            return out
        return item

    return _walk(value)


def parse_request_line(line: str) -> ProtocolRequest:
    raw = line.rstrip("\n")
    if not raw.strip():
        raise ProtocolError("empty_request", "Request line is empty.")
    if len(raw.encode("utf-8", errors="replace")) > MAX_REQUEST_BYTES:
        raise ProtocolError("request_too_large", "Request exceeds the maximum supported size.")
    from ..llm.resolved_entry import BridgeFrameInvalid, validate_bridge_request

    resolved_entry_valid = True
    try:
        validate_bridge_request(raw)
    except BridgeFrameInvalid as exc:
        if exc.response_frame:
            # Decode only to correlate and poison the owned pending request.
            resolved_entry_valid = False
        else:
            raise ProtocolError(
                "invalid_params",
                "Invalid resolved Computer Use frame.",
                request_id=exc.request_id,
            ) from None
    from ..host_actions import HostActionError
    from ..mcp.github_policy_json import validate_sidecar_frame

    try:
        validate_sidecar_frame(raw)
    except (ValueError, TypeError, RecursionError, HostActionError):
        raise ProtocolError("invalid_params", "Invalid GitHub repository policy frame.") from None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProtocolError("malformed_json", "Request is not valid JSON.") from e
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_request", "Request must be a JSON object.")

    request_id_raw = payload.get("id")
    request_id: RequestId
    if request_id_raw is None or isinstance(request_id_raw, str | int):
        request_id = request_id_raw
    else:
        raise ProtocolError("invalid_request", "Request id must be a string, integer, or null.")

    version = str(payload.get("protocol_version") or "").strip()
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported_protocol_version",
            f"Unsupported protocol_version: {version or '(missing)'}.",
            request_id=request_id,
        )

    method = str(payload.get("method") or "").strip()
    if not method:
        raise ProtocolError("invalid_request", "Request method is required.", request_id=request_id)

    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProtocolError(
            "invalid_request", "Request params must be an object.", request_id=request_id
        )

    from ..computer_use_attribution import request_context

    attribution = request_context(raw, method) if "computer_use_attribution" in payload else None
    return ProtocolRequest(
        id=request_id,
        method=method,
        params=params,
        protocol_version=version,
        computer_use_attribution=attribution,
        resolved_entry_valid=resolved_entry_valid,
    )


def response_message(request_id: RequestId, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "id": request_id,
        "ok": True,
        "result": redact_secrets(result),
    }


def error_message(
    request_id: RequestId,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": {
            "code": code,
            "message": str(redact_secrets(message)),
        },
    }
    if details:
        payload["error"]["details"] = redact_secrets(details)
    return payload


def dumps_message(payload: dict[str, Any]) -> str:
    return json.dumps(redact_secrets(payload), ensure_ascii=True, sort_keys=True) + "\n"
