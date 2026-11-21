"""Validation and response filtering shared by IDE management handlers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from ..config import ConfigError
from ..workspace_binding import WorkspaceAction, WorkspaceBindingError
from ..workspace_binding_ui import resolve_startup_workspace_binding
from .protocol import ProtocolError, RequestId, redact_secrets

_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b([A-Z0-9_.-]*(?:api[_-]?key|token|secret|password|credential)[A-Z0-9_.-]*\s*[:=]\s*)([^\s,;&]+)"
)


_URL_USERINFO_PATTERN = re.compile(
    r"(?i)\b((?:git\+)?https?://)([^/@\s]+)@([A-Za-z0-9.-]+(?::\d+)?)"
)


MUTATING_MANAGEMENT_METHODS = frozenset(
    {
        "skill.import.package",
        "skill.import.preview",
        "skill.import.install",
        "skill.import.discard",
        "skill.document.save",
        "skill.update.preview",
        "hooks.document.save",
        "config.set",
        "profile.add",
        "profile.remove",
        "profile.use",
        "profile.selectModel",
        "provider.auth.selectModel",
        "profile.rename",
        "profile.preset",
        "profile.convert",
        "tool.trust",
        "tool.untrust",
        "skill.init",
        "skill.install",
        "skill.enable",
        "skill.disable",
        "skill.remove",
        "sandbox.setup",
        "sandbox.pull",
        "report.create",
        "mcp.auth.login.start",
        "mcp.auth.login.cancel",
        "mcp.auth.logout",
        "hooks.trust",
        "hooks.untrust",
        "hooks.init",
        "hooks.enable",
        "hooks.disable",
        "ext.install",
        "ext.custom.save",
        "ext.uninstall",
        "ext.enable",
        "ext.disable",
    }
)


WORKSPACE_REQUIRED_MANAGEMENT_METHODS = frozenset(
    {
        "skill.import.describe",
        "skill.import.package",
        "skill.import.preview",
        "skill.import.install",
        "skill.import.discard",
        "skill.document.read",
        "skill.document.save",
        "skill.update.preview",
        "hooks.document.read",
        "hooks.document.save",
        "tool.list",
        "tool.info",
        "tool.trust",
        "tool.untrust",
        "skill.list",
        "skill.info",
        "skill.init",
        "skill.validate",
        "skill.install",
        "skill.enable",
        "skill.disable",
        "skill.remove",
        "report.create",
        "mcp.status",
        "mcp.prompts.list",
        "mcp.prompts.get",
        "mcp.auth.status",
        "mcp.auth.login.start",
        "mcp.auth.login.status",
        "mcp.auth.login.cancel",
        "mcp.auth.logout",
        "hooks.list",
        "hooks.doctor",
        "hooks.test",
        "hooks.trust",
        "hooks.untrust",
        "hooks.init",
        "hooks.effective",
        "hooks.enable",
        "hooks.disable",
        "conventions.list",
        "conventions.render",
        "ext.info",
        "ext.list",
        "ext.install",
        "ext.custom.save",
        "ext.uninstall",
        "ext.enable",
        "ext.disable",
    }
)


def _workspace_root_from_params(
    params: dict[str, Any],
    *,
    request_id: RequestId,
    required: bool,
) -> Path:
    raw = params.get("workspace", params.get("path"))
    if raw is None:
        if required:
            raise ProtocolError(
                "missing_param",
                "This method requires workspace or path.",
                request_id=request_id,
            )
        raw = "."
    path = Path(str(raw))
    try:
        binding = resolve_startup_workspace_binding(
            requested_path=path,
            interactive=False,
            create_if_missing=False,
            allow_broad_workspace=_optional_bool(params, "allow_broad_workspace", default=False),
            source="ide_management_param",
            action=WorkspaceAction.CHAT,
        )
    except WorkspaceBindingError as exc:
        raise ProtocolError(
            "workspace_binding_error",
            _redact_management_error_message(str(exc)),
            request_id=request_id,
        ) from exc
    return binding.workspace_context.workspace_root


def _require_workspace_trust(
    params: dict[str, Any],
    *,
    method: str,
    request_id: RequestId,
) -> None:
    if params.get("workspace_trusted") is True:
        return
    raise ProtocolError(
        "workspace_trust_required",
        f"{method} mutates local Alysis Code state and requires Workspace Trust.",
        request_id=request_id,
    )


def _require_workspace_binding_param(
    params: dict[str, Any],
    *,
    method: str,
    request_id: RequestId,
) -> None:
    for key in ("workspace", "path"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return
    raise ProtocolError(
        "missing_param",
        f"{method} requires an explicit workspace or path.",
        request_id=request_id,
    )


def _dataclass_payload(value: Any) -> Any:
    if value is None:
        return None
    if is_dataclass(value):
        return _json_safe(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_safe(model_dump(mode="json"))
    return _json_safe(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(child) for child in value]
    return value


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if _unsafe_payload_response_key(key_text) and isinstance(child, str):
                out[key_text] = "<redacted>" if child else child
            else:
                out[key_text] = _redact_payload(child)
        return out
    if isinstance(value, list):
        return [_redact_payload(child) for child in value]
    if isinstance(value, tuple):
        return [_redact_payload(child) for child in value]
    if isinstance(value, str):
        return _redact_secret_string(value)
    return value


def _redact_management_error_message(message: str) -> str:
    return _redact_secret_string(str(message))


def _redact_secret_string(value: str) -> str:
    redacted = str(redact_secrets(value))
    redacted = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1<redacted>", redacted)
    redacted = _URL_USERINFO_PATTERN.sub(r"\1<redacted>@\3", redacted)
    return redacted


def _unsafe_payload_response_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in {
        "api_key_env",
        "key_env_var",
        "secret_action",
        "secret_policy",
        "secret_redaction",
        "secret_values_included",
        "secret_values_in_params",
        "values_redacted",
    }:
        return False
    return _unsafe_config_response_key(key)


def _unsafe_config_response_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in {"api_key_env", "key_env_var"}:
        return False
    return normalized in {"prompt_cache_key"} or any(
        marker in normalized
        for marker in ("api_key", "token", "secret", "password", "credential", "authorization")
    )


def _required_str(
    params: dict[str, Any],
    key: str,
    *,
    request_id: RequestId,
) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(
            "missing_param", f"Missing required string param: {key}.", request_id=request_id
        )
    return value.strip()


def _optional_str(params: dict[str, Any], key: str, *, default: str) -> str:
    value = params.get(key)
    if value is None:
        return default
    return str(value)


def _optional_bool(params: dict[str, Any], key: str, *, default: bool) -> bool:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def dispatch_management_handler(
    method: str,
    params: dict[str, Any],
    request_id: RequestId,
    handler: Callable[[dict[str, Any], RequestId], dict[str, Any]] | None,
) -> dict[str, Any]:
    if handler is None:
        raise ProtocolError(
            "method_not_found",
            f"Unsupported method: {method}",
            request_id=request_id,
        )
    if method in MUTATING_MANAGEMENT_METHODS:
        _require_workspace_trust(params, method=method, request_id=request_id)
    if method in WORKSPACE_REQUIRED_MANAGEMENT_METHODS:
        _require_workspace_binding_param(params, method=method, request_id=request_id)
    try:
        return _redact_payload(handler(params, request_id))
    except ProtocolError as exc:
        raise ProtocolError(
            exc.code,
            _redact_management_error_message(exc.message),
            request_id=exc.request_id if exc.request_id is not None else request_id,
        ) from exc
    except ConfigError as exc:
        raise ProtocolError(
            "config_error", _redact_management_error_message(str(exc)), request_id=request_id
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtocolError(
            "management_error", _redact_management_error_message(str(exc)), request_id=request_id
        ) from exc
