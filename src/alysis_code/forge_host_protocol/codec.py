"""Strict JSON encoding and field-aware redaction boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from .errors import ProtocolValidationError, UnsafeSerializationError
from .models import ENVELOPE_ADAPTER, Envelope
from .redaction import PRIVATE_PATH_PATTERNS, SECRET_VALUE_PATTERNS

_FORBIDDEN_KEY_NAMES = frozenset(
    {
        "apikey",
        "apitoken",
        "authtoken",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "privatekey",
        "refreshtoken",
        "accesstoken",
        "secret",
        "token",
    }
)
_FORBIDDEN_KEY_SUFFIXES = ("apikey", "password", "privatekey", "refreshtoken", "secret")
_COMMAND_TEXT_FIELDS = frozenset({"argv", "originaltext", "displaytext"})
_STRUCTURED_PATH_FIELDS = frozenset(
    {
        "path",
        "cwd",
        "homedir",
        "cachedir",
        "tempdir",
        "workingdirectory",
        "workspacerelativepath",
        "estimatedfiles",
        "writescope",
        "referencedpaths",
        "allowedoutputs",
        "declaredinputs",
        "workspaceroot",
        "projectroot",
        "repositoryroot",
        "repositorypath",
        "workspacepath",
        "worktree",
        "worktreepath",
    }
)
_MAX_WIRE_BYTES = 1_048_576


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def _assert_safe_string(value: str, *, field: str | None) -> None:
    if "\x00" in value:
        raise UnsafeSerializationError("transport text contains a NUL byte")
    if field in _COMMAND_TEXT_FIELDS and any(
        pattern.search(value) for pattern in SECRET_VALUE_PATTERNS
    ):
        raise UnsafeSerializationError("structured command data contains credential material")
    path_field = field in _STRUCTURED_PATH_FIELDS or bool(
        field and field.endswith(("path", "root", "directory"))
    )
    path_is_absolute = path_field and (
        value.startswith(("/", "~/", "\\\\", "file://"))
        or bool(re.match(r"(?i)^[A-Z]:[\\/]", value))
    )
    command_contains_absolute = field in _COMMAND_TEXT_FIELDS and any(
        pattern.search(value) for pattern in PRIVATE_PATH_PATTERNS
    )
    if path_is_absolute or command_contains_absolute:
        raise UnsafeSerializationError("structured transport data contains a private host path")


def _assert_transport_safe(value: Any, *, depth: int, field: str | None) -> None:
    if depth > 64:
        raise UnsafeSerializationError("transport exceeds the safe nesting depth")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise UnsafeSerializationError("transport contains a non-string JSON key")
            normalized = _normalized_key(key)
            if normalized in _FORBIDDEN_KEY_NAMES or normalized.endswith(_FORBIDDEN_KEY_SUFFIXES):
                raise UnsafeSerializationError("transport contains a secret-bearing field")
            _assert_safe_string(key, field=None)
            _assert_transport_safe(item, depth=depth + 1, field=normalized)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            _assert_transport_safe(item, depth=depth + 1, field=field)
        return
    if isinstance(value, str):
        _assert_safe_string(value, field=field)


def assert_transport_safe(value: Any) -> None:
    """Reject secrets/private paths only at structured serialization boundaries.

    Human planner prose (goal, description, summary, and answers) is intentionally
    not content-scanned. This avoids rejecting ordinary text such as ``/api`` or
    documentation that discusses token formats. Typed path and command fields
    remain fail closed.
    """

    _assert_transport_safe(value, depth=0, field=None)


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ProtocolValidationError(
            "invalid_json_data", "message is not canonical JSON data"
        ) from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolValidationError(
                "duplicate_json_key", "message contains a duplicate JSON key"
            )
        value[key] = item
    return value


def _decode_json(value: str | bytes) -> Mapping[str, Any]:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) > _MAX_WIRE_BYTES:
        raise ProtocolValidationError(
            "message_too_large", "message exceeds the 1 MiB transport limit"
        )
    try:
        decoded = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except ProtocolValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as exc:
        raise ProtocolValidationError("invalid_json", "message is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ProtocolValidationError("invalid_envelope", "message JSON must be an object")
    return decoded


def validate_envelope(value: Mapping[str, Any] | str | bytes) -> Envelope:
    """Validate JSON data without reflecting attacker-controlled values in errors."""

    if isinstance(value, Mapping):
        wire_value = value
    elif isinstance(value, str | bytes):
        wire_value = _decode_json(value)
    else:
        raise ProtocolValidationError(
            "invalid_envelope", "message must be a JSON object, string, or bytes"
        )
    assert_transport_safe(wire_value)
    raw = _json_bytes(wire_value)
    if len(raw) > _MAX_WIRE_BYTES:
        raise ProtocolValidationError(
            "message_too_large", "message exceeds the 1 MiB transport limit"
        )
    try:
        envelope = ENVELOPE_ADAPTER.validate_json(raw, strict=True)
    except ValidationError as exc:
        raise ProtocolValidationError(
            "invalid_message", "message does not conform to Forge Host Protocol v2"
        ) from exc
    serialized = envelope.model_dump(mode="json", by_alias=True, exclude_none=False)
    assert_transport_safe(serialized)
    return envelope


def to_wire_dict(envelope: Envelope) -> dict[str, Any]:
    wire_value = envelope.model_dump(mode="json", by_alias=True, exclude_none=False)
    assert_transport_safe(wire_value)
    validated = validate_envelope(wire_value)
    return validated.model_dump(mode="json", by_alias=True, exclude_none=False)


def encode_envelope(envelope: Envelope) -> bytes:
    return _json_bytes(to_wire_dict(envelope))


def semantic_payload_digest(envelope: Envelope) -> str:
    """Hash command semantics, including its operation correlation authority."""

    semantic = {
        "operationId": envelope.operation_id,
        "payload": envelope.payload.model_dump(mode="json", by_alias=True, exclude_none=False),
    }
    assert_transport_safe(semantic)
    return hashlib.sha256(_json_bytes(semantic)).hexdigest()
