from __future__ import annotations

import copy
import json
import math
import re
import secrets
from collections.abc import Collection, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any

from ..llm.metadata import (
    ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY,
    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
    OPENAI_RESPONSES_PROVIDER_METADATA_KEY,
    PROVIDER_METADATA_KEY,
    ROUTE_IDENTITY_PROVIDER_METADATA_KEY,
    TOOL_CALL_PROVIDER_METADATA_KEY,
)
from ..tools.fs import classify_sensitive_path

_SENSITIVE_POLICY_KEY = "_alysis_output_policy"
_REDACTED_ARGUMENT = "[redacted: sensitive file content]"
_REDACTED_MESSAGE = "Sensitive tool output redacted."
_REDACTED_RESPONSE_TAINT = "[redacted: approved sensitive content]"
_TERMINAL_SENSITIVE_READ_ERROR = (
    "Sensitive path is protected and will not be readable after this failure. "
    "No content was returned. This failure is terminal; do not retry."
)
_CONTENT_ARGUMENT_KEYS = frozenset(
    {
        "body",
        "bytes",
        "content",
        "contents",
        "data",
        "diff",
        "edit",
        "edits",
        "expected",
        "expected_old",
        "insert",
        "lines",
        "new",
        "new_content",
        "old",
        "old_content",
        "patch",
        "replacement",
        "text",
        "value",
    }
)
_DIRECT_PATH_FIELDS = (
    "path",
    "source_path",
    "destination_path",
    "file",
    "file_path",
)
_PATCH_PATH_RE = re.compile(r"^(?:\+\+\+|---)\s+(?:[ab]/)?([^\t\r\n]+)", re.MULTILINE)
_TAINT_VALUE_KEYS = _CONTENT_ARGUMENT_KEYS | frozenset({"insert_content", "replacement", "target"})
_COMPUTER_USE_TAINT_VALUE_KEYS = _TAINT_VALUE_KEYS | frozenset(
    {
        "bundle_id",
        "help",
        "label",
        "name",
        "title",
        "window_title",
    }
)
_COMPUTER_USE_NUMERIC_TAINT_KEYS = frozenset(
    {
        "delta_x",
        "delta_y",
        "height",
        "pid",
        "to_x",
        "to_y",
        "width",
        "x",
        "y",
    }
)
_COMPUTER_USE_TOOL_NAMES = frozenset(
    {
        "computer_state",
        "computer_action",
        "computer_wait",
        "computer_verify",
        "computer_download",
        "computer_file_status",
        "computer_upload",
        "computer_upload_status",
    }
)
# These are protocol capabilities, not general content-bearing values. Keep them
# out of _TAINT_VALUE_KEYS: Computer Use needs byte-exact transient dispatch, while
# its durable projections explicitly replace these fields.
_COMPUTER_USE_PRIVATE_IDENTIFIER_KEYS = frozenset(
    {
        "element_id",
        "artifact_id",
        "artifactid",
        "upload_id",
        "uploadid",
        "table_id",
        "account_element_id",
        "status_element_id",
        "merchant_element_id",
        "period_element_id",
        "period_from_element_id",
        "period_through_element_id",
        "parent_id",
        "previous_snapshot_id",
        "snapshot_id",
        "window_id",
        "window_title_digest",
    }
)
_REDACTED_COMPUTER_USE_IDENTIFIER = "[redacted: transient computer use identifier]"
_COMPUTER_USE_RAW_IDENTIFIER_RE = re.compile(
    r"(?:[\"']?)(?:element_id|artifact_id|artifactId|upload_id|uploadId|table_id|account_element_id|status_element_id|merchant_element_id|period_element_id|period_from_element_id|period_through_element_id|previous_snapshot_id|snapshot_id)(?:[\"']?)"
    r"\s*:\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SensitiveToolBoundary:
    """Persistence/display policy derived without opening sensitive files."""

    sensitive: bool = False
    paths: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    ephemeral_io: bool = False


@dataclass(frozen=True, slots=True)
class _PersistenceSafeToolCall:
    id: str
    name: str
    arguments: Any
    provider_metadata: Any = None


@dataclass(frozen=True, slots=True)
class _PersistenceSafeResponse:
    content: str
    tool_calls: list[Any]
    raw: dict[str, Any]
    response_model: Any = None
    usage: Any = None
    provider_metadata: Any = None
    reasoning: Any = ()
    assistant_phase: Any = None


class _ProtectedProtocolValue:
    """Opaque placeholder used while content taints are projected.

    Provider linkage identifiers and replay signatures are control-plane
    values.  They must remain byte-exact even when a short UI value (for
    example ``"0"``) happens to occur inside one of them.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def _new_computer_use_protocol_id(kind: str, seen: set[str]) -> str:
    while True:
        candidate = f"alysis_cu_{kind}_{secrets.token_hex(20)}"
        if candidate not in seen:
            seen.add(candidate)
            return candidate


def _normalized_unique_strings(values: list[Any]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip().replace("\\", "/")
        if not item or item == "/dev/null":
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return tuple(normalized)


def _candidate_paths(tool_name: str, arguments: Any) -> tuple[str, ...]:
    if not isinstance(arguments, dict):
        return ()
    normalized_name = str(tool_name or "").strip().casefold()
    if not (normalized_name.startswith("fs_") or normalized_name == "git_apply_patch"):
        return ()
    candidates = [arguments.get(field) for field in _DIRECT_PATH_FIELDS]
    if normalized_name == "git_apply_patch":
        patch = arguments.get("patch")
        if patch is None:
            patch = arguments.get("diff")
        if patch is not None:
            candidates.extend(_PATCH_PATH_RE.findall(str(patch)))
    return _normalized_unique_strings(candidates)


def _policy_categories(result: Any) -> tuple[str, ...]:
    if not isinstance(result, dict):
        return ()
    policy = result.get(_SENSITIVE_POLICY_KEY)
    if not isinstance(policy, dict) or policy.get("sensitive") is not True:
        return ()
    if str(policy.get("persist") or "").casefold() != "redact":
        return ()
    if str(policy.get("display") or "").casefold() != "redact":
        return ()
    raw_categories = policy.get("categories")
    if not isinstance(raw_categories, list | tuple):
        return ()
    return _normalized_unique_strings(list(raw_categories))


def sensitive_tool_boundary(
    tool_name: str,
    arguments: Any,
    *,
    result: Any = None,
    tool_metadata: Mapping[str, Any] | None = None,
) -> SensitiveToolBoundary:
    if tool_metadata and tool_metadata.get("ephemeral_io") is True:
        return SensitiveToolBoundary(
            sensitive=True, categories=("ephemeral_tool_io",), ephemeral_io=True
        )
    normalized_tool_name = str(tool_name or "").strip().casefold()
    computer_use_sensitive = normalized_tool_name in {
        "computer_state",
        "computer_action",
        "computer_wait",
        "computer_verify",
        "computer_download",
        "computer_file_status",
        "computer_upload",
        "computer_upload_status",
    }
    paths = _candidate_paths(tool_name, arguments)
    categories: list[str] = []
    sensitive_paths: list[str] = []
    for path in paths:
        classification = classify_sensitive_path(path)
        if not classification.sensitive:
            continue
        sensitive_paths.append(path)
        categories.append(str(classification.category or "sensitive_file"))

    policy_categories = _policy_categories(result)
    policy = result.get(_SENSITIVE_POLICY_KEY) if isinstance(result, dict) else None
    policy_sensitive = isinstance(policy, dict) and policy.get("sensitive") is True
    if not sensitive_paths and not policy_sensitive and not computer_use_sensitive:
        return SensitiveToolBoundary()
    return SensitiveToolBoundary(
        sensitive=True,
        paths=_normalized_unique_strings(sensitive_paths or list(paths)),
        categories=_normalized_unique_strings(
            categories
            + list(policy_categories)
            + (["host_ui_context"] if computer_use_sensitive else [])
        ),
    )


def _redact_content_fields(value: Any, *, parent_key: str = "") -> Any:
    if parent_key.casefold() in _CONTENT_ARGUMENT_KEYS:
        return _REDACTED_ARGUMENT
    if isinstance(value, dict):
        return {
            str(key): _redact_content_fields(item, parent_key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_content_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_content_fields(item) for item in value)
    return copy.deepcopy(value)


def _redact_computer_use_identifier_fields(value: Any, *, parent_key: str = "") -> Any:
    if parent_key.casefold() in (
        _COMPUTER_USE_PRIVATE_IDENTIFIER_KEYS
        | _COMPUTER_USE_NUMERIC_TAINT_KEYS
        | {"_raw_arguments"}
    ):
        return _REDACTED_COMPUTER_USE_IDENTIFIER
    if isinstance(value, dict):
        return {
            str(key): _redact_computer_use_identifier_fields(item, parent_key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_computer_use_identifier_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_computer_use_identifier_fields(item) for item in value)
    return copy.deepcopy(value)


def redact_sensitive_tool_arguments(
    tool_name: str,
    arguments: Any,
    *,
    boundary: SensitiveToolBoundary | None = None,
) -> Any:
    effective = boundary or sensitive_tool_boundary(tool_name, arguments)
    if not effective.sensitive:
        return arguments
    if effective.ephemeral_io:
        return {"_alysis_redacted": True, "message": _REDACTED_ARGUMENT}
    redacted = _redact_content_fields(arguments)
    if str(tool_name or "").strip().casefold() in _COMPUTER_USE_TOOL_NAMES:
        redacted = _redact_computer_use_identifier_fields(redacted)
    return redacted


def sensitive_result_stub(
    tool_name: str,
    boundary: SensitiveToolBoundary,
) -> dict[str, Any]:
    """Return the only representation allowed beyond the immediate model call."""

    return {
        "_alysis_redacted": True,
        "sensitive": True,
        "persist": "redact",
        "display": "redact",
        "tool": str(tool_name or ""),
        "paths": list(boundary.paths),
        "categories": list(boundary.categories),
        "message": _REDACTED_MESSAGE,
    }


def redact_sensitive_tool_result(
    tool_name: str,
    arguments: Any,
    result: Any,
    *,
    boundary: SensitiveToolBoundary | None = None,
) -> Any:
    effective = boundary or sensitive_tool_boundary(tool_name, arguments, result=result)
    if not effective.sensitive:
        return result
    stub = sensitive_result_stub(tool_name, effective)
    if isinstance(result, dict) and "error" in result:
        normalized_tool = str(tool_name or "").strip().casefold()
        if normalized_tool in {"fs_read", "fs_read_lines"}:
            if result.get("error_code") == "fs_path_not_found":
                path = effective.paths[0] if effective.paths else "requested path"
                stub["error"] = (
                    f"Path does not exist: {path}. This result is terminal; do not retry this path."
                )
                stub["error_code"] = "fs_path_not_found"
            else:
                stub["error"] = _TERMINAL_SENSITIVE_READ_ERROR
                stub["error_code"] = "sensitive_read_terminal"
            stub["terminal"] = True
            stub["retryable"] = False
        else:
            stub["error"] = "Sensitive tool operation failed; details redacted."
    return stub


def _add_taint_variants(values: set[str], raw_value: Any) -> None:
    if isinstance(raw_value, bytes):
        text = raw_value.decode("utf-8", errors="replace")
    elif isinstance(raw_value, str):
        text = raw_value
    else:
        return
    if not text or not text.strip():
        return
    values.add(text)
    stripped = text.strip()
    if stripped:
        values.add(stripped)
    escaped = json.dumps(text, ensure_ascii=False)[1:-1]
    if escaped and escaped != text:
        values.add(escaped)

    def _collect_json_strings(value: Any) -> None:
        if isinstance(value, str):
            if value.strip():
                values.add(value)
            return
        if isinstance(value, dict):
            for item in value.values():
                _collect_json_strings(item)
            return
        if isinstance(value, list):
            for item in value:
                _collect_json_strings(item)

    def _collect_json_text(candidate: str) -> None:
        try:
            decoded = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        _collect_json_strings(decoded)

    for line in text.splitlines():
        line_value = line.strip()
        if line_value:
            values.add(line_value)
        for separator in ("=", ":"):
            if separator not in line_value:
                continue
            key, candidate = line_value.split(separator, 1)
            if not key.strip() or not candidate.strip():
                continue
            candidate = candidate.strip().strip("\"'")
            if candidate:
                values.add(candidate)
                _collect_json_text(candidate)
            break
    _collect_json_text(text)


def _collect_computer_use_private_identifier_values(
    value: Any,
    values: set[str],
    *,
    semantic_element_scope: bool = False,
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if normalized_key == "_raw_arguments" and isinstance(item, str):
                for match in _COMPUTER_USE_RAW_IDENTIFIER_RE.finditer(item):
                    raw_identifier = match.group("value")
                    if match.group("quote") == '"':
                        try:
                            decoded_identifier = json.loads(f'"{raw_identifier}"')
                        except (TypeError, ValueError, json.JSONDecodeError):
                            decoded_identifier = raw_identifier
                    else:
                        decoded_identifier = raw_identifier
                    if isinstance(decoded_identifier, str) and decoded_identifier:
                        values.add(decoded_identifier)
                continue
            if (
                (
                    normalized_key in _COMPUTER_USE_PRIVATE_IDENTIFIER_KEYS
                    or (semantic_element_scope and normalized_key == "id")
                )
                and isinstance(item, str)
                and item
            ):
                values.add(item)
            else:
                _collect_computer_use_private_identifier_values(
                    item,
                    values,
                    semantic_element_scope=(semantic_element_scope or normalized_key == "elements"),
                )
        return
    if isinstance(value, list | tuple):
        for item in value:
            _collect_computer_use_private_identifier_values(
                item,
                values,
                semantic_element_scope=semantic_element_scope,
            )


def collect_computer_use_private_identifiers(
    tool_name: str,
    arguments: Any,
    result: Any = None,
) -> tuple[str, ...]:
    """Collect exact Computer Use capability IDs for durable text projection only.

    This intentionally remains separate from the global approved-content taint
    collector. Callers may use these values to sanitize persistence, replay, and
    presentation copies without ever changing the live Computer Use arguments.
    """

    if str(tool_name or "").strip().casefold() not in _COMPUTER_USE_TOOL_NAMES:
        return ()
    values: set[str] = set()
    _collect_computer_use_private_identifier_values(arguments, values)
    _collect_computer_use_private_identifier_values(result, values)
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _collect_taint_values(
    value: Any,
    values: set[str],
    *,
    parent_key: str = "",
    taint_keys: Collection[str] = _TAINT_VALUE_KEYS,
    numeric_taint_keys: Collection[str] = (),
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if normalized_key in taint_keys and isinstance(item, str | bytes):
                _add_taint_variants(values, item)
            elif (
                normalized_key in numeric_taint_keys
                and not isinstance(item, bool)
                and isinstance(item, int | float)
                and math.isfinite(float(item))
                and float(item) not in {-1.0, 0.0, 1.0}
            ):
                values.add(json.dumps(item, ensure_ascii=False, allow_nan=False))
            else:
                _collect_taint_values(
                    item,
                    values,
                    parent_key=normalized_key,
                    taint_keys=taint_keys,
                    numeric_taint_keys=numeric_taint_keys,
                )
        return
    if isinstance(value, list | tuple):
        for item in value:
            _collect_taint_values(
                item,
                values,
                parent_key=parent_key,
                taint_keys=taint_keys,
                numeric_taint_keys=numeric_taint_keys,
            )
        return
    if parent_key in taint_keys:
        _add_taint_variants(values, value)


def collect_sensitive_response_taints(
    tool_name: str,
    arguments: Any,
    result: Any,
    *,
    boundary: SensitiveToolBoundary | None = None,
) -> tuple[str, ...]:
    """Collect exact approved content values that a provider must not echo durably.

    Paths, categories, operation names, and unrelated metadata are deliberately
    excluded. Only content-bearing leaves from a sensitive tool exchange become
    taints, which keeps redaction narrow and value-specific.
    """

    effective = boundary or sensitive_tool_boundary(tool_name, arguments, result=result)
    if not effective.sensitive:
        return ()
    values: set[str] = set()
    if effective.ephemeral_io:
        _collect_ephemeral_values(arguments, values)
        _collect_ephemeral_values(result, values)
        return tuple(sorted(values, key=lambda item: (-len(item), item)))
    normalized_tool_name = str(tool_name or "").strip().casefold()
    taint_keys = (
        _COMPUTER_USE_TAINT_VALUE_KEYS
        if normalized_tool_name in _COMPUTER_USE_TOOL_NAMES
        else _TAINT_VALUE_KEYS
    )
    numeric_taint_keys = (
        _COMPUTER_USE_NUMERIC_TAINT_KEYS if normalized_tool_name in _COMPUTER_USE_TOOL_NAMES else ()
    )
    _collect_taint_values(
        arguments,
        values,
        taint_keys=taint_keys,
        numeric_taint_keys=numeric_taint_keys,
    )
    if not (isinstance(result, dict) and "error" in result):
        if isinstance(result, str | bytes):
            _add_taint_variants(values, result)
        else:
            _collect_taint_values(
                result,
                values,
                taint_keys=taint_keys,
                numeric_taint_keys=numeric_taint_keys,
            )
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _collect_ephemeral_values(value: Any, values: set[str]) -> None:
    """A declared private tool may put private content in any value or error."""
    if isinstance(value, dict):
        for item in value.values():
            _collect_ephemeral_values(item, values)
    elif isinstance(value, list | tuple):
        for item in value:
            _collect_ephemeral_values(item, values)
    elif isinstance(value, str | bytes):
        _add_taint_variants(values, value)
        if isinstance(value, str) and value.startswith("[MCP_UNTRUSTED_TEXT]\n"):
            # The host wrapper is not private content. Parse its bounded body
            # so individual JSON field values cannot escape through an echo.
            body = value.partition("--- BEGIN UNTRUSTED MCP TEXT ---\n")[2]
            body = body.rpartition("\n--- END UNTRUSTED MCP TEXT ---")[0]
            if body:
                _collect_ephemeral_values(body, values)
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, UnicodeDecodeError):
            return
        if isinstance(decoded, dict | list):
            _collect_ephemeral_values(decoded, values)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        if math.isfinite(float(value)):
            values.add(json.dumps(value, ensure_ascii=False, allow_nan=False))


# Summary disclosure is independent of raw-tool retention. These values must
# remain private even when a tool explicitly allows requested-result prose.
_SUMMARY_SECRET_NAME = re.compile(
    r"(?:password|passwd|passcode|pin|secret|token|credential|api[ _-]?key|authorization|cookie)",
    re.I,
)
_SUMMARY_ASSIGNMENT = re.compile(
    r"(?:password|passwd|passcode|pin|secret|token|api[ _-]?key)\s*[:=]\s*[\"']?([^\s,;\"'}]+)",
    re.I,
)


def collect_requested_summary_protections(value: Any) -> tuple[str, ...]:
    values: set[str] = set()
    _collect_computer_use_private_identifier_values(value, values)

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            sensitive_control = (
                any(
                    isinstance(item.get(key), str) and _SUMMARY_SECRET_NAME.search(item[key])
                    for key in ("label", "title", "name", "help", "role")
                )
                or item.get("secure") is True
            )
            for key, child in item.items():
                if _SUMMARY_SECRET_NAME.search(str(key)) or (
                    sensitive_control and key in {"value", "text", "content"}
                ):
                    _collect_ephemeral_values(child, values)
                elif key in {"data", "bytes"} and item.get("type") == "image":
                    _collect_ephemeral_values(child, values)
                else:
                    visit(child)
        elif isinstance(item, list | tuple):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            for match in _SUMMARY_ASSIGNMENT.finditer(item):
                _add_taint_variants(values, match.group(1))
            if item.startswith("data:image/"):
                _add_taint_variants(values, item)
            # Protect whole raw envelopes and inspect structured private fields
            # inside the MCP untrusted-text wrapper; never persist that wrapper.
            candidate = item
            if item.startswith("[MCP_UNTRUSTED_TEXT]\n"):
                values.add(item)
                candidate = item.partition("--- BEGIN UNTRUSTED MCP TEXT ---\n")[2]
                candidate = candidate.rpartition("\n--- END UNTRUSTED MCP TEXT ---")[0]
            try:
                decoded = json.loads(candidate)
            except (ValueError, TypeError):
                return
            if isinstance(decoded, dict | list):
                values.add(candidate)
                _collect_computer_use_private_identifier_values(decoded, values)
                visit(decoded)

    visit(value)
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def requested_result_summary_text(response: Any, protected_taints: Collection[str]) -> str | None:
    if getattr(response, "tool_calls", None):
        return None
    text = str(getattr(response, "content", "") or "")
    if not text.strip():
        return None
    # Only narrative results use this path. Raw tool envelopes/screenshots are
    # never a final-answer format, even under the explicit summary contract.
    if re.search(r"(?:^|\n)\s*(?:```(?:json)?\s*\n)?[\[{]", text) or any(
        marker in text
        for marker in (
            "[MCP_UNTRUSTED_TEXT]",
            "snapshot_id",
            "element_id",
            "data:image/",
            '"elements":',
            '"structured_content":',
            '"window_id":',
        )
    ):
        return "App details are private. Please check the app for the requested result."
    from ..ide.protocol import redact_secrets
    from ..logging_redaction import redact_log_text

    return redact_log_text(redact_secrets(_redact_tainted_text(text, tuple(protected_taints))))


def _redact_tainted_text(text: str, taints: tuple[str, ...]) -> str:
    # Apply exact taints to the original text in one pass. Never rescan inserted
    # markers: short values must remain protected without corrupting the marker.
    candidates = sorted(
        {taint for taint in taints if taint and taint in text},
        key=lambda value: (-len(value), value),
    )
    if not candidates:
        return text
    matcher = re.compile("|".join(re.escape(taint) for taint in candidates))
    return _REDACTED_RESPONSE_TAINT.join(
        matcher.sub(lambda _: _REDACTED_RESPONSE_TAINT, part)
        for part in text.split(_REDACTED_RESPONSE_TAINT)
    )


def _redact_tainted_value(value: Any, taints: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        redacted = _redact_tainted_text(value, taints)
        return value if redacted == value else redacted
    if isinstance(value, bytes):
        decoded = value.decode("utf-8", errors="replace")
        redacted = _redact_tainted_text(decoded, taints)
        return value if redacted == decoded else redacted.encode("utf-8")
    if isinstance(value, dict):
        copied: dict[Any, Any] | None = None
        for key, item in value.items():
            redacted_item = _redact_tainted_value(item, taints)
            if redacted_item is not item and copied is None:
                copied = dict(value)
            if copied is not None:
                copied[key] = redacted_item
        return value if copied is None else copied
    if isinstance(value, list):
        redacted_items = [_redact_tainted_value(item, taints) for item in value]
        if all(
            redacted is original for redacted, original in zip(redacted_items, value, strict=True)
        ):
            return value
        return redacted_items
    if isinstance(value, tuple):
        redacted_items = tuple(_redact_tainted_value(item, taints) for item in value)
        if all(
            redacted is original for redacted, original in zip(redacted_items, value, strict=True)
        ):
            return value
        return redacted_items
    if is_dataclass(value) and not isinstance(value, type):
        updates: dict[str, Any] = {}
        for dataclass_field in fields(value):
            original = getattr(value, dataclass_field.name)
            redacted = _redact_tainted_value(original, taints)
            if redacted is not original:
                updates[dataclass_field.name] = redacted
        if not updates:
            return value
        try:
            return replace(value, **updates)
        except (TypeError, ValueError):
            return value
    return value


def redact_sensitive_exception_taints(error: BaseException, taints: Collection[str]) -> None:
    """Scrub approved request values echoed by provider errors before logging."""
    normalized = tuple(sorted((value for value in taints if value), key=len, reverse=True))
    if not normalized:
        return
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        current.args = _redact_tainted_value(current.args, normalized)
        for nested in (current.__cause__, current.__context__):
            if nested is not None:
                pending.append(nested)


def _protect_protocol_key(mapping: dict[str, Any], key: str) -> None:
    if key in mapping:
        mapping[key] = _ProtectedProtocolValue(mapping[key])


def _protect_protocol_keys(mapping: dict[str, Any], keys: Collection[str]) -> None:
    for key in keys:
        _protect_protocol_key(mapping, key)


def _protect_request_plan_linkage(metadata: dict[str, Any]) -> None:
    request_plan = metadata.get("request_plan")
    if isinstance(request_plan, dict):
        _protect_protocol_keys(
            request_plan,
            {"request_message_count", "request_messages_signature"},
        )


def _protect_openai_responses_linkage(metadata: dict[str, Any]) -> None:
    _protect_protocol_keys(
        metadata,
        {
            "response_id",
            "call_ids_rewritten",
            "call_id_normalization",
        },
    )
    _protect_request_plan_linkage(metadata)
    output_items = metadata.get("output_items")
    if not isinstance(output_items, list):
        return
    for item in output_items:
        if not isinstance(item, dict):
            continue
        # These values are provider-owned linkage and replay state, never UI
        # content. Arguments, message text, and reasoning summaries remain
        # unprotected and are taint-redacted below.
        item_type = str(item.get("type") or "")
        if item_type == "function_call":
            _protect_protocol_keys(item, {"type", "id", "call_id", "name", "status"})
        elif item_type == "reasoning":
            _protect_protocol_keys(item, {"type", "id", "status", "encrypted_content"})
        elif item_type == "message":
            _protect_protocol_keys(item, {"type", "id", "status", "role"})


def _protect_anthropic_messages_linkage(metadata: dict[str, Any]) -> None:
    _protect_protocol_key(metadata, "message_id")
    _protect_request_plan_linkage(metadata)
    content_blocks = metadata.get("content_blocks")
    if not isinstance(content_blocks, list):
        return
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type not in {
            "text",
            "tool_use",
            "server_tool_use",
            "thinking",
            "redacted_thinking",
            "web_search_tool_result",
        }:
            continue
        _protect_protocol_key(block, "type")
        if block_type in {"tool_use", "server_tool_use"}:
            _protect_protocol_keys(block, {"id", "name"})
        if block_type == "thinking":
            _protect_protocol_key(block, "signature")
        elif block_type == "redacted_thinking":
            _protect_protocol_key(block, "data")


def _protect_gemini_generate_content_linkage(metadata: dict[str, Any]) -> None:
    _protect_request_plan_linkage(metadata)
    # Thought signatures can occur both on the retained assistant content and
    # in per-tool metadata. They are opaque provider capabilities.
    _protect_protocol_keys(metadata, {"thoughtSignature", "thought_signature"})
    content = metadata.get("content")
    if not isinstance(content, dict):
        return
    if str(content.get("role") or "") != "model":
        return
    _protect_protocol_key(content, "role")
    parts = content.get("parts")
    if not isinstance(parts, list):
        return
    for part in parts:
        if not isinstance(part, dict):
            continue
        _protect_protocol_keys(part, {"thoughtSignature", "thought_signature"})
        function_call = part.get("functionCall")
        if isinstance(function_call, dict):
            _protect_protocol_keys(function_call, {"id", "name"})


def _protect_provider_linkage_in_place(metadata: dict[str, Any]) -> None:
    _protect_protocol_key(metadata, ROUTE_IDENTITY_PROVIDER_METADATA_KEY)

    ephemeral_linkage = metadata.get(_COMPUTER_USE_EPHEMERAL_LINKAGE_KEY)
    if isinstance(ephemeral_linkage, dict):
        _protect_protocol_key(ephemeral_linkage, "provider_call_id")

    responses_metadata = metadata.get(OPENAI_RESPONSES_PROVIDER_METADATA_KEY)
    if isinstance(responses_metadata, dict):
        _protect_openai_responses_linkage(responses_metadata)

    anthropic_metadata = metadata.get(ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY)
    if isinstance(anthropic_metadata, dict):
        _protect_anthropic_messages_linkage(anthropic_metadata)

    gemini_metadata = metadata.get(GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY)
    if isinstance(gemini_metadata, dict):
        _protect_gemini_generate_content_linkage(gemini_metadata)

    tool_call_entries = metadata.get(TOOL_CALL_PROVIDER_METADATA_KEY)
    if isinstance(tool_call_entries, list):
        for entry in tool_call_entries:
            if not isinstance(entry, dict):
                continue
            _protect_protocol_keys(entry, {"id", "index"})
            entry_metadata = entry.get("metadata")
            if isinstance(entry_metadata, dict):
                _protect_provider_linkage_in_place(entry_metadata)


def _restore_protocol_values(value: Any) -> Any:
    if isinstance(value, _ProtectedProtocolValue):
        return value.value
    if isinstance(value, dict):
        return {key: _restore_protocol_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_protocol_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_restore_protocol_values(item) for item in value)
    return value


def _redact_provider_metadata_taints(value: Any, taints: tuple[str, ...]) -> Any:
    if not isinstance(value, dict):
        return _redact_tainted_value(value, taints)
    masked = copy.deepcopy(value)
    _protect_provider_linkage_in_place(masked)
    restored = _restore_protocol_values(_redact_tainted_value(masked, taints))
    return value if restored == value else restored


_COMPUTER_USE_EPHEMERAL_LINKAGE_KEY = "_alysis_computer_use_ephemeral_linkage"
_PROVIDER_IDENTIFIER_KEYS = frozenset({"id", "call_id", "item_id", "message_id", "response_id"})
_PROVIDER_OPAQUE_REPLAY_KEYS = frozenset(
    {
        "data",
        "encrypted_content",
        "signature",
        "thoughtsignature",
        "thought_signature",
    }
)


def _collect_provider_identifier_values(value: Any, values: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if (
                normalized_key in _PROVIDER_IDENTIFIER_KEYS | _PROVIDER_OPAQUE_REPLAY_KEYS
                and isinstance(item, str)
                and item
            ):
                values.add(item)
            _collect_provider_identifier_values(item, values)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _collect_provider_identifier_values(item, values)


def collect_computer_use_provider_identifiers(response: Any) -> tuple[str, ...]:
    """Collect provider-owned linkage before it is removed from durable state."""

    values: set[str] = set()
    for tool_call in getattr(response, "tool_calls", None) or []:
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        if tool_call_id:
            values.add(tool_call_id)
        _collect_provider_identifier_values(getattr(tool_call, "provider_metadata", None), values)
    _collect_provider_identifier_values(getattr(response, "provider_metadata", None), values)
    _collect_provider_identifier_values(getattr(response, "raw", None), values)
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _computer_use_contract_error(code: str = "invalid_tool_linkage") -> ValueError:
    error = ValueError("Provider returned malformed Computer Use tool linkage")
    error.computer_use_error_code = code  # type: ignore[attr-defined]
    return error


def _native_tool_name_matches(value: Any, expected: str) -> bool:
    return str(value or "").strip().casefold() == expected.casefold()


def _native_tool_arguments_match(value: Any, expected: Any) -> bool:
    return _decoded_tool_arguments(value) == _decoded_tool_arguments(expected)


def _project_openai_computer_use_replay(
    metadata: dict[str, Any],
    *,
    provider_call_ids: list[str],
    tool_names: list[str],
    tool_arguments: list[Any],
) -> tuple[dict[str, Any], list[str]]:
    output_items = metadata.get("output_items")
    if not isinstance(output_items, list):
        raise _computer_use_contract_error()
    native_calls = [
        item
        for item in output_items
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]
    if len(native_calls) != len(provider_call_ids):
        raise _computer_use_contract_error()

    projected_items: list[dict[str, Any]] = []
    call_index = 0
    native_call_ids: list[str] = []
    for raw_item in output_items:
        if not isinstance(raw_item, dict):
            raise _computer_use_contract_error()
        item_type = str(raw_item.get("type") or "")
        if item_type == "function_call":
            native_id = str(raw_item.get("call_id") or raw_item.get("id") or "")
            if (
                not native_id
                or native_id != provider_call_ids[call_index]
                or not _native_tool_name_matches(raw_item.get("name"), tool_names[call_index])
                or not _native_tool_arguments_match(
                    raw_item.get("arguments"), tool_arguments[call_index]
                )
            ):
                raise _computer_use_contract_error("provider_tool_metadata_mismatch")
            projected_call: dict[str, Any] = {
                "type": "function_call",
                "call_id": native_id,
                "name": tool_names[call_index],
                "arguments": copy.deepcopy(raw_item.get("arguments")),
                "status": "completed",
            }
            item_id = raw_item.get("id")
            if isinstance(item_id, str) and item_id:
                projected_call["id"] = item_id
            projected_items.append(projected_call)
            native_call_ids.append(native_id)
            call_index += 1
            continue
        if item_type == "reasoning":
            item_id = raw_item.get("id")
            encrypted_content = raw_item.get("encrypted_content")
            if not isinstance(encrypted_content, str) or not encrypted_content:
                raise _computer_use_contract_error("reasoning_encryption_missing")
            if not isinstance(item_id, str) or not item_id:
                raise _computer_use_contract_error()
            summary = raw_item.get("summary", [])
            if not isinstance(summary, list) or summary:
                # Computer Use requests explicitly suppress readable reasoning.
                # A provider that nevertheless returns a summary must not gain
                # a transient or durable side channel for host-screen content.
                raise _computer_use_contract_error("reasoning_summary_refused")
            projected_reasoning = {
                "type": "reasoning",
                "encrypted_content": encrypted_content,
                "summary": [],
                "status": "completed",
            }
            projected_reasoning["id"] = item_id
            projected_items.append(projected_reasoning)
            continue
        if item_type == "message":
            item_id = raw_item.get("id")
            content = raw_item.get("content")
            if not isinstance(item_id, str) or not item_id or not isinstance(content, list):
                raise _computer_use_contract_error()
            projected_content: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    raise _computer_use_contract_error()
                part_type = str(part.get("type") or "")
                if part_type == "output_text":
                    text = part.get("text")
                    annotations = part.get("annotations", [])
                    if (
                        not isinstance(text, str)
                        or not isinstance(annotations, list)
                        or annotations
                    ):
                        raise _computer_use_contract_error()
                    projected_content.append(
                        {"type": "output_text", "text": text, "annotations": []}
                    )
                    continue
                if part_type == "refusal":
                    refusal = part.get("refusal")
                    if not isinstance(refusal, str):
                        raise _computer_use_contract_error()
                    projected_content.append({"type": "refusal", "refusal": refusal})
                    continue
                else:
                    raise _computer_use_contract_error()
            projected_items.append(
                {
                    "type": "message",
                    "id": item_id,
                    "role": "assistant",
                    "status": "completed",
                    "content": projected_content,
                }
            )
            continue
        raise _computer_use_contract_error()
    return {"output_items": projected_items, "call_ids_rewritten": True}, native_call_ids


def _project_anthropic_computer_use_replay(
    metadata: dict[str, Any],
    *,
    provider_call_ids: list[str],
    tool_names: list[str],
    tool_arguments: list[Any],
) -> tuple[dict[str, Any], list[str]]:
    content_blocks = metadata.get("content_blocks")
    if not isinstance(content_blocks, list):
        raise _computer_use_contract_error()
    native_calls = [
        block
        for block in content_blocks
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if len(native_calls) != len(provider_call_ids):
        raise _computer_use_contract_error()

    projected_blocks: list[dict[str, Any]] = []
    native_call_ids: list[str] = []
    call_index = 0
    for raw_block in content_blocks:
        if not isinstance(raw_block, dict):
            raise _computer_use_contract_error()
        block_type = str(raw_block.get("type") or "")
        if block_type == "tool_use":
            native_id = str(raw_block.get("id") or "")
            if (
                not native_id
                or native_id != provider_call_ids[call_index]
                or not _native_tool_name_matches(raw_block.get("name"), tool_names[call_index])
                or not _native_tool_arguments_match(
                    raw_block.get("input"), tool_arguments[call_index]
                )
            ):
                raise _computer_use_contract_error()
            projected_blocks.append(
                {
                    "type": "tool_use",
                    "id": native_id,
                    "name": tool_names[call_index],
                    "input": copy.deepcopy(raw_block.get("input")),
                }
            )
            native_call_ids.append(native_id)
            call_index += 1
            continue
        if block_type == "thinking":
            if set(raw_block) - {"type", "thinking", "signature"}:
                raise _computer_use_contract_error()
            if raw_block.get("thinking") != "" or not isinstance(raw_block.get("signature"), str):
                raise _computer_use_contract_error()
            projected_blocks.append(copy.deepcopy(raw_block))
            continue
        if block_type == "redacted_thinking":
            if set(raw_block) != {"type", "data"} or not isinstance(raw_block.get("data"), str):
                raise _computer_use_contract_error()
            projected_blocks.append(copy.deepcopy(raw_block))
            continue
        if block_type == "text":
            if not isinstance(raw_block.get("text"), str):
                raise _computer_use_contract_error()
            projected_blocks.append({"type": "text", "text": raw_block["text"]})
            continue
        raise _computer_use_contract_error()
    return {"content_blocks": projected_blocks}, native_call_ids


def _project_gemini_computer_use_replay(
    metadata: dict[str, Any],
    *,
    provider_call_ids: list[str],
    tool_names: list[str],
    tool_arguments: list[Any],
) -> tuple[dict[str, Any], list[str]]:
    content = metadata.get("content")
    if not isinstance(content, dict) or str(content.get("role") or "") != "model":
        raise _computer_use_contract_error()
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise _computer_use_contract_error()
    native_calls = []
    for part in parts:
        function_call = part.get("functionCall") if isinstance(part, dict) else None
        if isinstance(function_call, dict):
            native_calls.append(function_call)
    if len(native_calls) != len(provider_call_ids):
        raise _computer_use_contract_error()

    projected_parts: list[dict[str, Any]] = []
    native_call_ids: list[str] = []
    call_index = 0
    for raw_part in parts:
        if not isinstance(raw_part, dict):
            raise _computer_use_contract_error()
        if "partMetadata" in raw_part and not isinstance(raw_part.get("partMetadata"), dict):
            raise _computer_use_contract_error()
        function_call = raw_part.get("functionCall")
        if isinstance(function_call, dict):
            if set(raw_part) - {
                "functionCall",
                "thought",
                "thoughtSignature",
                "thought_signature",
                "partMetadata",
            }:
                raise _computer_use_contract_error()
            if set(function_call) - {"id", "name", "args"}:
                raise _computer_use_contract_error()
            native_id = str(function_call.get("id") or "")
            if (
                (native_id and native_id != provider_call_ids[call_index])
                or str(function_call.get("name") or "") != tool_names[call_index]
                or not _native_tool_arguments_match(
                    function_call.get("args"), tool_arguments[call_index]
                )
            ):
                raise _computer_use_contract_error()
            # All remaining keys are allowlisted and validated above. Preserve
            # their original ordering and representation so the signed Part is
            # byte-identical when serialized by the same JSON encoder later.
            projected_part = copy.deepcopy(raw_part)
            projected_parts.append(projected_part)
            # ID-less FunctionCalls are valid Gemini wire objects. The adapter
            # gives their public ToolCall a response-unique local ID; retain it
            # only for in-process linkage while keeping the signed native Part
            # exactly ID-less.
            native_call_ids.append(native_id or provider_call_ids[call_index])
            call_index += 1
            continue
        if set(raw_part) - {
            "text",
            "thought",
            "thoughtSignature",
            "thought_signature",
            "partMetadata",
        }:
            raise _computer_use_contract_error()
        text = raw_part.get("text")
        if raw_part.get("thought") is True and isinstance(text, str) and text:
            raise _computer_use_contract_error()
        if text is not None and not isinstance(text, str):
            raise _computer_use_contract_error()
        projected_parts.append(copy.deepcopy(raw_part))
    return {"content": {"role": "model", "parts": projected_parts}}, native_call_ids


def _gemini_function_call_id_omissions(
    provider_metadata: Any,
    *,
    expected_count: int,
) -> list[bool]:
    if not isinstance(provider_metadata, dict):
        return [False] * expected_count
    gemini = provider_metadata.get(GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY)
    content = gemini.get("content") if isinstance(gemini, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return [False] * expected_count
    omissions = [
        function_call.get("id") in {None, ""}
        for part in parts
        if isinstance(part, dict) and isinstance((function_call := part.get("functionCall")), dict)
    ]
    return omissions if len(omissions) == expected_count else [False] * expected_count


def _project_computer_use_provider_metadata(
    provider_metadata: Any,
    *,
    provider_call_ids: list[str],
    tool_names: list[str],
    tool_arguments: list[Any],
) -> tuple[dict[str, Any] | None, list[str] | None]:
    if not isinstance(provider_metadata, dict):
        return None, None
    projected: dict[str, Any] = {}
    route_identity = provider_metadata.get(ROUTE_IDENTITY_PROVIDER_METADATA_KEY)
    if isinstance(route_identity, dict):
        version = route_identity.get("version")
        fingerprint = route_identity.get("fingerprint")
        if version == 1 and isinstance(fingerprint, str) and fingerprint:
            projected[ROUTE_IDENTITY_PROVIDER_METADATA_KEY] = {
                "version": 1,
                "fingerprint": fingerprint,
            }

    native_call_ids: list[str] | None = None
    projectors = (
        (OPENAI_RESPONSES_PROVIDER_METADATA_KEY, _project_openai_computer_use_replay),
        (ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY, _project_anthropic_computer_use_replay),
        (GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY, _project_gemini_computer_use_replay),
    )
    for key, projector in projectors:
        metadata = provider_metadata.get(key)
        if metadata is None:
            continue
        if not isinstance(metadata, dict):
            raise _computer_use_contract_error()
        projected_metadata, candidate_ids = projector(
            metadata,
            provider_call_ids=provider_call_ids,
            tool_names=tool_names,
            tool_arguments=tool_arguments,
        )
        if native_call_ids is not None and candidate_ids != native_call_ids:
            raise _computer_use_contract_error()
        native_call_ids = candidate_ids
        projected[key] = projected_metadata
    return projected or None, native_call_ids


def _replace_computer_use_response(
    response: Any,
    *,
    tool_calls: list[Any],
    provider_metadata: Any,
    response_model: Any = None,
    reasoning: Any = (),
) -> Any:
    try:
        return replace(
            response,
            tool_calls=tool_calls,
            raw={},
            response_model=response_model,
            provider_metadata=provider_metadata,
            reasoning=reasoning,
        )
    except (TypeError, ValueError):
        return _PersistenceSafeResponse(
            content=str(getattr(response, "content", "") or ""),
            tool_calls=tool_calls,
            raw={},
            response_model=response_model,
            usage=copy.deepcopy(getattr(response, "usage", None)),
            provider_metadata=provider_metadata,
            reasoning=reasoning,
            assistant_phase=copy.deepcopy(getattr(response, "assistant_phase", None)),
        )


def canonicalize_computer_use_response_for_dispatch(
    response: Any,
    *,
    used_protocol_ids: set[str] | None = None,
    allowed_tool_names: Collection[str] | None = None,
) -> Any:
    """Own Computer Use linkage IDs and reject tools outside the fixed contract.

    Provider output is untrusted. A model must not be able to persist UI data by
    placing it in a syntactically valid tool ID or tool name. Replace every
    Computer Use call ID with a fresh Alysis-owned opaque identifier and
    canonicalize known tool names. Provider-native linkage and signed replay
    blocks remain exact only inside the transient response envelope; they are
    never used as durable public IDs. Arguments remain the original objects for
    byte-exact trusted dispatch.
    """

    response_tool_calls = list(getattr(response, "tool_calls", []) or [])
    if not response_tool_calls:
        return _replace_computer_use_response(
            response,
            tool_calls=[],
            provider_metadata=None,
        )
    canonical_names = {
        name.casefold(): name
        for name in (_COMPUTER_USE_TOOL_NAMES if allowed_tool_names is None else allowed_tool_names)
    }
    tool_names: list[str] = []
    for tool_call in response_tool_calls:
        normalized_name = str(getattr(tool_call, "name", "") or "").strip().casefold()
        canonical_name = canonical_names.get(normalized_name)
        if canonical_name is None:
            error = ValueError("Provider returned a tool outside the fixed Computer Use contract")
            error.computer_use_error_code = "unknown_computer_use_tool"  # type: ignore[attr-defined]
            raise error
        tool_names.append(canonical_name)

    provider_call_ids = [
        str(getattr(tool_call, "id", "") or "") for tool_call in response_tool_calls
    ]
    if any(not call_id for call_id in provider_call_ids):
        raise _computer_use_contract_error("missing_tool_call_id")
    tool_arguments = [getattr(tool_call, "arguments", None) for tool_call in response_tool_calls]
    seen = used_protocol_ids if used_protocol_ids is not None else set()
    if len(set(provider_call_ids)) != len(provider_call_ids) or any(
        call_id in seen for call_id in provider_call_ids
    ):
        # A full stateless replay cannot disambiguate two native calls with the
        # same provider ID, and opaque provider signatures prevent renaming that
        # ID safely. Reject reuse before any host action is dispatched.
        raise _computer_use_contract_error("reused_tool_call_id")
    # Validate the entire batch and provider-native envelope transactionally.
    # Rejection must not reserve either native IDs or generated public IDs.
    pending_seen = set(seen)
    pending_seen.update(provider_call_ids)
    call_ids = [
        _new_computer_use_protocol_id("call", pending_seen) for _tool_call in response_tool_calls
    ]
    provider_metadata, native_call_ids = _project_computer_use_provider_metadata(
        getattr(response, "provider_metadata", None),
        provider_call_ids=provider_call_ids,
        tool_names=tool_names,
        tool_arguments=tool_arguments,
    )
    replay_call_ids = native_call_ids if native_call_ids is not None else call_ids
    provider_call_id_omitted = _gemini_function_call_id_omissions(
        provider_metadata,
        expected_count=len(response_tool_calls),
    )

    canonical_calls: list[Any] = []
    for index, tool_call in enumerate(response_tool_calls):
        linkage: dict[str, Any] = {
            "provider_call_id": replay_call_ids[index],
        }
        if provider_call_id_omitted[index]:
            linkage["provider_call_id_omitted"] = True
        tool_metadata = {
            _COMPUTER_USE_EPHEMERAL_LINKAGE_KEY: linkage,
        }
        try:
            canonical_calls.append(
                replace(
                    tool_call,
                    id=call_ids[index],
                    name=tool_names[index],
                    provider_metadata=tool_metadata,
                )
            )
        except (TypeError, ValueError):
            canonical_calls.append(
                _PersistenceSafeToolCall(
                    id=call_ids[index],
                    name=tool_names[index],
                    arguments=getattr(tool_call, "arguments", None),
                    provider_metadata=tool_metadata,
                )
            )

    canonical_response = _replace_computer_use_response(
        response,
        tool_calls=canonical_calls,
        provider_metadata=provider_metadata,
        response_model=copy.deepcopy(getattr(response, "response_model", None)),
        reasoning=copy.deepcopy(getattr(response, "reasoning", ())),
    )
    seen.update(pending_seen)
    return canonical_response


def computer_use_ephemeral_provider_replay_state(response: Any) -> dict[str, dict[str, Any]]:
    """Capture minimum native replay state in run-turn memory only."""

    provider_metadata = getattr(response, "provider_metadata", None)
    states: dict[str, dict[str, Any]] = {}
    for tool_call in getattr(response, "tool_calls", None) or []:
        canonical_call_id = str(getattr(tool_call, "id", "") or "")
        tool_metadata = getattr(tool_call, "provider_metadata", None)
        linkage = (
            tool_metadata.get(_COMPUTER_USE_EPHEMERAL_LINKAGE_KEY)
            if isinstance(tool_metadata, dict)
            else None
        )
        provider_call_id = (
            str(linkage.get("provider_call_id") or "") if isinstance(linkage, dict) else ""
        )
        if not canonical_call_id or not provider_call_id:
            raise _computer_use_contract_error()
        state: dict[str, Any] = {
            "provider_call_id": provider_call_id,
            # ``canonicalize_computer_use_response_for_dispatch`` already
            # schema-projects this envelope and validates its call name,
            # arguments, and linkage.  Preserve that native representation
            # exactly; rebuilding it from the public ToolCall can invalidate
            # provider signatures even when the decoded arguments compare
            # equal.
            "provider_metadata": copy.deepcopy(provider_metadata),
        }
        if isinstance(linkage, dict) and linkage.get("provider_call_id_omitted") is True:
            state["provider_call_id_omitted"] = True
        states[canonical_call_id] = state
    return states


def strip_computer_use_provider_state_for_persistence(response: Any) -> Any:
    """Remove all native replay capabilities from durable Computer Use state."""

    stripped_calls: list[Any] = []
    for tool_call in getattr(response, "tool_calls", None) or []:
        try:
            stripped_calls.append(replace(tool_call, provider_metadata=None))
        except (TypeError, ValueError):
            stripped_calls.append(
                _PersistenceSafeToolCall(
                    id=str(getattr(tool_call, "id", "") or ""),
                    name=str(getattr(tool_call, "name", "") or ""),
                    arguments=copy.deepcopy(getattr(tool_call, "arguments", None)),
                    provider_metadata=None,
                )
            )
    return _replace_computer_use_response(
        response,
        tool_calls=stripped_calls,
        provider_metadata=None,
    )


def inject_ephemeral_computer_use_provider_state(
    messages: list[dict[str, Any]],
    replay_state: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Restore provider-native replay and IDs in a disposable request copy."""

    if not replay_state:
        return messages
    injected = copy.deepcopy(messages)
    canonical_to_provider: dict[str, str] = {}
    for message in injected:
        if str(message.get("role") or "") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        matching_state: Mapping[str, Any] | None = None
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            canonical_call_id = str(tool_call.get("id") or "")
            state = replay_state.get(canonical_call_id)
            if not isinstance(state, Mapping):
                continue
            provider_call_id = str(state.get("provider_call_id") or "")
            if not provider_call_id:
                continue
            if matching_state is not None and state.get("provider_metadata") != matching_state.get(
                "provider_metadata"
            ):
                raise _computer_use_contract_error()
            matching_state = state
            if state.get("provider_call_id_omitted") is not True:
                canonical_to_provider[canonical_call_id] = provider_call_id
        if matching_state is None:
            continue
        # The replay envelope was projected from the live provider response and
        # may contain signatures over the exact native arguments.  Never rebuild
        # it from the redacted durable assistant projection: Gemini revalidates
        # older signed Parts on N+2 requests, and even a semantically equivalent
        # rewrite can invalidate that opaque signature.
        provider_metadata = copy.deepcopy(matching_state.get("provider_metadata"))
        if provider_metadata:
            message[PROVIDER_METADATA_KEY] = provider_metadata

    for message in injected:
        if str(message.get("role") or "") != "tool":
            continue
        canonical_call_id = str(message.get("tool_call_id") or message.get("call_id") or "")
        provider_call_id = canonical_to_provider.get(canonical_call_id)
        if not provider_call_id:
            continue
        if "tool_call_id" in message:
            message["tool_call_id"] = provider_call_id
        if "call_id" in message:
            message["call_id"] = provider_call_id
    return injected


def scrub_consumed_computer_use_provider_state(
    messages: list[dict[str, Any]],
    replay_state: Mapping[str, Mapping[str, Any]],
    *,
    tool_metadata_by_name: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Destroy native replay material in a provider-consumed request copy.

    Provider clients are synchronous at this boundary, but transports, test
    doubles, and diagnostics may retain the list object they were handed.  A
    Computer Use request therefore gets scrubbed in place as soon as the call
    returns: assistant arguments are redacted, native replay metadata is
    removed, and tool-result linkage is restored to the Alysis-owned ID.
    """

    if not replay_state:
        return
    provider_to_canonical = {
        str(state.get("provider_call_id") or ""): str(canonical_call_id)
        for canonical_call_id, state in replay_state.items()
        if isinstance(state, Mapping)
        and str(canonical_call_id)
        and str(state.get("provider_call_id") or "")
    }
    canonical_call_ids = set(replay_state)
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list) or not any(
                isinstance(call, dict) and str(call.get("id") or "") in canonical_call_ids
                for call in tool_calls
            ):
                continue
            scrubbed = copy.deepcopy(
                redact_assistant_tool_call_message(
                    message, tool_metadata_by_name=tool_metadata_by_name
                )
            )
            scrubbed.pop(PROVIDER_METADATA_KEY, None)
            messages[index] = scrubbed
            continue
        if role != "tool":
            continue
        for key in ("tool_call_id", "call_id"):
            provider_call_id = str(message.get(key) or "")
            canonical_call_id = provider_to_canonical.get(provider_call_id)
            if canonical_call_id:
                message[key] = canonical_call_id


def redact_sensitive_response_taints(response: Any, taints: Collection[str]) -> Any:
    """Redact exact approved values from every provider-response channel."""

    normalized = tuple(
        sorted(
            {str(value) for value in taints if isinstance(value, str) and value},
            key=lambda item: (-len(item), item),
        )
    )
    if not normalized:
        return response
    if not any(
        hasattr(response, field_name)
        for field_name in ("content", "tool_calls", "raw", "provider_metadata")
    ):
        return _redact_tainted_value(response, normalized)
    content = _redact_tainted_value(str(getattr(response, "content", "") or ""), normalized)
    raw = _redact_tainted_value(getattr(response, "raw", {}), normalized)
    response_tool_calls = list(getattr(response, "tool_calls", []) or [])
    tool_calls: list[Any] = []
    calls_changed = False
    for tool_call in response_tool_calls:
        arguments = _redact_tainted_value(getattr(tool_call, "arguments", None), normalized)
        provider_metadata = _redact_provider_metadata_taints(
            getattr(tool_call, "provider_metadata", None), normalized
        )
        if arguments is getattr(tool_call, "arguments", None) and provider_metadata is getattr(
            tool_call, "provider_metadata", None
        ):
            tool_calls.append(tool_call)
            continue
        calls_changed = True
        try:
            tool_calls.append(
                replace(
                    tool_call,
                    arguments=arguments,
                    provider_metadata=provider_metadata,
                )
            )
        except (TypeError, ValueError):
            tool_calls.append(
                _PersistenceSafeToolCall(
                    id=str(getattr(tool_call, "id", "") or ""),
                    name=str(getattr(tool_call, "name", "") or ""),
                    arguments=arguments,
                    provider_metadata=provider_metadata,
                )
            )
    provider_metadata = _redact_provider_metadata_taints(
        getattr(response, "provider_metadata", None), normalized
    )
    reasoning = _redact_tainted_value(getattr(response, "reasoning", ()), normalized)
    usage = _redact_tainted_value(getattr(response, "usage", None), normalized)
    changed = (
        content != str(getattr(response, "content", "") or "")
        or raw is not getattr(response, "raw", {})
        or calls_changed
        or provider_metadata is not getattr(response, "provider_metadata", None)
        or reasoning is not getattr(response, "reasoning", ())
        or usage is not getattr(response, "usage", None)
    )
    if not changed:
        return response
    return _PersistenceSafeResponse(
        content=content,
        tool_calls=tool_calls,
        raw=raw,
        response_model=copy.deepcopy(getattr(response, "response_model", None)),
        usage=usage,
        provider_metadata=provider_metadata,
        reasoning=reasoning,
        assistant_phase=copy.deepcopy(getattr(response, "assistant_phase", None)),
    )


def _decoded_tool_arguments(value: Any) -> Any:
    if not isinstance(value, str):
        return copy.deepcopy(value)
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"_raw_arguments": value}


def _rewrite_provider_native_tool_arguments(
    provider_metadata: Any,
    arguments_by_call_id: Mapping[str, Any],
) -> Any:
    """Keep provider-native replays on the same sensitive-argument boundary.

    OpenAI Responses, Anthropic Messages, and Gemini GenerateContent retain
    native assistant blocks in provider metadata because opaque
    reasoning/signature state may be required on the next request. Those blocks
    also repeat function arguments. Redacting only the public ``tool_calls``
    projection would therefore leave a second raw copy in durable history, and
    each adapter would prefer that copy on replay. Rewrite only the native
    tool-input field while preserving all unrelated opaque provider state.
    """

    if not isinstance(provider_metadata, dict) or not arguments_by_call_id:
        return provider_metadata
    normalized_arguments = {
        str(call_id): _decoded_tool_arguments(arguments)
        for call_id, arguments in arguments_by_call_id.items()
        if str(call_id)
    }
    if not normalized_arguments:
        return provider_metadata

    copied = copy.deepcopy(provider_metadata)
    responses_metadata = copied.get(OPENAI_RESPONSES_PROVIDER_METADATA_KEY)
    if isinstance(responses_metadata, dict):
        output_items = responses_metadata.get("output_items")
        if isinstance(output_items, list):
            for item in output_items:
                if not isinstance(item, dict) or item.get("type") != "function_call":
                    continue
                call_id = str(item.get("call_id") or item.get("id") or "")
                if call_id not in normalized_arguments:
                    continue
                item["arguments"] = json.dumps(
                    normalized_arguments[call_id],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

    anthropic_metadata = copied.get(ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY)
    if isinstance(anthropic_metadata, dict):
        content_blocks = anthropic_metadata.get("content_blocks")
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                call_id = str(block.get("id") or "")
                if call_id in normalized_arguments:
                    block["input"] = copy.deepcopy(normalized_arguments[call_id])

    gemini_metadata = copied.get(GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY)
    if isinstance(gemini_metadata, dict):
        content = gemini_metadata.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                function_call = part.get("functionCall")
                if not isinstance(function_call, dict):
                    continue
                call_id = str(function_call.get("id") or "")
                if call_id in normalized_arguments:
                    function_call["args"] = copy.deepcopy(normalized_arguments[call_id])
    return copied


def redact_assistant_tool_call_message(
    message: dict[str, Any],
    *,
    tool_metadata_by_name: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Redact sensitive call bodies while preserving provider call IDs and shape."""

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return message
    copied: dict[str, Any] | None = None
    copied_calls: list[Any] = []
    redacted_arguments_by_call_id: dict[str, Any] = {}
    for call in tool_calls:
        if not isinstance(call, dict):
            copied_calls.append(copy.deepcopy(call))
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            copied_calls.append(copy.deepcopy(call))
            continue
        tool_name = str(function.get("name") or "")
        serialized = function.get("arguments")
        try:
            arguments = json.loads(serialized) if isinstance(serialized, str) else serialized
        except (TypeError, ValueError, json.JSONDecodeError):
            arguments = {}
        boundary = sensitive_tool_boundary(
            tool_name, arguments, tool_metadata=(tool_metadata_by_name or {}).get(tool_name)
        )
        if not boundary.sensitive:
            copied_calls.append(copy.deepcopy(call))
            continue
        if copied is None:
            copied = copy.deepcopy(message)
        copied_call = copy.deepcopy(call)
        copied_function = dict(copied_call["function"])
        redacted = redact_sensitive_tool_arguments(
            tool_name,
            arguments,
            boundary=boundary,
        )
        copied_function["arguments"] = json.dumps(redacted, ensure_ascii=False)
        copied_call["function"] = copied_function
        copied_calls.append(copied_call)
        call_id = str(call.get("id") or call.get("call_id") or "")
        if call_id:
            redacted_arguments_by_call_id[call_id] = redacted
    if copied is None:
        return message
    copied["content"] = ""
    copied["tool_calls"] = copied_calls
    if PROVIDER_METADATA_KEY in copied:
        copied[PROVIDER_METADATA_KEY] = _rewrite_provider_native_tool_arguments(
            copied.get(PROVIDER_METADATA_KEY),
            redacted_arguments_by_call_id,
        )
    return copied


def redact_sensitive_response_for_persistence(
    response: Any,
    *,
    taints: Collection[str] = (),
    tool_metadata_by_name: Mapping[str, Mapping[str, Any]] | None = None,
) -> Any:
    """Return a usage/persistence-safe response while preserving accounting fields."""

    response = redact_sensitive_response_taints(response, taints)

    tool_calls = getattr(response, "tool_calls", None)
    if not isinstance(tool_calls, list):
        return response
    copied_calls: list[Any] = []
    redacted_arguments_by_call_id: dict[str, Any] = {}
    changed = False
    for tool_call in tool_calls:
        tool_name = str(getattr(tool_call, "name", "") or "")
        arguments = getattr(tool_call, "arguments", None)
        boundary = sensitive_tool_boundary(
            tool_name, arguments, tool_metadata=(tool_metadata_by_name or {}).get(tool_name)
        )
        if not boundary.sensitive:
            copied_calls.append(tool_call)
            continue
        changed = True
        redacted_arguments = redact_sensitive_tool_arguments(
            tool_name,
            arguments,
            boundary=boundary,
        )
        call_id = str(getattr(tool_call, "id", "") or "")
        if call_id:
            redacted_arguments_by_call_id[call_id] = redacted_arguments
        try:
            copied_calls.append(replace(tool_call, arguments=redacted_arguments))
        except (TypeError, ValueError):
            copied_calls.append(
                _PersistenceSafeToolCall(
                    id=str(getattr(tool_call, "id", "") or ""),
                    name=tool_name,
                    arguments=redacted_arguments,
                    provider_metadata=copy.deepcopy(getattr(tool_call, "provider_metadata", None)),
                )
            )
    if not changed:
        return response
    provider_metadata = _rewrite_provider_native_tool_arguments(
        getattr(response, "provider_metadata", None),
        redacted_arguments_by_call_id,
    )
    try:
        # Provider-native raw bodies commonly repeat function arguments. They
        # are not needed for usage accounting and cannot safely be retained.
        return replace(
            response,
            content="",
            tool_calls=copied_calls,
            raw={},
            provider_metadata=provider_metadata,
        )
    except (TypeError, ValueError):
        return _PersistenceSafeResponse(
            content="",
            tool_calls=copied_calls,
            raw={},
            response_model=copy.deepcopy(getattr(response, "response_model", None)),
            usage=copy.deepcopy(getattr(response, "usage", None)),
            provider_metadata=provider_metadata,
            reasoning=copy.deepcopy(getattr(response, "reasoning", ())),
            assistant_phase=copy.deepcopy(getattr(response, "assistant_phase", None)),
        )


def redact_consumed_sensitive_tool_messages(
    messages: list[dict[str, Any]],
    result_stubs: dict[str, str],
    *,
    tool_metadata_by_name: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Scrub one-call-only sensitive messages after the provider consumed them."""

    if not result_stubs:
        return
    call_ids = set(result_stubs)
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id in result_stubs:
                message["content"] = result_stubs[call_id]
            continue
        if str(message.get("role") or "") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        if any(
            isinstance(call, dict) and str(call.get("id") or "") in call_ids for call in tool_calls
        ):
            messages[index] = redact_assistant_tool_call_message(
                message, tool_metadata_by_name=tool_metadata_by_name
            )


def inject_ephemeral_sensitive_tool_messages(
    messages: list[dict[str, Any]],
    *,
    result_content: dict[str, str],
    arguments_content: dict[str, str],
) -> list[dict[str, Any]]:
    """Expose sensitive tool material to one provider request, without mutating history."""

    call_ids = set(result_content) | set(arguments_content)
    if not call_ids:
        return messages
    injected = copy.deepcopy(messages)
    for message in injected:
        if str(message.get("role") or "") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id in result_content:
                message["content"] = result_content[call_id]
            continue
        if str(message.get("role") or "") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            if call_id not in arguments_content:
                continue
            function = call.get("function")
            if isinstance(function, dict):
                function["arguments"] = arguments_content[call_id]
        if PROVIDER_METADATA_KEY in message:
            message[PROVIDER_METADATA_KEY] = _rewrite_provider_native_tool_arguments(
                message.get(PROVIDER_METADATA_KEY),
                arguments_content,
            )
    return injected


__all__ = [
    "SensitiveToolBoundary",
    "canonicalize_computer_use_response_for_dispatch",
    "collect_computer_use_provider_identifiers",
    "collect_computer_use_private_identifiers",
    "collect_sensitive_response_taints",
    "computer_use_ephemeral_provider_replay_state",
    "inject_ephemeral_computer_use_provider_state",
    "inject_ephemeral_sensitive_tool_messages",
    "redact_assistant_tool_call_message",
    "redact_consumed_sensitive_tool_messages",
    "redact_sensitive_exception_taints",
    "redact_sensitive_tool_arguments",
    "redact_sensitive_tool_result",
    "redact_sensitive_response_for_persistence",
    "redact_sensitive_response_taints",
    "scrub_consumed_computer_use_provider_state",
    "sensitive_result_stub",
    "sensitive_tool_boundary",
    "strip_computer_use_provider_state_for_persistence",
]
