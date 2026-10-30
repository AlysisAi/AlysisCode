from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import __version__
from ..atomic_io import atomic_write_text
from ..cli_impl.commands.auth import native_provider_status_payload
from ..cli_impl.commands.hooks import (
    _HOOK_SESSION_SOURCES,
    _HOOKS_INIT_TEMPLATE,
    _collect_hook_source_statuses,
    _find_hook_in_layer,
    _hook_test_match_result,
    _hooks_config_path_for_layer,
)
from ..cli_impl.commands.mcp import (
    _build_mcp_auth_status_rows,
    _manual_mcp_manager_for_path,
    _manual_mcp_resolved_config_for_path,
    _mcp_status_payload,
    _parse_manual_mcp_prompt_runtime,
    _require_http_oauth_server_for_auth,
    _resolve_mcp_server_by_id,
)
from ..computer_use_attribution import phase
from ..config import (
    AppConfig,
    ConfigError,
    clear_persisted_profile_key,
    config_path,
    credentials_path,
    load_config,
    rename_persisted_profile_key,
    resolve_api_key,
    resolve_profile_api_key,
    save_config,
    set_config_value,
)
from ..custom_tools import (
    CustomToolCatalogEntry,
    global_custom_tools_root,
    project_custom_tools_root,
    trust_project_tool,
    untrust_project_tool,
)
from ..extensions.activation import resolve_active_plugins
from ..extensions.bundled import bundled_manifest, parse_bundled_manifest, same_bundled_access
from ..extensions.component_activation import (
    build_plugin_activation_index,
    filter_discovered_skills_for_plugins,
)
from ..extensions.install import (
    PluginInstallError,
    disable_plugin,
    enable_plugin,
    install_plugin,
    uninstall_plugin,
)
from ..extensions.manifest import PluginManifestError, load_manifest
from ..extensions.models import normalize_extension_id
from ..extensions.paths import installed_plugin_root, project_extensions_path
from ..extensions.registry import find_by_id, load_registry
from ..extensions.registry import search as search_extensions
from ..extensions.state import (
    load_global_state,
    load_project_overrides,
    load_project_state,
)
from ..extensions.workspace_trust import is_workspace_trusted as extension_workspace_trusted
from ..feedback_report import (
    FeedbackReportError,
    create_feedback_bundle,
    create_feedback_github_issue_draft,
    feedback_github_issue_status_lines,
    resolve_feedback_workspace_root,
)
from ..hooks import (
    canonicalize_hook_event_name,
    hook_audit_artifact_path,
    load_hook_config_file,
    load_resolved_hooks_config,
    project_hooks_config_path,
    project_local_hooks_config_path,
    read_hook_audit_events,
    trust_project_hooks_config,
    untrust_project_hooks_config,
)
from ..mcp.oauth_store import delete_oauth_token_record
from ..profile_presets import (
    PROFILE_PRESETS,
    convert_profile_to_preset,
    get_preset,
    make_profile_from_preset,
    normalize_conversion_target,
    preset_protocol_kind,
    preset_selection_label,
    target_preset_for_profile_conversion,
)
from ..profiles import (
    SUBSCRIPTION_SELECTION_REQUIRED_KEY,
    ProfileSpec,
    active_subscription_selection_ready,
    add_profile,
    get_active_profile,
    get_profile,
    list_profiles,
    remove_profile,
    rename_profile,
    set_active_profile,
    subscription_selection_supported,
    update_active_profile_defaults,
    update_profile,
)
from ..provider_auth import create_provider_auth
from ..provider_diagnostics import build_provider_diagnostics, validate_active_provider_live
from ..provider_telemetry import (
    diagnostic_bundle_payload,
    last_provider_call_summary,
    last_web_search_summary,
)
from ..runtime_kind import RuntimeKind
from ..sandbox_doctor import (
    configured_sandbox_images,
    diagnose_sandbox,
    format_sandbox_problem_message,
    pull_sandbox_images,
    sandbox_env_summary,
)
from ..session_metrics import score_session_log
from ..session_store import (
    list_sessions,
    read_session_events,
    resolve_sessions_dir,
    sanitize_session_id,
)
from ..skills import (
    SkillLifecycleError,
    discover_skills,
    install_skill_bundle,
    load_global_skill_state,
    load_project_skill_state,
    load_repo_conventions,
    remove_managed_skill,
    render_repo_conventions_context,
    render_skill_info_text,
    resolve_skill_catalog,
    resolve_skill_catalog_entry,
    save_global_skill_state,
    save_project_skill_state,
    scaffold_skill_bundle,
    set_global_skill_disabled,
    set_project_skill_override,
    validate_skill_bundle,
)
from ..skills.workbench import SkillWorkbench, provenance_revision, read_bundle
from ..tools.availability import get_tool_availability
from ..tools.registry import iter_builtin_tool_metadata
from ..tools.web_search_runtime import resolve_web_search_runtime_status
from ..updates import check_for_updates, status_from_cache
from ..usage_tracker import aggregate_usage_from_session_logs
from ..workspace_binding import WorkspaceAction, WorkspaceBindingError
from ..workspace_binding_ui import resolve_startup_workspace_binding
from ..workspace_context import WorkspaceContextError
from .mcp_oauth_coordinator import McpOAuthLoginRequest
from .protocol import ProtocolError, RequestId, redact_secrets

_SESSION_SHOW_DEFAULT_MAX_EVENTS = 200
_SESSION_SHOW_MAX_EVENTS = 1_000
_SESSION_SHOW_DEFAULT_MAX_TOTAL_BYTES = 256 * 1024
_SESSION_SHOW_MAX_TOTAL_BYTES = 1024 * 1024
_CONVENTIONS_RENDER_DEFAULT_MAX_CHARS = 4_000
_CONVENTIONS_RENDER_MAX_CHARS = 100_000
_CONVENTIONS_RENDER_DEFAULT_MAX_BYTES = 64 * 1024
_CONVENTIONS_RENDER_MAX_BYTES = 256 * 1024
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b([A-Z0-9_.-]*(?:api[_-]?key|token|secret|password|credential)[A-Z0-9_.-]*\s*[:=]\s*)([^\s,;&]+)"
)
_URL_USERINFO_PATTERN = re.compile(
    r"(?i)\b((?:git\+)?https?://)([^/@\s]+)@([A-Za-z0-9.-]+(?::\d+)?)"
)
_EXTENSION_REGISTRY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_-]*$")

MANAGEMENT_METHODS = (
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
    "provider.auth.status",
    "config.get",
    "config.set",
    "config.schema",
    "config.validate",
    "profile.list",
    "profile.show",
    "profile.add",
    "profile.remove",
    "profile.use",
    "profile.rename",
    "profile.presets",
    "profile.preset",
    "profile.convert",
    "session.show",
    "session.usage",
    "session.score",
    "tools.catalog",
    "tool.list",
    "tool.info",
    "tool.trust",
    "tool.untrust",
    "commands.list",
    "agents.list",
    "skill.list",
    "skill.info",
    "skill.init",
    "skill.validate",
    "skill.install",
    "skill.enable",
    "skill.disable",
    "skill.remove",
    "doctor.summary",
    "doctor.providers",
    "doctor.providers.live",
    "doctor.bundle",
    "sandbox.doctor",
    "sandbox.setup",
    "sandbox.pull",
    "update.check",
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
    "hooks.trace",
    "hooks.test",
    "hooks.trust",
    "hooks.untrust",
    "hooks.init",
    "hooks.effective",
    "hooks.enable",
    "hooks.disable",
    "conventions.list",
    "conventions.render",
    "ext.search",
    "ext.list",
    "ext.info",
    "ext.install",
    "ext.custom.save",
    "ext.uninstall",
    "ext.enable",
    "ext.disable",
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

MANAGEMENT_METHOD_CAPABILITIES: dict[str, dict[str, Any]] = {
    method: {
        "supported": True,
        "mutates": method in MUTATING_MANAGEMENT_METHODS,
        "trust_required": method in MUTATING_MANAGEMENT_METHODS,
        "workspace_required": method in WORKSPACE_REQUIRED_MANAGEMENT_METHODS,
        "terminal_output_scraping": False,
        "secret_values_in_params": False,
    }
    for method in MANAGEMENT_METHODS
}
MANAGEMENT_METHOD_CAPABILITIES["mcp.auth.login.start"].update(
    {
        "supported": True,
        "callable": True,
        "behavior": "returns_authorization_url_and_completes_via_loopback_callback",
        "mutates": True,
        "trust_required": True,
        "passive_safe": False,
        "browser_opened_by_bridge": False,
        "tokens_in_protocol_params": False,
        "secret_store_required": True,
        "lifecycle_methods": [
            "mcp.auth.login.start",
            "mcp.auth.login.status",
            "mcp.auth.login.cancel",
        ],
        "flow_state_persistent": True,
        "loopback_callback_owned": True,
        "callback_host": "127.0.0.1",
        "authorization_code_in_protocol": False,
    }
)
MANAGEMENT_METHOD_CAPABILITIES["ext.install"].update(
    {
        "trust_review_required": True,
        "approval_payload": "extension_install_trust_approval_v1",
        "yes_alone_auto_trusts_package": False,
        "source_policy": "registry_id_only",
        "url_userinfo_rejected": True,
    }
)
MANAGEMENT_METHOD_CAPABILITIES["session.show"].update(
    {
        "redacted": True,
        "bounded": True,
        "default_max_events": _SESSION_SHOW_DEFAULT_MAX_EVENTS,
        "max_events": _SESSION_SHOW_MAX_EVENTS,
        "default_max_total_bytes": _SESSION_SHOW_DEFAULT_MAX_TOTAL_BYTES,
        "max_total_bytes": _SESSION_SHOW_MAX_TOTAL_BYTES,
    }
)
MANAGEMENT_METHOD_CAPABILITIES["conventions.render"].update(
    {
        "redacted": True,
        "bounded": True,
        "default_max_chars": _CONVENTIONS_RENDER_DEFAULT_MAX_CHARS,
        "max_chars": _CONVENTIONS_RENDER_MAX_CHARS,
        "default_max_bytes": _CONVENTIONS_RENDER_DEFAULT_MAX_BYTES,
        "max_bytes": _CONVENTIONS_RENDER_MAX_BYTES,
    }
)
MANAGEMENT_METHOD_CAPABILITIES["update.check"].update(
    {
        "network_default": False,
        "network_requires_explicit_user_intent": True,
        "network_params": ["allow_network", "force"],
        "passive_safe": True,
    }
)
MANAGEMENT_METHOD_CAPABILITIES["doctor.providers.live"].update(
    {
        "network_default": False,
        "network_requires_explicit_user_intent": True,
        "network_params": ["allow_live"],
        "live_provider_request": True,
        "passive_safe": False,
    }
)
MANAGEMENT_METHOD_CAPABILITIES["skill.install"].update(
    {
        "local_path_policy": "workspace_scoped_regular_dir_or_zip",
        "remote_source_policy": "https_remote_requires_allow_remote_and_confirmation",
        "url_userinfo_rejected": True,
        "rejects_symlinks": True,
    }
)


def _feature_group(*prefixes: str) -> dict[str, Any]:
    methods = [
        method
        for method in MANAGEMENT_METHODS
        if any(method.startswith(f"{prefix}.") for prefix in prefixes)
    ]
    return {
        "supported": True,
        "methods": methods,
        "mutating_methods": [method for method in methods if method in MUTATING_MANAGEMENT_METHODS],
        "trust_required_methods": [
            method for method in methods if method in MUTATING_MANAGEMENT_METHODS
        ],
        "terminal_output_scraping": False,
    }


MANAGEMENT_FEATURES: dict[str, Any] = {
    "methods": MANAGEMENT_METHOD_CAPABILITIES,
    "secret_policy": {
        "inline_secrets_rejected": True,
        "profile_key_values": "status_only",
        "config_secret_values": "redacted_or_status_only",
        "extension_secret_storage": "VS Code SecretStorage remains extension-side",
        "management_responses_redacted": True,
        "management_errors_redacted": True,
        "urls_with_userinfo_redacted": True,
        "authorization_headers_redacted": True,
    },
    "config": _feature_group("config"),
    "profile": _feature_group("profile"),
    "sessions": _feature_group("session"),
    "tools": _feature_group("tools", "tool"),
    "skills": _feature_group("skill"),
    "agents": _feature_group("agents"),
    "doctor": _feature_group("doctor"),
    "sandbox": _feature_group("sandbox"),
    "update": _feature_group("update"),
    "report": _feature_group("report"),
    "mcp": {
        **_feature_group("mcp"),
        "auth_login": {
            "supported": True,
            "callable_probe_method": "mcp.auth.login.start",
            "advertised_lifecycle_methods": True,
            "methods": [
                "mcp.auth.login.start",
                "mcp.auth.login.status",
                "mcp.auth.login.cancel",
            ],
            "flow_state_persistent": True,
            "loopback_callback_owned": True,
            "pkce_s256": True,
            "state_validation": True,
            "host_header_validation": True,
            "timeout_expiry": True,
            "cancel_cleanup": True,
            "logout_fences_late_token_writes": True,
            "encrypted_token_store": True,
            "browser_opened_by_bridge": False,
            "tokens_in_protocol_params": False,
            "secret_store_required": True,
            "authorization_code_in_protocol": False,
            "remote_extension_host_support": False,
        },
    },
    "hooks": {
        **_feature_group("hooks"),
        "watch": {
            "supported": False,
            "advertised_method": False,
            "advertised_lifecycle_methods": False,
            "proposed_methods": [
                "hooks.watch.start",
                "hooks.watch.poll",
                "hooks.watch.stop",
                "hooks.watch.status",
            ],
            "missing_lifecycle_primitives": [
                "subscription_registry",
                "bounded_event_buffer",
                "dropped_event_accounting",
                "watcher_cancellation",
                "stop_cleanup",
                "backend_redaction_for_live_output",
            ],
            "explicit_user_action_required": True,
            "workspace_trust_required": True,
            "passive_safe": True,
            "reason": "hooks.watch requires a managed subscription lifecycle, cancellation, bounded redacted events, dropped-event accounting, and disposable cleanup; it is not a request/response IDE method.",
        },
    },
    "conventions": _feature_group("conventions"),
    "ext": _feature_group("ext"),
}


def handle_management_method(
    method: str,
    params: dict[str, Any],
    *,
    request_id: RequestId,
    stateful_handlers: Mapping[str, Callable[[dict[str, Any], RequestId], dict[str, Any]]]
    | None = None,
) -> dict[str, Any]:
    handler = (stateful_handlers or {}).get(method) or _HANDLERS.get(method)
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
    except (
        WorkspaceContextError,
        FeedbackReportError,
        SkillLifecycleError,
        PluginInstallError,
    ) as exc:
        raise ProtocolError(
            "management_error", _redact_management_error_message(str(exc)), request_id=request_id
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtocolError(
            "management_error", _redact_management_error_message(str(exc)), request_id=request_id
        ) from exc


def retained_session_usage(
    params: dict[str, Any],
    *,
    request_id: RequestId,
) -> dict[str, Any]:
    return _session_usage(params, request_id)


def _provider_auth_status(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    provider_id = _required_str(params, "provider_id", request_id=request_id)
    status = native_provider_status_payload(provider_id)
    # Only the validated route fingerprint crosses the bridge, never credential material.
    # Its wire name avoids weakening the general credential-field redactor.
    return {
        **{
            key: status.get(key)
            for key in (
                "connection",
                "authenticated",
                "verified",
                "detail",
                "transport",
                "display_name",
                "error",
            )
        },
        "route_scope": status.get("credential_scope"),
        "secret_values_included": False,
    }


def _config_get(_params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    with phase("config"):
        cfg = load_config()
        return {
            "config": _safe_config_payload(cfg),
            "active_profile": str((cfg.extra_fields or {}).get("active_profile") or ""),
            "api_key": _api_key_status(resolve_api_key(cfg)),
            "config_path": str(config_path()),
            "credentials_path": str(credentials_path()),
            "secret_values_included": False,
        }


def _config_schema(_params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    return {
        "schema": AppConfig.model_json_schema(),
        "secret_values_included": False,
    }


def _config_validate(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    raw_values = params.get("values")
    if raw_values is None:
        cfg = load_config()
    else:
        if not isinstance(raw_values, dict):
            raise ProtocolError(
                "invalid_field",
                "config.validate values must be an object when provided.",
                request_id=request_id,
            )
        cfg = AppConfig.model_validate(raw_values)
    return {
        "valid": True,
        "errors": [],
        "config": _safe_config_payload(cfg),
        "secret_values_included": False,
    }


def _config_set(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    key = _required_str(params, "key", request_id=request_id)
    if _unsafe_config_set_key(key):
        raise ProtocolError(
            "inline_secret_rejected",
            "Secret-bearing config keys cannot be set through IDE protocol params.",
            request_id=request_id,
        )
    if "value" not in params:
        raise ProtocolError("missing_param", "config.set requires value.", request_id=request_id)
    raw_value = params.get("value")
    _reject_url_userinfo_value(raw_value, field=f"config.{key}", request_id=request_id)
    _reject_secret_bearing_value(raw_value, field=f"config.{key}", request_id=request_id)
    value = _config_value_to_cli_string(raw_value)
    cfg = load_config()
    subscription_effort: str | None = None
    if key.strip().lower() == "model" and get_active_profile(cfg).auth_provider:
        subscription_effort = _select_subscription_model_default(
            cfg,
            value,
            request_id=request_id,
        )
    else:
        cfg = set_config_value(cfg, key, value)
    save_config(cfg)
    return {
        "key": key,
        "changed": True,
        "config_path": str(config_path()),
        **(
            {
                "reasoning_effort": subscription_effort,
                "subscription_selection_ready": active_subscription_selection_ready(cfg),
            }
            if subscription_effort is not None
            else {}
        ),
        "secret_values_included": False,
    }


def _select_subscription_model_default(
    cfg: AppConfig,
    model: str,
    *,
    request_id: RequestId,
) -> str:
    """Atomically select an account model and its provider-advertised default effort.

    The VS Code model picker is intentionally a one-step control. Subscription profiles cannot use
    the generic single-field config mutation because that could leave an unsupported model/effort
    pair, so the bridge validates the live account catalog and commits both fields together.
    """

    profile = get_active_profile(cfg)
    provider_id = str(profile.auth_provider or "").strip()
    if not provider_id:
        raise ProtocolError(
            "config_error",
            "The active profile is not an AI subscription connection.",
            request_id=request_id,
        )
    adapter = create_provider_auth(provider_id)
    status = adapter.account_status()
    if not status.connected:
        raise ProtocolError(
            "provider_login_required",
            "Connect the active AI subscription account before choosing its model.",
            request_id=request_id,
        )
    models = tuple(adapter.list_models(refresh=True))
    selected = next((item for item in models if str(getattr(item, "id", "")) == model), None)
    if selected is None:
        raise ProtocolError(
            "unsupported_subscription_model",
            "The selected model is not available to the connected subscription account.",
            request_id=request_id,
        )
    efforts = tuple(
        str(getattr(item, "id", "") or "").strip()
        for item in tuple(getattr(selected, "reasoning_efforts", ()) or ())
    )
    efforts = tuple(item for item in efforts if item)
    advertised_default = str(getattr(selected, "default_reasoning_effort", "") or "").strip()
    reasoning_effort = (
        advertised_default
        if advertised_default and (not efforts or advertised_default in efforts)
        else ("medium" if "medium" in efforts else (efforts[0] if efforts else "auto"))
    )
    update_active_profile_defaults(
        cfg,
        default_model=model,
        reasoning_effort=reasoning_effort,
        allow_subscription_selection=True,
    )
    updated = get_active_profile(cfg)
    if not subscription_selection_supported(updated, models):
        raise ProtocolError(
            "unsupported_subscription_selection",
            "The selected model and reasoning effort are not supported by the connected account.",
            request_id=request_id,
        )
    cfg.extra_fields.pop(SUBSCRIPTION_SELECTION_REQUIRED_KEY, None)
    cfg.extra_fields.pop("subscription_reconnect_required", None)
    cfg.extra_fields["onboarded"] = True
    return reasoning_effort


def _profile_list(_params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    active = str((cfg.extra_fields or {}).get("active_profile") or "")
    return {
        "active_profile": active,
        "profiles": [_profile_payload(cfg, profile) for profile in list_profiles(cfg)],
        "secret_values_included": False,
    }


def _profile_show(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    name = _required_str(params, "name", request_id=request_id)
    profile = get_profile(cfg, name)
    if profile is None:
        raise ProtocolError(
            "profile_not_found", f"Profile not found: {name}", request_id=request_id
        )
    return {
        "profile": _profile_payload(cfg, profile, include_details=True),
        "secret_values_included": False,
    }


def _profile_add(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    extra_headers_action = _extra_headers_secret_storage_action(
        params,
        request_id=request_id,
    )
    if extra_headers_action is not None:
        return {
            "changed": False,
            "profile": None,
            "action": extra_headers_action,
            "secret_values_included": False,
        }
    base_url = _required_str(params, "base_url", request_id=request_id)
    _reject_url_userinfo_value(base_url, field="profile.base_url", request_id=request_id)
    profile = ProfileSpec(
        name=_required_str(params, "name", request_id=request_id),
        protocol=_optional_str(params, "protocol", default="openai_compat"),
        base_url=base_url,
        api_key_env=_optional_str_or_none(params, "api_key_env"),
        extra_headers=_extra_headers_param(params, request_id=request_id),
        default_model=_optional_str(params, "default_model", default=""),
        web_search_adapter=_optional_str(params, "web_search_adapter", default="auto"),
        web_search_model=_optional_str(params, "web_search_model", default=""),
        notes=_optional_str(params, "notes", default=""),
    )
    add_profile(cfg, profile)
    save_config(cfg)
    return {
        "profile": _profile_payload(cfg, profile, include_details=True),
        "changed": True,
        "secret_action": _secret_action_for_profile(profile),
        "secret_values_included": False,
    }


def _profile_remove(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    if not _confirmed(params):
        return _confirmation_required("profile.remove", "profile removal")
    cfg = load_config()
    name = _required_str(params, "name", request_id=request_id)
    remove_profile(cfg, name)
    clear_persisted_profile_key(name)
    save_config(cfg)
    return {"name": name, "removed": True, "secret_values_included": False}


def _profile_use(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    name = _required_str(params, "name", request_id=request_id)
    set_active_profile(cfg, name)
    save_config(cfg)
    profile = get_profile(cfg, name)
    return {
        "active_profile": name,
        "profile": _profile_payload(cfg, profile, include_details=True) if profile else None,
        "changed": True,
        "secret_values_included": False,
    }


def _profile_rename(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    old = _required_str(params, "old", request_id=request_id)
    new = _required_str(params, "new", request_id=request_id)
    rename_profile(cfg, old, new)
    rename_persisted_profile_key(old, new)
    save_config(cfg)
    profile = get_profile(cfg, new)
    return {
        "old": old,
        "new": new,
        "profile": _profile_payload(cfg, profile, include_details=True) if profile else None,
        "changed": True,
        "secret_values_included": False,
    }


def _profile_presets(_params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    return {
        "presets": [_preset_payload(preset) for preset in PROFILE_PRESETS],
        "secret_values_included": False,
    }


def _profile_preset(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    preset_key = _required_str_any(params, ("preset_key", "preset"), request_id=request_id)
    preset = get_preset(preset_key)
    if preset is None:
        raise ProtocolError(
            "unknown_profile_preset",
            f"Unknown profile preset: {preset_key}",
            request_id=request_id,
        )
    cfg = load_config()
    name = _optional_str(params, "name", default=str(preset.key)).strip().lower()
    if get_profile(cfg, name) is not None and not _confirmed(params):
        return _confirmation_required("profile.preset", f"overwrite profile {name}")
    if not preset.base_url and not _optional_str(params, "base_url", default="").strip():
        raise ProtocolError(
            "missing_param",
            "profile.preset requires base_url for the custom preset.",
            request_id=request_id,
        )
    profile = make_profile_from_preset(preset, name=name)
    add_profile(cfg, profile)
    base_url = _optional_str(params, "base_url", default="").strip()
    if base_url and base_url != profile.base_url:
        _reject_url_userinfo_value(base_url, field="profile.base_url", request_id=request_id)
        update_profile(cfg, profile.name, base_url=base_url)
        profile = get_profile(cfg, profile.name) or profile
    save_config(cfg)
    return {
        "profile": _profile_payload(cfg, profile, include_details=True),
        "preset": _preset_payload(preset),
        "changed": True,
        "secret_action": _secret_action_for_profile(profile),
        "secret_values_included": False,
    }


def _profile_convert(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    profile_name = _optional_str(
        params,
        "name",
        default=str((cfg.extra_fields or {}).get("active_profile") or ""),
    ).strip()
    if not profile_name:
        raise ProtocolError(
            "missing_param",
            "profile.convert requires name when no active profile is configured.",
            request_id=request_id,
        )
    profile = get_profile(cfg, profile_name)
    if profile is None:
        raise ProtocolError(
            "profile_not_found", f"Profile not found: {profile_name}", request_id=request_id
        )
    target = normalize_conversion_target(_required_str(params, "target", request_id=request_id))
    preset = target_preset_for_profile_conversion(profile, target=target)
    if preset is None:
        raise ProtocolError(
            "unsupported_profile_conversion",
            "Only OpenAI, Anthropic, and Gemini profiles can be converted between native and compatibility protocols.",
            request_id=request_id,
        )
    converted = convert_profile_to_preset(profile, preset)
    preview = {
        "before": _profile_payload(cfg, profile, include_details=True),
        "after": _profile_payload(cfg, converted, include_details=True),
        "target": target,
        "preset": _preset_payload(preset),
    }
    if converted.to_dict() == profile.to_dict():
        return {**preview, "changed": False, "secret_values_included": False}
    if not _confirmed(params):
        return {
            **preview,
            "changed": False,
            "action": {
                "kind": "requires_confirmation",
                "method": "profile.convert",
                "reason": f"Convert profile {profile.name} to {target}.",
            },
            "secret_values_included": False,
        }
    add_profile(cfg, converted)
    if str((cfg.extra_fields or {}).get("active_profile") or "") == profile.name:
        set_active_profile(cfg, profile.name)
    save_config(cfg)
    return {**preview, "changed": True, "secret_values_included": False}


def _session_show(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    path = _session_log_path(params, request_id=request_id)
    max_events = _positive_int(
        params,
        "max_events",
        default=_SESSION_SHOW_DEFAULT_MAX_EVENTS,
        upper=_SESSION_SHOW_MAX_EVENTS,
    )
    max_total_bytes = _positive_int(
        params,
        "max_total_bytes",
        default=_SESSION_SHOW_DEFAULT_MAX_TOTAL_BYTES,
        upper=_SESSION_SHOW_MAX_TOTAL_BYTES,
    )
    events: list[dict[str, Any]] = []
    event_count = 0
    response_bytes = 0
    truncated_by_events = False
    truncated_by_bytes = False
    for event in read_session_events(path):
        event_count += 1
        if len(events) >= max_events:
            truncated_by_events = True
            continue
        redacted_event = _redact_payload(event)
        event_bytes = _json_size_bytes(redacted_event)
        if response_bytes + event_bytes > max_total_bytes:
            truncated_by_bytes = True
            continue
        events.append(redacted_event)
        response_bytes += event_bytes
    return {
        "session_id": path.stem,
        "path": str(path),
        "events": events,
        "event_count": event_count,
        "returned_event_count": len(events),
        "truncated": truncated_by_events or truncated_by_bytes,
        "truncated_by_events": truncated_by_events,
        "truncated_by_bytes": truncated_by_bytes,
        "max_events": max_events,
        "max_total_bytes": max_total_bytes,
        "response_bytes": response_bytes,
        "secret_values_included": False,
        "redacted": True,
    }


def _session_usage(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    path = _session_log_path(params, request_id=request_id)
    summary = aggregate_usage_from_session_logs([path])
    totals = summary.totals()
    records = summary.records()
    return {
        "session_id": path.stem,
        "path": str(path),
        "by_model": summary.by_model_rows(),
        "totals": totals,
        "call_count": int(totals.get("calls", len(records)) if isinstance(totals, dict) else 0),
    }


def _session_score(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    sessions_dir = resolve_sessions_dir(cfg)
    session_id = str(params.get("session_id") or "").strip()
    latest = _positive_int(params, "latest", default=0, upper=200)
    if session_id and latest > 0:
        raise ProtocolError(
            "invalid_field",
            "Use either session_id or latest, not both.",
            request_id=request_id,
        )
    if session_id:
        paths = [_session_log_path(params, request_id=request_id)]
    else:
        infos = list_sessions(sessions_dir)
        count = latest if latest > 0 else 1
        paths = [info.path for info in infos[:count]]
    scores = [score_session_log(path) for path in paths]
    return {
        "sessions_dir": str(sessions_dir),
        "scores": scores,
        "score": scores[0] if len(scores) == 1 else None,
    }


def _tools_catalog(_params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    tools = []
    for spec in iter_builtin_tool_metadata():
        status = "available"
        notes: list[str] = []
        availability = get_tool_availability(spec.name)
        if spec.optional and availability is not None and availability.unavailable_reason:
            status = "optional-unavailable"
            notes.append(str(availability.unavailable_reason))
        if spec.name == "web_search":
            api_key = resolve_api_key(cfg)
            runtime = resolve_web_search_runtime_status(cfg=cfg, api_key=api_key.key)
            status = runtime.availability_label
            notes.extend(runtime.notes)
            if not runtime.registration_ready:
                notes.append(runtime.setup_hint)
        tools.append(
            {
                "name": spec.name,
                "description": spec.description,
                "categories": list(spec.categories),
                "parameters": spec.copied_parameters(),
                "display_name": spec.rich.display_name,
                "optional": spec.optional,
                "status": status,
                "notes": [note for note in notes if note],
                "built_in_subagent_exposure": spec.built_in_subagent_exposure,
            }
        )
    return {"tools": tools, "count": len(tools), "secret_values_included": False}


def _tool_list(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root, entries = _custom_tool_entries(params, request_id=request_id)
    return {
        "workspace_root": str(workspace_root),
        "project_root": str(project_custom_tools_root(workspace_root)),
        "global_root": str(global_custom_tools_root()),
        "tools": [_custom_tool_payload(entry, workspace_root=workspace_root) for entry in entries],
    }


def _tool_info(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    name = _required_str(params, "name", request_id=request_id)
    workspace_root, entries = _custom_tool_entries(params, request_id=request_id)
    matches = [
        entry for entry in entries if str(getattr(entry, "name", "")).casefold() == name.casefold()
    ]
    if not matches:
        raise ProtocolError(
            "tool_not_found", f"Custom tool not found: {name}", request_id=request_id
        )
    return {
        "workspace_root": str(workspace_root),
        "tools": [
            {
                **_custom_tool_payload(entry, workspace_root=workspace_root),
                "code": entry.source_path.read_text(encoding="utf-8")
                if entry.spec is not None and entry.source_path.stat().st_size <= 1024 * 1024
                else "",
            }
            for entry in matches
        ],
    }


def _tool_trust(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    name = _required_str(params, "name", request_id=request_id)
    workspace_root, entries = _custom_tool_entries(params, request_id=request_id)
    entry = _select_project_tool_entry(entries, name=name, request_id=request_id)
    assert entry.spec is not None
    if params.get("expected_hash") is not None and params["expected_hash"] != entry.spec.file_hash:
        raise SkillLifecycleError(
            "The tool changed after review. Review it again before trusting it."
        )
    trust_project_tool(entry.spec)
    return {
        "workspace_root": str(workspace_root),
        "name": entry.spec.name,
        "trusted": True,
        "changed": True,
    }


def _tool_untrust(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    name = _required_str(params, "name", request_id=request_id)
    workspace_root, entries = _custom_tool_entries(params, request_id=request_id)
    entry = _select_project_tool_entry(entries, name=name, request_id=request_id)
    assert entry.spec is not None
    untrust_project_tool(entry.spec)
    return {
        "workspace_root": str(workspace_root),
        "name": entry.spec.name,
        "trusted": False,
        "changed": True,
    }


def _commands_list(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    # Reuse the terminal catalog; hosts decide which commands have native handlers.
    from ..cli_impl.chat_slash_completer import get_chat_specs, get_forge_specs

    mode = params.get("mode", "chat")
    if not isinstance(mode, str) or mode not in {"chat", "forge"}:
        raise ProtocolError("invalid_field", "mode must be chat or forge", request_id=request_id)
    specs = get_chat_specs() + (get_forge_specs() if mode == "forge" else ())
    return {
        "commands": [
            {"name": spec.name, "usage": spec.usage, "description": spec.description}
            for spec in specs
        ]
    }


def _agents_list(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    """Every subagent with the model and provider it would run on (subagent_settings.py).

    Without workspace or path, the project's .alysis_agents/ is not included.
    """
    from ..subagent_settings import subagent_catalog

    root = (
        _workspace_root_from_params(params, request_id=request_id, required=True)
        if params.get("workspace") is not None or params.get("path") is not None
        else None
    )
    return {**subagent_catalog(load_config(), root=root), "secret_values_included": False}


_skill_workbench = SkillWorkbench()


def _skill_import_describe(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    root = _workspace_root_from_params(params, request_id=request_id, required=True)
    token = _required_str(params, "draft_id", request_id=request_id)
    return _skill_workbench._payload(token, _skill_workbench._draft(root, token))


def _skill_import_package(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    if not _confirmed(params):
        return _confirmation_required("skill.import.package", "install the reviewed native package")
    return _skill_workbench.install_package(
        _workspace_root_from_params(params, request_id=request_id, required=True),
        _required_str(params, "draft_id", request_id=request_id),
    )


def _skill_import_preview(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    if not _confirmed(params):
        return _confirmation_required("skill.import.preview", "read the explicitly selected source")
    return _skill_workbench.preview(
        workspace=_workspace_root_from_params(params, request_id=request_id, required=True),
        source=_required_str(params, "source", request_id=request_id),
        subdir=_optional_str(params, "subdir", default=""),
        ref=_optional_str(params, "ref", default=""),
    )


def _skill_import_install(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    if not _confirmed(params):
        return _confirmation_required(
            "skill.import.install", "install the reviewed skill snapshots"
        )
    selected = params.get("selected")
    if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
        raise SkillLifecycleError("Select skills from the import preview.")
    return _skill_workbench.install(
        _workspace_root_from_params(params, request_id=request_id, required=True),
        _required_str(params, "draft_id", request_id=request_id),
        selected,
        project=_optional_bool(params, "project", default=False),
        accept_partial=_optional_bool(params, "accept_partial", default=False),
    )


def _skill_import_discard(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    return _skill_workbench.discard(
        _workspace_root_from_params(params, request_id=request_id, required=True),
        _required_str(params, "draft_id", request_id=request_id),
    )


def _skill_document_entry(params: dict[str, Any], request_id: RequestId) -> tuple[Any, Any]:
    root, catalog = _skill_catalog(params, request_id=request_id)
    entry = resolve_skill_catalog_entry(
        entries=catalog.entries, raw_name=_required_str(params, "name", request_id=request_id)
    )
    if entry is None:
        raise SkillLifecycleError("This skill is no longer available.")
    return root, entry


def _skill_document_read(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    from ..extensions.component_activation import skill_plugin_id
    from ..skills.workbench import DESCRIPTIVE_FIELDS

    root, entry = _skill_document_entry(params, request_id)
    owner_plugin = skill_plugin_id(entry.skill, build_plugin_activation_index(root))
    files = read_bundle(entry.skill.bundle_path)
    content = files.files["SKILL.md"][0].decode("utf-8")
    editable = False
    if entry.managed:
        _, managed_path = _skill_workbench.managed(
            root, entry.skill.name, project=entry.skill.source_scope == "project"
        )
        editable = managed_path.resolve() == entry.skill.bundle_path.resolve()
    redacted = _redact_payload(content) != content
    return {
        "name": entry.skill.name,
        "content": content,
        "revision": files.digest,
        "editable": editable and not redacted and not owner_plugin,
        "owner_plugin": owner_plugin,
        "enabled": entry.enabled,
        "unsupported_metadata": sorted(
            set(validate_skill_bundle(entry.skill.bundle_path).metadata) - DESCRIPTIVE_FIELDS
        ),
        "redacted": redacted,
        "scope": entry.skill.source_scope,
        "files": sorted(files.files),
        "source": _dataclass_payload(entry.install_record),
        "provenance_revision": provenance_revision(entry.install_record)
        if entry.install_record
        else None,
        "can_update": bool(entry.install_record and entry.install_record.source_digest),
    }


def _skill_document_save(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    if not _confirmed(params):
        return _confirmation_required("skill.document.save", "save skill instructions")
    root = _workspace_root_from_params(params, request_id=request_id, required=True)
    template = None
    if params.get("copy_from"):
        _, entry = _skill_document_entry({**params, "name": params["copy_from"]}, request_id)
        template = read_bundle(entry.skill.bundle_path)
        if template.digest != _required_str(params, "revision", request_id=request_id):
            raise SkillLifecycleError("The source skill changed. Open it again before copying.")
    return _skill_workbench.save(
        root,
        _required_str(params, "content", request_id=request_id),
        project=_optional_bool(params, "project", default=False),
        name=_optional_str(params, "name", default=""),
        revision=_optional_str(params, "revision", default=""),
        template=template,
    )


def _skill_update_preview(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    if not _confirmed(params):
        return _confirmation_required(
            "skill.update.preview", "check the skill's recorded source for updates"
        )
    return _skill_workbench.update_preview(
        _workspace_root_from_params(params, request_id=request_id, required=True),
        _required_str(params, "name", request_id=request_id),
        project=_optional_bool(params, "project", default=False),
        expected_source=_optional_str_or_none(params, "expected_source"),
    )


def _hooks_document_path(params: dict[str, Any], request_id: RequestId) -> Path:
    root = _workspace_root_from_params(params, request_id=request_id, required=True)
    layer = _optional_str(params, "layer", default="project")
    if layer not in {"user", "project", "local"}:
        raise SkillLifecycleError("Choose personal, project or local project hooks.")
    path = _hooks_config_path_for_layer(workspace_root=root, layer=layer)
    if path.is_symlink() or path.parent.is_symlink():
        raise SkillLifecycleError("Hooks configuration must not be a symbolic link.")
    return path


def _hooks_document_read(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    path = _hooks_document_path(params, request_id)
    if path.exists() and path.stat().st_size > 1024 * 1024:
        raise SkillLifecycleError("Hooks configuration exceeds 1 MiB.")
    content = path.read_bytes().decode("utf-8") if path.exists() else ""
    return {
        "content": content or json.dumps(_HOOKS_INIT_TEMPLATE, indent=2),
        "revision": hashlib.sha256(content.encode()).hexdigest(),
        "path": str(path),
        "editable": _redact_payload(content) == content,
    }


def _hooks_document_save(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    from pydantic import ValidationError

    from ..hooks.models import HookConfigFile

    if not _confirmed(params):
        return _confirmation_required("hooks.document.save", "save hook commands and options")
    path = _hooks_document_path(params, request_id)
    content = _required_str(params, "content", request_id=request_id)
    if len(content.encode()) > 1024 * 1024:
        raise SkillLifecycleError("Hooks configuration exceeds 1 MiB.")
    current = _hooks_document_read(params, request_id)
    if not current["editable"] or current["revision"] != params.get("revision"):
        raise SkillLifecycleError(
            "Hooks changed or contain protected values. Reload before editing."
        )
    try:
        HookConfigFile.model_validate_json(content)
    except ValidationError as exc:
        raise SkillLifecycleError("Invalid hooks configuration: " + str(exc)) from exc
    atomic_write_text(path, content)
    return {"changed": True, "path": str(path)}


def _skill_list(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root, catalog, available = _available_skill_catalog(params, request_id=request_id)
    return {
        "workspace_root": str(workspace_root),
        "skills_enabled": bool(load_config().skills_enabled),
        "skills": [
            {**_skill_entry_payload(entry), "available": entry.skill.name in available}
            for entry in catalog.entries
        ],
        "issues": _skill_issues_payload(catalog),
    }


def _skill_info(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    name = _required_str(params, "name", request_id=request_id)
    workspace_root, catalog, available = _available_skill_catalog(params, request_id=request_id)
    entry = resolve_skill_catalog_entry(entries=catalog.entries, raw_name=name)
    if entry is None:
        raise ProtocolError("skill_not_found", f"Skill not found: {name}", request_id=request_id)
    return {
        "workspace_root": str(workspace_root),
        "skill": {**_skill_entry_payload(entry), "available": entry.skill.name in available},
        "info_text": render_skill_info_text(entry.skill, catalog_entry=entry),
        "issues": _skill_issues_payload(catalog),
    }


def _skill_init(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    result = scaffold_skill_bundle(
        name=_required_str(params, "name", request_id=request_id),
        description=_optional_str(params, "description", default=""),
        workspace_root=workspace_root,
        project=_optional_bool(params, "project", default=True),
        family=_optional_str(params, "family", default="native"),
        force=_optional_bool(params, "force", default=False),
    )
    return {"result": _dataclass_payload(result), "changed": True}


def _skill_validate(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root, catalog = _skill_catalog(params, request_id=request_id)
    bundle = _optional_str(params, "bundle", default="").strip()
    name = _optional_str(params, "name", default="").strip()
    validate_all = _optional_bool(params, "all", default=False)
    choices = sum(1 for value in (bool(bundle), bool(name), validate_all) if value)
    if choices != 1:
        raise ProtocolError(
            "invalid_field",
            "skill.validate requires exactly one of bundle, name, or all.",
            request_id=request_id,
        )
    results = []
    if bundle:
        path = _workspace_child_path(workspace_root, bundle, request_id=request_id)
        results.append(validate_skill_bundle(path))
    elif name:
        entry = resolve_skill_catalog_entry(entries=catalog.entries, raw_name=name)
        if entry is None:
            raise ProtocolError(
                "skill_not_found", f"Skill not found: {name}", request_id=request_id
            )
        results.append(validate_skill_bundle(entry.skill.bundle_path))
    else:
        seen_paths: set[Path] = set()
        for entry in catalog.entries:
            if entry.skill.bundle_path in seen_paths:
                continue
            seen_paths.add(entry.skill.bundle_path)
            results.append(validate_skill_bundle(entry.skill.bundle_path))
    return {
        "workspace_root": str(workspace_root),
        "results": [_validation_payload(result) for result in results],
        "valid": all(bool(getattr(result, "valid", False)) for result in results),
    }


def _skill_install(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    source = _validate_skill_install_source(
        _required_str(params, "source", request_id=request_id),
        workspace_root=workspace_root,
        params=params,
        request_id=request_id,
    )
    result = install_skill_bundle(
        source=source,
        workspace_root=workspace_root,
        project=_optional_bool(params, "project", default=False),
        subdir=_optional_str_or_none(params, "subdir"),
        force=_optional_bool(params, "force", default=False),
    )
    return {"result": _dataclass_payload(result), "changed": True}


def _skill_enable(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    return _set_skill_enabled(params, request_id=request_id, enabled=True)


def _skill_disable(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    return _set_skill_enabled(params, request_id=request_id, enabled=False)


def _skill_remove(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    _skill_workbench._root(workspace_root, _optional_bool(params, "project", default=False))
    name = _required_str(params, "name", request_id=request_id)
    if name.casefold() in build_plugin_activation_index(workspace_root).skill_lookup_to_plugin_id:
        raise SkillLifecycleError("Remove this skill through its owning plugin.")
    result = remove_managed_skill(
        name=_required_str(params, "name", request_id=request_id),
        workspace_root=workspace_root,
        project=_optional_bool(params, "project", default=False),
    )
    return {"result": _dataclass_payload(result), "changed": True}


def _doctor_summary(_params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    api_key = resolve_api_key(cfg)
    web_search = resolve_web_search_runtime_status(cfg=cfg, api_key=api_key.key)
    try:
        sandbox = diagnose_sandbox(cfg, include_smoke=False, include_server_image=False)
        sandbox_payload: dict[str, Any] = {
            "ready": sandbox.ready,
            "status": sandbox.status,
            "selected_backend": sandbox.selected_backend,
        }
    except ConfigError as exc:
        sandbox_payload = {"ready": False, "status": "config_error", "error": str(exc)}
    try:
        update_status = status_from_cache(current_version=__version__, cfg=cfg).to_json()
    except Exception as exc:  # noqa: BLE001 - summary should remain best effort.
        update_status = {"state": "unknown", "error": str(redact_secrets(str(exc)))}
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "git": shutil.which("git") is not None,
        "rg": shutil.which("rg") is not None,
        "model_set": bool(cfg.model),
        "model": cfg.model,
        "base_url_host": _url_host(cfg.base_url),
        "api_key": _api_key_status(api_key),
        "config_path": str(config_path()),
        "update": update_status,
        "web_search": {
            "availability": web_search.availability_label,
            "provider": web_search.provider,
            "setup_hint": web_search.setup_hint,
        },
        "sandbox": sandbox_payload,
        "custom_tools_enabled": bool(cfg.custom_tools_enabled),
        "skills_enabled": bool(getattr(cfg, "skills_enabled", True)),
        "stream": bool(cfg.stream),
        "secret_values_included": False,
    }


def _doctor_providers(_params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    diagnostics = build_provider_diagnostics(cfg)
    return {
        "diagnostics": _rows_to_dict(diagnostics.rows()),
        "last_provider_call": last_provider_call_summary(),
        "last_web_search": last_web_search_summary(),
        "secret_values_included": False,
    }


def _doctor_providers_live(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    # One minimal live text request against the active profile. Never passive: the caller must
    # send allow_live=true, which the extension only does on an explicit user action (Connect /
    # Test connection). Mirrors `alysis doctor providers --live` and stays fully redacted.
    if not _optional_bool(params, "allow_live", default=False):
        raise ProtocolError(
            "missing_param",
            "doctor.providers.live requires allow_live=true; it performs one minimal live "
            "provider request after explicit user intent.",
            request_id=request_id,
        )
    cfg = load_config()
    validation = validate_active_provider_live(
        cfg,
        timeout_s=float(_positive_int(params, "timeout_s", default=15, upper=60)),
    )
    return {
        "validation": {
            "profile": validation.profile_name,
            "provider_key": validation.provider_key,
            "protocol": validation.protocol,
            "model": validation.model,
            "status": validation.status,
            "message": redact_secrets(validation.message),
        },
        "ok": validation.ok,
        "network_used": True,
        "secret_values_included": False,
    }


def _doctor_bundle(_params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    diagnostics = build_provider_diagnostics(cfg)
    return {
        "bundle": diagnostic_bundle_payload(provider_diagnostics=_rows_to_dict(diagnostics.rows())),
        "secret_values_included": False,
    }


def _sandbox_doctor(params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    diagnostic = diagnose_sandbox(
        cfg,
        include_smoke=_optional_bool(params, "smoke", default=False),
        include_server_image=_optional_bool(params, "include_server", default=True),
    )
    result = {
        "diagnostic": _dataclass_payload(diagnostic),
        "message": format_sandbox_problem_message(diagnostic),
    }
    if _optional_bool(params, "env", default=False):
        result["env"] = sandbox_env_summary()
    return result


def _sandbox_pull(params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    images = _string_list_param(params.get("images"))
    if not images:
        images = list(
            configured_sandbox_images(
                cfg,
                include_server=_optional_bool(params, "include_server", default=True),
            )
        )
    result = pull_sandbox_images(
        images,
        timeout_s=_positive_int(params, "timeout_s", default=900, upper=3600),
    )
    return {"result": _dataclass_payload(result), "changed": True}


def _sandbox_setup(params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    diagnostic = diagnose_sandbox(cfg, include_smoke=False, include_server_image=True)
    pull_result = None
    if (
        not diagnostic.ready
        and diagnostic.can_pull
        and _optional_bool(params, "pull", default=True)
    ):
        pull_result = pull_sandbox_images(configured_sandbox_images(cfg, include_server=True))
        diagnostic = diagnose_sandbox(cfg, include_smoke=True, include_server_image=True)
    return {
        "diagnostic": _dataclass_payload(diagnostic),
        "pull_result": _dataclass_payload(pull_result) if pull_result is not None else None,
        "message": format_sandbox_problem_message(diagnostic),
        "changed": pull_result is not None,
    }


def _update_check(params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    force = _optional_bool(params, "force", default=False)
    allow_network = _optional_bool(params, "allow_network", default=False)
    cached = _optional_bool(params, "cached", default=not (force or allow_network))
    network_used = bool((force or allow_network) and not cached)
    status = (
        check_for_updates(current_version=__version__, cfg=cfg, force=True)
        if network_used
        else status_from_cache(current_version=__version__, cfg=cfg)
    )
    return {
        "status": status.to_json(),
        "network_used": network_used,
        "cached": not network_used,
        "allow_network": allow_network,
        "force": force,
        "secret_values_included": False,
    }


def _report_create(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root = resolve_feedback_workspace_root(
        _workspace_root_from_params(params, request_id=request_id, required=True)
    )
    cfg = load_config()
    result = create_feedback_bundle(
        workspace_root=workspace_root,
        feedback_text=_optional_str_or_none(params, "feedback"),
        cfg=cfg,
        session_id=_optional_str_or_none(params, "session_id"),
        run_id=_optional_str_or_none(params, "run_id"),
        latest=_optional_bool(params, "latest", default=False),
    )
    github_enabled = params.get("github") if "github" in params else False
    issue_result = None
    issue_status_lines: list[str] = []
    if github_enabled is not False and not _optional_bool(params, "local_only", default=True):
        issue_result = create_feedback_github_issue_draft(
            bundle_result=result,
            feedback_text=_optional_str_or_none(params, "feedback"),
            cfg=cfg,
            github_enabled=bool(github_enabled),
            open_browser=False,
        )
        issue_status_lines = list(feedback_github_issue_status_lines(issue_result))
    return {
        "bundle": _dataclass_payload(result),
        "github_issue": _dataclass_payload(issue_result) if issue_result is not None else None,
        "github_status_lines": issue_status_lines,
        "changed": True,
        "secret_values_included": False,
    }


def _mcp_status(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    manager = _mcp_manager(params, request_id=request_id)
    bootstrap_errors: list[str] = []
    prompt_errors: dict[str, str] = {}
    try:
        try:
            _ = manager.tool_bindings
        except (ConfigError, RuntimeError) as exc:
            bootstrap_errors.append(str(redact_secrets(str(exc))))
        prompt_enabled_ids = [
            str(server_id)
            for server_id in manager.catalog_snapshot_metadata().get("prompt_enabled_server_ids")
            or []
        ]
        for prompt_server_id in prompt_enabled_ids:
            try:
                manager.list_prompts(server_id=prompt_server_id, limit=1)
            except (ConfigError, RuntimeError) as exc:
                prompt_errors[prompt_server_id] = str(redact_secrets(str(exc)))
        payload = _mcp_status_payload(
            manager=manager,
            bootstrap_errors=bootstrap_errors,
            prompt_errors=prompt_errors,
        )
        payload.pop("table_rows", None)
        payload["secret_values_included"] = False
        return _redact_payload(payload)
    finally:
        manager.close()


def _mcp_prompts_list(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    manager = _mcp_manager(params, request_id=request_id)
    try:
        payload = manager.list_prompts(
            server_id=_optional_str_or_none(params, "server"),
            query=_optional_str_or_none(params, "query"),
            limit=_positive_int(params, "limit", default=20, upper=50),
            refresh=_optional_bool(params, "refresh", default=False),
        )
        payload["secret_values_included"] = False
        return _redact_payload(payload)
    finally:
        manager.close()


def _mcp_prompts_get(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    manager = _mcp_manager(params, request_id=request_id)
    try:
        payload = manager.get_prompt(
            server_id=_required_str(params, "server_id", request_id=request_id),
            prompt_name=_required_str_any(params, ("prompt_name", "name"), request_id=request_id),
            arguments=_string_dict_param(params.get("arguments"), request_id=request_id),
            refresh=_optional_bool(params, "refresh", default=False),
        )
        payload["secret_values_included"] = False
        return _redact_payload(payload)
    finally:
        manager.close()


def _mcp_auth_status(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    resolved_config = _manual_mcp_resolved_config_for_path(
        path=_workspace_root_from_params(params, request_id=request_id, required=True)
    )
    rows = _build_mcp_auth_status_rows(
        resolved_config=resolved_config,
        server_id=_optional_str_or_none(params, "server"),
    )
    return {"rows": _redact_payload(rows), "secret_values_included": False}


def prepare_mcp_oauth_login_request(
    params: dict[str, Any], *, request_id: RequestId
) -> McpOAuthLoginRequest:
    """Resolve a configured OAuth server into a secret-safe coordinator request."""

    server_id = _required_str(params, "server_id", request_id=request_id)
    resolved_config = _manual_mcp_resolved_config_for_path(
        path=_workspace_root_from_params(params, request_id=request_id, required=True)
    )
    server = _require_http_oauth_server_for_auth(
        server=_resolve_mcp_server_by_id(resolved_config=resolved_config, server_id=server_id),
        command_name="login",
    )
    oauth = server.oauth
    if oauth is None or not server.url:
        raise ProtocolError(
            "mcp_oauth_config_error",
            "MCP server OAuth configuration is incomplete.",
            request_id=request_id,
        )
    return McpOAuthLoginRequest(
        server_id=server.id,
        resource_server_url=str(server.url),
        client_id=oauth.client_id,
        scopes=tuple(oauth.scopes or ()),
        authorization_server_url=oauth.authorization_server_url,
    )


def _mcp_auth_login_start(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    prepare_mcp_oauth_login_request(params, request_id=request_id)
    raise ProtocolError(
        "stateful_bridge_required",
        "MCP OAuth login requires the stateful IDE bridge coordinator.",
        request_id=request_id,
    )


def _mcp_auth_login_status(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    prepare_mcp_oauth_login_request(params, request_id=request_id)
    _required_str(params, "flow_id", request_id=request_id)
    raise ProtocolError(
        "stateful_bridge_required",
        "MCP OAuth flow status requires the stateful IDE bridge coordinator.",
        request_id=request_id,
    )


def _mcp_auth_login_cancel(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    prepare_mcp_oauth_login_request(params, request_id=request_id)
    _required_str(params, "flow_id", request_id=request_id)
    raise ProtocolError(
        "stateful_bridge_required",
        "MCP OAuth flow cancellation requires the stateful IDE bridge coordinator.",
        request_id=request_id,
    )


def _mcp_auth_logout(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    if not _confirmed(params):
        return _confirmation_required("mcp.auth.logout", "clear stored MCP OAuth tokens")
    server_id = _required_str(params, "server_id", request_id=request_id)
    resolved_config = _manual_mcp_resolved_config_for_path(
        path=_workspace_root_from_params(params, request_id=request_id, required=True)
    )
    server = _require_http_oauth_server_for_auth(
        server=_resolve_mcp_server_by_id(resolved_config=resolved_config, server_id=server_id),
        command_name="logout",
    )
    removed = delete_oauth_token_record(server.id)
    return {
        "server_id": server.id,
        "removed": bool(removed),
        "changed": bool(removed),
        "secret_values_included": False,
    }


def _hooks_list(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    entries: list[dict[str, Any]] = []
    statuses = _collect_hook_source_statuses(workspace_root=workspace_root)
    for status in statuses:
        if not status.exists or status.status == "invalid":
            continue
        try:
            config_file = load_hook_config_file(status.path)
        except ConfigError:
            continue
        for event_name, groups in config_file.hooks.items():
            for group in groups:
                if not group.enabled:
                    continue
                for hook in sorted(
                    (item for item in group.hooks if item.enabled),
                    key=lambda item: (-item.priority, item.id or "", item.command),
                ):
                    entries.append(
                        {
                            "source_scope": status.source_scope,
                            "status": status.status,
                            "trusted": bool(status.trusted),
                            "event": event_name,
                            "matcher": group.matcher or "*",
                            "id": hook.id or "",
                            "priority": hook.priority,
                            "failure_policy": hook.failure_policy,
                            "runtime_kinds": list(hook.runtime_kinds or ()),
                            "session_source": list(hook.session_source or ()),
                            "command": str(redact_secrets(hook.command)),
                        }
                    )
    return {
        "workspace_root": str(workspace_root),
        "sources": [_hook_source_status_payload(status) for status in statuses],
        "hooks": entries,
        "count": len(entries),
        "secret_values_included": False,
    }


def _hooks_doctor(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    statuses = _collect_hook_source_statuses(workspace_root=workspace_root)
    result: dict[str, Any] = {
        "workspace_root": str(workspace_root),
        "sources": [_hook_source_status_payload(status) for status in statuses],
        "effective": {},
        "matcher_errors": [],
        "untrusted_project_paths": [],
        "secret_values_included": False,
    }
    try:
        resolved = load_resolved_hooks_config(workspace_root)
        result["effective"] = {
            event: len(groups) for event, groups in resolved.groups_by_event.items()
        }
        matcher_errors: list[dict[str, str]] = []
        for event_name, groups in resolved.groups_by_event.items():
            for group in groups:
                if not group.matcher:
                    continue
                try:
                    re.compile(group.matcher)
                except re.error as exc:
                    matcher_errors.append(
                        {
                            "event": event_name,
                            "source_path": str(group.source_path),
                            "matcher": group.matcher,
                            "error": str(redact_secrets(str(exc))),
                        }
                    )
        result["matcher_errors"] = matcher_errors
        result["untrusted_project_paths"] = [str(path) for path in resolved.untrusted_project_paths]
    except ConfigError as exc:
        result["effective_error"] = str(redact_secrets(str(exc)))
    return result


def _hooks_trace(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    sessions_dir = resolve_sessions_dir(cfg)
    session_id = _optional_str_or_none(params, "session_id")
    if not session_id:
        infos = list_sessions(sessions_dir)
        if not infos:
            return {"session_id": None, "events": [], "count": 0, "secret_values_included": False}
        session_id = infos[0].session_id
    artifact_path = hook_audit_artifact_path(sessions_dir=sessions_dir, session_id=session_id)
    events = list(read_hook_audit_events(artifact_path))
    limit = _positive_int(params, "limit", default=200, upper=2_000)
    limited = events[-limit:]
    return {
        "session_id": session_id,
        "artifact_path": str(artifact_path),
        "events": _redact_payload(limited),
        "count": len(limited),
        "total_count": len(events),
        "secret_values_included": False,
    }


def _hooks_test(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    canonical_event = _canonical_hook_event(params, request_id=request_id)
    runtime_kind = _runtime_kind_value(
        params,
        key="runtime_kind",
        default="interactive_chat",
        request_id=request_id,
    )
    session_source = _optional_str(params, "session_source", default="startup").strip().lower()
    if session_source not in _HOOK_SESSION_SOURCES:
        raise ProtocolError(
            "invalid_field",
            "session_source must be one of: startup, resume, fork.",
            request_id=request_id,
        )
    tool = _optional_str(params, "tool", default="").strip()
    resolved = load_resolved_hooks_config(workspace_root)
    rows: list[dict[str, Any]] = []
    for group in resolved.groups_for_event(canonical_event):
        matcher = str(group.matcher or "")
        for hook in group.hooks:
            matched, reason = _hook_test_match_result(
                event_name=canonical_event,
                matcher_target=tool,
                runtime_kind=runtime_kind,
                session_source=session_source,
                matcher=matcher,
                hook=hook,
            )
            rows.append(
                {
                    "source_scope": group.source_scope,
                    "trusted": bool(group.trusted),
                    "matcher": matcher or "*",
                    "id": hook.id or "",
                    "runtime_kinds": list(hook.runtime_kinds or ()),
                    "session_source": list(hook.session_source or ()),
                    "matched": bool(matched),
                    "reason": reason,
                    "command": str(redact_secrets(hook.command)),
                }
            )
    return {
        "workspace_root": str(workspace_root),
        "event": canonical_event,
        "matches": rows,
        "ignored_untrusted_project_paths": [str(path) for path in resolved.untrusted_project_paths],
        "secret_values_included": False,
    }


def _hooks_trust(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    target = _required_str(params, "target", request_id=request_id)
    if target != "project_config":
        raise ProtocolError(
            "invalid_field",
            "hooks.trust currently supports only target='project_config'.",
            request_id=request_id,
        )
    workspace_root, config_path = _project_hooks_config(
        params, request_id=request_id, validate=True
    )
    trust_project_hooks_config(
        workspace_root=workspace_root,
        config_path=config_path,
        expected_hash=_optional_str_or_none(params, "expected_hash"),
    )
    return {
        "target": target,
        "workspace_root": str(workspace_root),
        "config_path": str(config_path),
        "trusted": True,
        "changed": True,
        "secret_values_included": False,
    }


def _hooks_untrust(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    target = _required_str(params, "target", request_id=request_id)
    if target != "project_config":
        raise ProtocolError(
            "invalid_field",
            "hooks.untrust currently supports only target='project_config'.",
            request_id=request_id,
        )
    workspace_root, config_path = _project_hooks_config(
        params, request_id=request_id, validate=False
    )
    untrust_project_hooks_config(workspace_root=workspace_root, config_path=config_path)
    return {
        "target": target,
        "workspace_root": str(workspace_root),
        "config_path": str(config_path),
        "trusted": False,
        "changed": True,
        "secret_values_included": False,
    }


def _hooks_init(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    local_config_path = project_local_hooks_config_path(workspace_root)
    force = _optional_bool(params, "force", default=False)
    if local_config_path.exists() and not force:
        return _confirmation_required("hooks.init", "overwrite existing local hooks config")
    local_config_path.parent.mkdir(parents=True, exist_ok=True)
    local_config_path.write_text(
        json.dumps(_HOOKS_INIT_TEMPLATE, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    gitignore_changed = False
    gitignore_path = workspace_root / ".gitignore"
    gitignore_entry = ".alysis/hooks.local.json"
    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
        already_ignored = any(
            line.strip() == gitignore_entry or line.strip() == ".alysis/"
            for line in existing.splitlines()
        )
        if not already_ignored:
            with gitignore_path.open("a", encoding="utf-8") as handle:
                if not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(f"{gitignore_entry}\n")
            gitignore_changed = True
    return {
        "workspace_root": str(workspace_root),
        "config_path": str(local_config_path),
        "changed": True,
        "gitignore_changed": gitignore_changed,
        "secret_values_included": False,
    }


def _hooks_effective(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    canonical_event = _canonical_hook_event(params, request_id=request_id)
    tool = _optional_str_or_none(params, "tool")
    runtime = _runtime_kind_value(
        params,
        key="runtime",
        default="interactive_chat",
        request_id=request_id,
    )
    session_source = _optional_str_or_none(params, "session_source")
    resolved = load_resolved_hooks_config(workspace_root)
    rows: list[dict[str, Any]] = []
    order = 0
    for group in resolved.groups_for_event(canonical_event):
        matcher_pass = True
        matcher_reason = ""
        if tool is not None and canonical_event in {"PreToolUse", "PostToolUse", "SubagentStop"}:
            if group.matcher:
                try:
                    matcher_pass = re.compile(group.matcher).search(tool) is not None
                    if not matcher_pass:
                        matcher_reason = f"matcher {group.matcher!r} does not match {tool!r}"
                except re.error as exc:
                    matcher_pass = False
                    matcher_reason = f"matcher regex error: {exc}"
        for hook in group.hooks:
            order += 1
            fires = matcher_pass
            reason = matcher_reason
            if fires and hook.runtime_kinds and runtime not in set(hook.runtime_kinds):
                fires = False
                reason = f"runtime_kind {runtime!r} not in {list(hook.runtime_kinds)}"
            if fires and hook.session_source:
                if session_source is None:
                    fires = False
                    reason = "sessionSource filter set but session_source not provided"
                elif session_source not in set(hook.session_source):
                    fires = False
                    reason = f"session_source {session_source!r} not in {list(hook.session_source)}"
            rows.append(
                {
                    "order": order,
                    "id": hook.id or "",
                    "fires": bool(fires),
                    "reason": reason or "",
                    "priority": hook.priority,
                    "source_scope": group.source_scope,
                    "source_path": str(group.source_path),
                    "command": str(redact_secrets(hook.command)),
                }
            )
    return {
        "workspace_root": str(workspace_root),
        "event": canonical_event,
        "hooks": rows,
        "count": len(rows),
        "secret_values_included": False,
    }


def _hooks_enable(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    return _set_hook_enabled_for_protocol(params, request_id=request_id, enabled=True)


def _hooks_disable(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    return _set_hook_enabled_for_protocol(params, request_id=request_id, enabled=False)


def _conventions_list(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root, focus_path = _conventions_paths(params, request_id=request_id)
    documents = load_repo_conventions(focus_path=focus_path, workspace_root=workspace_root)
    return {
        "workspace_root": str(workspace_root),
        "focus_path": str(focus_path),
        "documents": [_conventions_document_payload(doc, workspace_root) for doc in documents],
        "count": len(documents),
    }


def _conventions_render(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    workspace_root, focus_path = _conventions_paths(params, request_id=request_id)
    documents = load_repo_conventions(focus_path=focus_path, workspace_root=workspace_root)
    max_chars = _positive_int(
        params,
        "max_chars",
        default=_CONVENTIONS_RENDER_DEFAULT_MAX_CHARS,
        upper=_CONVENTIONS_RENDER_MAX_CHARS,
    )
    max_bytes = _positive_int(
        params,
        "max_bytes",
        default=_CONVENTIONS_RENDER_DEFAULT_MAX_BYTES,
        upper=_CONVENTIONS_RENDER_MAX_BYTES,
    )
    rendered = render_repo_conventions_context(
        documents=documents,
        max_chars=max_chars,
    )
    redacted_rendered = _redact_payload(rendered) if rendered is not None else None
    if isinstance(redacted_rendered, str):
        limited_rendered, truncated_by_bytes = _truncate_text_utf8(redacted_rendered, max_bytes)
    else:
        limited_rendered = None
        truncated_by_bytes = False
    rendered_bytes = len((limited_rendered or "").encode("utf-8"))
    truncated_by_chars = rendered is not None and len(rendered) >= max_chars
    return {
        "workspace_root": str(workspace_root),
        "focus_path": str(focus_path),
        "rendered": limited_rendered,
        "document_count": len(documents),
        "truncated_or_limited": truncated_by_chars or truncated_by_bytes,
        "truncated": truncated_by_chars or truncated_by_bytes,
        "truncated_by_chars": truncated_by_chars,
        "truncated_by_bytes": truncated_by_bytes,
        "max_chars": max_chars,
        "max_bytes": max_bytes,
        "rendered_chars": len(limited_rendered or ""),
        "rendered_bytes": rendered_bytes,
        "secret_values_included": False,
        "redacted": True,
    }


def _ext_search(params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    query = params.get("query", "")
    if not isinstance(query, str):
        raise ProtocolError("invalid_param", "query must be a string.", request_id=_request_id)
    registry = load_registry()
    matches = search_extensions(registry, query)
    return {
        "extensions": [_registry_entry_payload(entry) for entry in matches],
        "count": len(matches),
    }


def _ext_list(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    repo_root = _workspace_root_from_params(params, request_id=request_id, required=False)
    global_state = load_global_state()
    project_overrides = load_project_overrides(repo_root)
    project_state = load_project_state(repo_root)
    effective_enabled = set(
        resolve_active_plugins(repo_root=repo_root, workspace_trust_prompt=None).enabled_plugin_ids
    )
    installed: list[dict[str, Any]] = []
    for ext_id in sorted(set(global_state.installed) | set(project_state.installed)):
        record = global_state.installed.get(ext_id) or project_state.installed.get(ext_id)
        if record is None:
            continue
        installed.append(_installed_plugin_payload(ext_id, record, effective_enabled))
    return {
        "workspace_root": str(repo_root),
        "extensions": installed,
        "count": len(installed),
        "project_overrides": {
            "enabled": sorted(project_overrides.enabled),
            "disabled": sorted(project_overrides.disabled),
        },
        "workspace_trust": _extension_workspace_trust_payload(repo_root),
        "secret_values_included": False,
    }


def _ext_info(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    ext_id = _required_str_any(params, ("ext_id", "id", "plugin_id"), request_id=request_id)
    repo_root = _workspace_root_from_params(params, request_id=request_id, required=False)
    registry = load_registry()
    global_state = load_global_state()
    project_overrides = load_project_overrides(repo_root)
    project_state = load_project_state(repo_root)
    entry = find_by_id(registry, ext_id)
    normalized = normalize_extension_id(entry.id if entry is not None else ext_id)
    global_record = _installed_record(global_state, normalized)
    project_record = _installed_record(project_state, normalized)
    installed_record = global_record or project_record
    if entry is None and installed_record is None:
        raise ProtocolError(
            "extension_not_found", f"Extension not found: {ext_id}", request_id=request_id
        )
    effective_enabled = set(
        resolve_active_plugins(repo_root=repo_root, workspace_trust_prompt=None).enabled_plugin_ids
    )
    # A bundled install whose pin differs from the descriptor this engine ships.
    update_available = (
        entry is not None
        and installed_record is not None
        and getattr(installed_record, "source_kind", "git") == "bundled"
        and getattr(entry, "source_kind", "git") == "bundled"
        and getattr(installed_record, "package_sha256", None) != entry.package_sha256
    )
    custom = (
        _custom_extension_payload(normalized, repo_root)
        if entry is None and getattr(installed_record, "source_kind", "git") == "custom"
        else None
    )
    imported = None
    if (
        entry is None
        and installed_record is not None
        and installed_record.source_kind == "snapshot"
    ):
        package_path = installed_plugin_root(
            normalized, project=global_record is None, repo_root=repo_root
        )
        try:
            manifest = load_manifest(package_path)
            imported = {
                "id": normalized,
                "name": manifest.plugin.name,
                "description": manifest.plugin.description,
                "publisher": manifest.plugin.author,
                "version": manifest.plugin.version,
                "catalog_source": "Imported package",
                "components": installed_record.component_ids,
                "manifest_review": manifest.model_dump(mode="json"),
                "permissions": [
                    key for key, values in installed_record.component_ids.items() if values
                ],
            }
        except PluginManifestError:
            imported = {
                "id": normalized,
                "description": "Installed manifest is invalid. Re-import to repair this plugin.",
            }
    return {
        "workspace_root": str(repo_root),
        "extension": _registry_entry_payload(entry)
        if entry is not None
        else custom or imported or {"id": ext_id},
        "installed": _dataclass_payload(installed_record),
        "installed_scopes": [
            scope
            for scope, record in (("user", global_record), ("project", project_record))
            if record is not None
        ],
        "enabled_effective": normalized in effective_enabled,
        "update_available": update_available,
        # The shipped copy grants different access, so the installed one stays off until the
        # person reviews and installs the update.
        "needs_review": update_available
        and not _installed_grants_bundled_access(
            normalized, entry, project=global_record is None, repo_root=repo_root
        ),
        "project_override_state": _project_extension_override_state(project_overrides, normalized),
        "workspace_trust": _extension_workspace_trust_payload(repo_root),
        "secret_values_included": False,
    }


def _ext_install(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    source = _required_str(params, "source", request_id=request_id)
    _validate_ext_install_source(source, request_id=request_id)
    repo_root = _workspace_root_from_params(params, request_id=request_id, required=False)
    project = _optional_bool(params, "project", default=False)
    approval = _ext_install_trust_approval_param(params, request_id=request_id)
    if approval is not None and not _confirmed(params):
        return _confirmation_required(
            "ext.install",
            "install extension package after explicit package trust review",
        )
    trust_request: Any | None = None

    def _trust_prompt(request: Any) -> bool:
        nonlocal trust_request
        trust_request = request
        if approval is None:
            return False
        _validate_ext_install_trust_approval(
            request,
            approval,
            source=source,
            project=project,
            request_id=request_id,
        )
        return True

    try:
        result = install_plugin(
            source=source,
            repo_root=repo_root,
            project=project,
            trust_prompt=_trust_prompt,
        )
    except PluginInstallError as exc:
        if (
            trust_request is not None
            and approval is None
            and str(exc) == "install rejected by user"
        ):
            return _ext_install_trust_review_result(
                trust_request,
                source=source,
                project=project,
            )
        raise
    return {
        "result": _dataclass_payload(result),
        "changed": bool(getattr(result, "trust_was_prompted", True)),
        "secret_values_included": False,
    }


_CUSTOM_PLUGIN_ID_RE = re.compile(r"^custom\.[a-z][a-z0-9_]{0,19}$")


def _ext_custom_save(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    """Save the user's own online plugin (Plugins M2d), after the same two-step trust review.

    The desktop app supplies the descriptor; the engine accepts only the restricted custom
    form (one HTTPS endpoint, the person's tools and marks, optional skills). Every edit is a
    new descriptor hash, so it needs the person's consent again.
    """
    plugin_id = _required_str(params, "plugin_id", request_id=request_id)
    # The exact text: the app pins the same bytes' hash, so nothing here may trim or rewrite it.
    descriptor = params.get("descriptor")
    if not isinstance(descriptor, str) or not descriptor.strip():
        raise ProtocolError(
            "missing_param", "ext.custom.save requires descriptor.", request_id=request_id
        )
    if not _CUSTOM_PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ProtocolError(
            "invalid_extension_source",
            "A custom plugin id is custom.<lowercase name>.",
            request_id=request_id,
        )
    content = descriptor.encode("utf-8")
    if len(content) > 64 * 1024:
        raise ProtocolError(
            "invalid_extension_source", "The custom descriptor is too large.", request_id=request_id
        )
    source = f"custom:{plugin_id}"
    repo_root = _workspace_root_from_params(params, request_id=request_id, required=False)
    approval = _ext_install_trust_approval_param(params, request_id=request_id)
    if approval is not None and not _confirmed(params):
        return _confirmation_required(
            "ext.custom.save", "save the custom plugin after explicit review of its descriptor"
        )
    trust_request: Any | None = None

    def _trust_prompt(request: Any) -> bool:
        nonlocal trust_request
        trust_request = request
        if approval is None:
            return False
        _validate_ext_install_trust_approval(
            request, approval, source=source, project=False, request_id=request_id
        )
        return True

    try:
        result = install_plugin(
            source=source,
            repo_root=repo_root,
            project=False,
            trust_prompt=_trust_prompt,
            custom_descriptor=content,
        )
    except PluginInstallError as exc:
        if (
            trust_request is not None
            and approval is None
            and str(exc) == "install rejected by user"
        ):
            review = _ext_install_trust_review_result(trust_request, source=source, project=False)
            review["action"]["method"] = "ext.custom.save"
            return review
        raise ProtocolError("invalid_custom_plugin", str(exc), request_id=request_id) from exc
    return {
        "result": _dataclass_payload(result),
        "changed": bool(getattr(result, "trust_was_prompted", True)),
        "secret_values_included": False,
    }


def _custom_extension_payload(ext_id: str, repo_root: Path) -> dict[str, Any] | None:
    """A custom plugin's page and consent details, from its installed descriptor."""
    try:
        manifest = load_manifest(installed_plugin_root(ext_id, project=False, repo_root=repo_root))
    except (OSError, ValueError, PluginManifestError):
        return None
    if not manifest.components.custom_connector:
        return None
    connector = manifest.components.custom_connector[0]
    short = manifest.plugin.id.split(".", 1)[1]
    return {
        "id": manifest.plugin.id,
        "name": manifest.plugin.name,
        "description": manifest.plugin.description,
        "version": manifest.plugin.version,
        "source_kind": "custom",
        "catalog_source": "custom",
        "publisher": "You",
        "tier": "custom",
        "categories": [],
        "permissions": [],
        "connects_to": [str(connector.url)],
        "custom_connector": {
            "url": str(connector.url),
            "auth": connector.auth,
            "auth_header": connector.auth_header,
            "auth_bearer": connector.auth_bearer,
            "oauth_issuer": str(connector.oauth_issuer) if connector.oauth_issuer else None,
            "oauth_client": connector.oauth_client,
            "oauth_scopes": list(connector.oauth_scopes),
            "tools": [
                {"name": tool.name, "reads_only": tool.reads_only} for tool in connector.tools
            ],
        },
        "skills": [
            {
                "name": f"{short}:{skill.name}",
                "title": skill.title,
                "description": skill.description,
                "uses_tools": list(skill.uses_tools),
                "text": skill.text,
            }
            for skill in manifest.components.plugin_skill
        ],
    }


def _ext_uninstall(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    if not _confirmed(params):
        return _confirmation_required("ext.uninstall", "uninstall extension package")
    result = uninstall_plugin(
        plugin_id=_required_str_any(params, ("plugin_id", "ext_id", "id"), request_id=request_id),
        repo_root=_workspace_root_from_params(params, request_id=request_id, required=False),
        project=_optional_bool(params, "project", default=False),
    )
    return {"result": _dataclass_payload(result), "changed": True, "secret_values_included": False}


def _ext_enable(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    return _set_ext_enabled(params, request_id=request_id, enabled=True)


def _ext_disable(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    return _set_ext_enabled(params, request_id=request_id, enabled=False)


def _set_skill_enabled(
    params: dict[str, Any],
    *,
    request_id: RequestId,
    enabled: bool,
) -> dict[str, Any]:
    name = _required_str(params, "name", request_id=request_id)
    project = _optional_bool(params, "project", default=False)
    workspace_root, catalog = _skill_catalog(params, request_id=request_id)
    _skill_workbench._root(workspace_root, project)
    key = name.strip().casefold()
    if project:
        project_state = load_project_skill_state(workspace_root)
        if resolve_skill_catalog_entry(entries=catalog.entries, raw_name=name) is None and (
            key not in project_state.managed_installs
        ):
            raise ProtocolError(
                "skill_not_found",
                f"Skill not found for project override: {name}",
                request_id=request_id,
            )
        state = set_project_skill_override(project_state, name=name, enabled=enabled)
        save_project_skill_state(workspace_root, state)
        scope = "project"
    else:
        global_state = load_global_skill_state()
        visible_user_skill = next(
            (
                entry
                for entry in catalog.entries
                if str(getattr(entry.skill, "source_scope", "")) == "user"
                and key in entry.skill.lookup_keys()
            ),
            None,
        )
        if visible_user_skill is None and key not in global_state.managed_installs:
            raise ProtocolError(
                "skill_not_found", f"Global skill not found: {name}", request_id=request_id
            )
        state = set_global_skill_disabled(global_state, name=name, disabled=not enabled)
        save_global_skill_state(state)
        scope = "user"
    return {
        "workspace_root": str(workspace_root),
        "name": name,
        "enabled": enabled,
        "scope": scope,
        "changed": True,
    }


def _mcp_manager(params: dict[str, Any], *, request_id: RequestId) -> Any:
    return _manual_mcp_manager_for_path(
        path=_workspace_root_from_params(params, request_id=request_id, required=True),
        runtime_kind=_runtime_kind_value(
            params,
            key="runtime",
            default="interactive_chat",
            request_id=request_id,
            mcp_prompt_runtime=True,
        ),
    )


def _runtime_kind_value(
    params: dict[str, Any],
    *,
    key: str,
    default: str,
    request_id: RequestId,
    mcp_prompt_runtime: bool = False,
) -> str:
    raw = _optional_str(params, key, default=default)
    try:
        runtime = _parse_manual_mcp_prompt_runtime(raw) if mcp_prompt_runtime else RuntimeKind(raw)
    except (ConfigError, ValueError) as exc:
        raise ProtocolError("invalid_field", str(exc), request_id=request_id) from exc
    return runtime.value


def _string_dict_param(value: Any, *, request_id: RequestId) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProtocolError(
            "invalid_field",
            "arguments must be an object with string keys and values.",
            request_id=request_id,
        )
    parsed: dict[str, str] = {}
    for key, child in value.items():
        key_text = str(key or "").strip()
        if not key_text:
            raise ProtocolError(
                "invalid_field", "argument keys cannot be empty.", request_id=request_id
            )
        parsed[key_text] = str(child)
    return parsed


def _hook_source_status_payload(status: Any) -> dict[str, Any]:
    return {
        "source_scope": status.source_scope,
        "path": str(status.path),
        "exists": bool(status.exists),
        "trusted": bool(status.trusted),
        "status": status.status,
        "event_count": int(status.event_count),
        "hook_count": int(status.hook_count),
        "issue": str(redact_secrets(status.issue)),
    }


def _project_hooks_config(
    params: dict[str, Any],
    *,
    request_id: RequestId,
    validate: bool,
) -> tuple[Path, Path]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    config_path = project_hooks_config_path(workspace_root)
    if not config_path.exists():
        raise ProtocolError(
            "hooks_config_not_found",
            "Project hooks config was not found.",
            request_id=request_id,
        )
    if validate:
        load_hook_config_file(config_path)
    return workspace_root, config_path


def _canonical_hook_event(params: dict[str, Any], *, request_id: RequestId) -> str:
    try:
        return canonicalize_hook_event_name(
            _required_str_any(params, ("event", "event_name"), request_id=request_id)
        )
    except ValueError as exc:
        raise ProtocolError("invalid_field", str(exc), request_id=request_id) from exc


def _set_hook_enabled_for_protocol(
    params: dict[str, Any],
    *,
    request_id: RequestId,
    enabled: bool,
) -> dict[str, Any]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    hook_id = _required_str(params, "hook_id", request_id=request_id)
    layer = _optional_str(params, "layer", default="local")
    config_path = _hooks_config_path_for_layer(workspace_root=workspace_root, layer=layer)
    if not config_path.exists():
        raise ProtocolError(
            "hooks_config_not_found", "Hooks config was not found.", request_id=request_id
        )
    result = _find_hook_in_layer(path=config_path, hook_id=hook_id)
    if result is None:
        raise ProtocolError(
            "hook_not_found", f"Hook id not found: {hook_id}", request_id=request_id
        )
    raw, entry = result
    previous = bool(entry.get("enabled", True))
    entry["enabled"] = enabled
    config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "workspace_root": str(workspace_root),
        "config_path": str(config_path),
        "hook_id": hook_id,
        "layer": layer,
        "enabled": enabled,
        "previous_enabled": previous,
        "changed": previous != enabled,
        "secret_values_included": False,
    }


def _conventions_paths(params: dict[str, Any], *, request_id: RequestId) -> tuple[Path, Path]:
    raw = params.get("focus_path", params.get("path", params.get("workspace")))
    if raw is None:
        raise ProtocolError(
            "missing_param",
            "conventions methods require workspace, path, or focus_path.",
            request_id=request_id,
        )
    focus_path = Path(str(raw)).expanduser().resolve()
    workspace_root = _workspace_root_from_params(
        {"workspace": str(focus_path if focus_path.is_dir() else focus_path.parent)},
        request_id=request_id,
        required=True,
    )
    try:
        focus_path.relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise ProtocolError(
            "path_outside_workspace",
            "Conventions focus_path must stay inside the resolved workspace.",
            request_id=request_id,
        ) from exc
    return workspace_root, focus_path


def _conventions_document_payload(document: Any, workspace_root: Path) -> dict[str, Any]:
    try:
        display_path = document.path.relative_to(workspace_root).as_posix()
    except ValueError:
        display_path = str(document.path)
    return {
        "name": document.name,
        "trust_level": document.trust_level,
        "chars": len(document.content),
        "path": str(document.path),
        "workspace_relative_path": display_path,
    }


def _registry_entry_payload(entry: Any) -> dict[str, Any]:
    if entry is None:
        return {}
    return {
        "id": entry.id,
        "name": entry.name,
        "description": entry.description,
        "repo": entry.repo,
        "commit": entry.commit,
        "version": entry.version,
        "tags": list(entry.tags),
        "permissions": list(entry.permissions),
        "source_kind": getattr(entry, "source_kind", "git"),
        "package_sha256": getattr(entry, "package_sha256", None),
        "upstream_commit": getattr(entry, "upstream_commit", None),
        "categories": list(getattr(entry, "categories", None) or []),
        "publisher": getattr(entry, "publisher", None),
        "tier": getattr(entry, "tier", None),
        "catalog_source": "built-in",
        "connects_to": _bundled_endpoints(entry),
        "skills": _bundled_skills(entry),
        **_bundled_verified_connector(entry),
    }


def _pinned_bundled_manifest(entry: Any) -> Any | None:
    if getattr(entry, "source_kind", "git") != "bundled":
        return None
    try:
        return parse_bundled_manifest(bundled_manifest(entry))
    except (OSError, RuntimeError):
        return None


def _bundled_endpoints(entry: Any) -> list[str]:
    """Where a built-in plugin connects, read from its pinned descriptor (not from display text)."""
    manifest = _pinned_bundled_manifest(entry)
    if manifest is None:
        return []
    return [str(server.url) for server in manifest.components.mcp_server if server.url] + [
        origin
        for connector in manifest.components.verified_connector
        for origin in (connector.url, *connector.oauth_sign_in_origins)
    ]


def _bundled_verified_connector(entry: Any) -> dict[str, Any]:
    """A verified plugin's server, sign-in and reviewed tools, from its pinned descriptor.

    The app compares the identity fields with its own pins before it signs in or sends a token.
    """
    manifest = _pinned_bundled_manifest(entry)
    if manifest is None or not manifest.components.verified_connector:
        return {}
    connector = manifest.components.verified_connector[0]
    return {
        "verified_connector": {
            "id": connector.id,
            "url": connector.url,
            "resource": connector.effective_resource,
            "oauth_issuer": connector.oauth_issuer,
            "oauth_sign_in_origins": list(connector.oauth_sign_in_origins),
            "oauth_scopes": list(connector.oauth_scopes),
            "max_argument_bytes": connector.max_argument_bytes,
            "host_arguments": [
                {"name": argument.name, "source": argument.source}
                for argument in connector.host_arguments
            ],
            "tools": [
                {"name": tool.name, "reads_only": tool.reads_only} for tool in connector.tools
            ],
        }
    }


def _bundled_skills(entry: Any) -> list[dict[str, Any]]:
    """The plugin's skills in full, from its pinned descriptor, for its page and the consent."""
    manifest = _pinned_bundled_manifest(entry)
    if manifest is None:
        return []
    short = manifest.plugin.id.split(".", 1)[1]
    return [
        {
            "name": f"{short}:{skill.name}",
            "title": skill.title,
            "description": skill.description,
            "uses_tools": list(skill.uses_tools),
            "text": skill.text,
        }
        for skill in manifest.components.plugin_skill
    ]


def _installed_grants_bundled_access(
    ext_id: str, entry: Any, *, project: bool, repo_root: Path
) -> bool:
    try:
        installed = load_manifest(
            installed_plugin_root(ext_id, project=project, repo_root=repo_root)
        )
        return same_bundled_access(installed, bundled_manifest(entry))
    except (OSError, RuntimeError, ValueError, PluginManifestError):
        return False


def _installed_record(state: Any, ext_id: str) -> Any | None:
    normalized = normalize_extension_id(ext_id)
    for installed_id, installed in state.installed.items():
        if normalize_extension_id(installed_id) == normalized:
            return installed
    return None


def _installed_plugin_payload(
    ext_id: str,
    record: Any,
    effective_enabled: set[str],
) -> dict[str, Any]:
    normalized = normalize_extension_id(ext_id)
    return {
        "id": ext_id,
        "enabled_effective": normalized in effective_enabled,
        "version": getattr(record, "version", "") or "",
        "trust": getattr(record, "trust", "") or "",
        "commit": getattr(record, "commit", "") or "",
        "scope": getattr(record, "scope", "") or "",
        "installed_at": getattr(record, "installed_at", "") or "",
        "component_ids": _redact_payload(getattr(record, "component_ids", {}) or {}),
        "source_kind": getattr(record, "source_kind", "git"),
        "source_url": getattr(record, "source_url", None),
        "manifest_sha256": getattr(record, "manifest_sha256", None),
        "package_sha256": getattr(record, "package_sha256", None),
        "upstream_commit": getattr(record, "upstream_commit", None),
    }


def _project_extension_override_state(project_overrides: Any, normalized: str) -> str:
    enabled = {normalize_extension_id(item) for item in project_overrides.enabled}
    disabled = {normalize_extension_id(item) for item in project_overrides.disabled}
    if normalized in enabled:
        return "enabled"
    if normalized in disabled:
        return "disabled"
    return "absent"


def _extension_workspace_trust_payload(repo_root: Path) -> dict[str, Any]:
    overrides_path = project_extensions_path(repo_root)
    if not overrides_path.exists():
        return {"state": "no_overrides_file", "path": str(overrides_path)}
    raw_bytes = overrides_path.read_bytes()
    overrides_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return {
        "state": "trusted"
        if extension_workspace_trusted(repo_root=repo_root, overrides_sha256=overrides_sha256)
        else "untrusted",
        "path": str(overrides_path),
        "sha256": overrides_sha256,
    }


def _set_ext_enabled(
    params: dict[str, Any],
    *,
    request_id: RequestId,
    enabled: bool,
) -> dict[str, Any]:
    method = "ext.enable" if enabled else "ext.disable"
    if not _confirmed(params):
        return _confirmation_required(
            method, f"{'enable' if enabled else 'disable'} extension package"
        )
    fn = enable_plugin if enabled else disable_plugin
    result = fn(
        plugin_id=_required_str_any(params, ("plugin_id", "ext_id", "id"), request_id=request_id),
        repo_root=_workspace_root_from_params(params, request_id=request_id, required=False),
        project=_optional_bool(params, "project", default=False),
    )
    return {"result": _dataclass_payload(result), "changed": True, "secret_values_included": False}


def _custom_tool_entries(
    params: dict[str, Any],
    *,
    request_id: RequestId,
) -> tuple[Path, tuple[CustomToolCatalogEntry, ...]]:
    from ..cli_impl.commands.tools import _discover_custom_tools_for_path

    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    cfg = load_config()
    resolved_root, state = _discover_custom_tools_for_path(path=workspace_root, cfg=cfg)
    return resolved_root, tuple(getattr(state, "catalog_entries", ()) or ())


def _select_project_tool_entry(
    entries: tuple[CustomToolCatalogEntry, ...],
    *,
    name: str,
    request_id: RequestId,
) -> CustomToolCatalogEntry:
    matches = [entry for entry in entries if entry.name.casefold() == name.casefold()]
    if not matches:
        raise ProtocolError(
            "tool_not_found", f"Custom tool not found: {name}", request_id=request_id
        )
    for entry in matches:
        if (
            entry.source_scope == "project"
            and entry.spec is not None
            and entry.status != "shadowed"
        ):
            return entry
    raise ProtocolError(
        "unsupported_tool_trust_target",
        "Trust commands only apply to valid project custom tools.",
        request_id=request_id,
    )


def _available_skill_catalog(
    params: dict[str, Any], *, request_id: RequestId
) -> tuple[Path, Any, frozenset[str]]:
    workspace_root, catalog = _skill_catalog(params, request_id=request_id)
    # Management requests do not grant workspace trust. Use exactly the same
    # plugin activation and ownership filter as noninteractive agent sessions.
    effective, _ = filter_discovered_skills_for_plugins(
        discovered=catalog.effective,
        activation_decision=resolve_active_plugins(
            repo_root=workspace_root, workspace_trust_prompt=None
        ),
        index=build_plugin_activation_index(workspace_root),
    )
    available = (
        frozenset(skill.name for skill in effective.ordered)
        if load_config().skills_enabled
        else frozenset()
    )
    return workspace_root, catalog, available


def _skill_catalog(params: dict[str, Any], *, request_id: RequestId) -> tuple[Path, Any]:
    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    discovered = discover_skills(
        focus_path=workspace_root,
        workspace_root=workspace_root,
        cfg=load_config(),
    )
    catalog = resolve_skill_catalog(discovered=discovered, workspace_root=workspace_root)
    return workspace_root, catalog


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


def _workspace_child_path(workspace_root: Path, raw: str, *, request_id: RequestId) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise ProtocolError(
            "path_outside_workspace",
            "IDE management file paths must stay inside the workspace.",
            request_id=request_id,
        ) from exc
    return resolved


def _validate_skill_install_source(
    source: str,
    *,
    workspace_root: Path,
    params: dict[str, Any],
    request_id: RequestId,
) -> str:
    raw = source.strip()
    if not raw:
        raise ProtocolError(
            "missing_param",
            "skill.install requires source.",
            request_id=request_id,
        )
    _reject_url_userinfo_value(raw, field="skill.install.source", request_id=request_id)
    if _is_remote_skill_source(raw):
        if _unsupported_remote_source(raw):
            raise ProtocolError(
                "unsupported_remote_source",
                "Git skill installs require HTTPS; local directories and zip archives are also supported.",
                request_id=request_id,
            )
        if not (_optional_bool(params, "allow_remote", default=False) and _confirmed(params)):
            raise ProtocolError(
                "remote_source_requires_confirmation",
                "Remote skill installs require allow_remote=true and explicit confirmation.",
                request_id=request_id,
            )
        return raw

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    if candidate.is_symlink():
        raise ProtocolError(
            "skill_install_symlink_rejected",
            "skill.install source paths must not be symlinks.",
            request_id=request_id,
        )
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise ProtocolError(
            "path_outside_workspace",
            "skill.install local source paths must stay inside the workspace.",
            request_id=request_id,
        ) from exc
    if not resolved.exists():
        raise ProtocolError(
            "skill_install_source_not_found",
            "skill.install local source path was not found.",
            request_id=request_id,
        )
    if resolved.is_symlink():
        raise ProtocolError(
            "skill_install_symlink_rejected",
            "skill.install source paths must not be symlinks.",
            request_id=request_id,
        )
    if resolved.is_dir():
        return str(resolved)
    if resolved.is_file() and resolved.suffix.casefold() == ".zip":
        return str(resolved)
    raise ProtocolError(
        "unsupported_skill_install_source",
        "skill.install local source must be a directory or .zip archive inside the workspace.",
        request_id=request_id,
    )


def _validate_ext_install_source(source: str, *, request_id: RequestId) -> None:
    raw = source.strip()
    if not raw:
        raise ProtocolError("missing_param", "ext.install requires source.", request_id=request_id)
    _reject_url_userinfo_value(raw, field="ext.install.source", request_id=request_id)
    if _EXTENSION_REGISTRY_ID_RE.fullmatch(raw):
        return
    raise ProtocolError(
        "invalid_extension_source",
        "ext.install source must be a registry id. Developer git sources are CLI-only.",
        request_id=request_id,
    )


def _is_remote_skill_source(source: str) -> bool:
    # A native Windows drive or UNC share is a filesystem source, not a URL
    # scheme. It must pass the workspace boundary checks below like other paths.
    if Path(source).drive:
        return False
    parsed = urlparse(source)
    return (
        bool(parsed.scheme)
        or source.startswith("git@")
        or source.startswith("ssh://")
        or source.startswith("git://")
    )


def _unsupported_remote_source(source: str) -> bool:
    parsed = urlparse(source)
    if source.startswith("git+https://"):
        return False
    if parsed.scheme == "https":
        return False
    return True


def _looks_like_local_path(source: str) -> bool:
    return (
        "/" in source
        or "\\" in source
        or source.startswith(".")
        or source.startswith("~")
        or Path(source).is_absolute()
    )


def _session_log_path(params: dict[str, Any], *, request_id: RequestId) -> Path:
    session_id = _required_str(params, "session_id", request_id=request_id)
    sanitized = sanitize_session_id(session_id)
    if sanitized != session_id:
        raise ProtocolError(
            "invalid_session_id",
            "Session id contains unsupported characters.",
            request_id=request_id,
        )
    cfg = load_config()
    path = resolve_sessions_dir(cfg) / f"{session_id}.jsonl"
    if not path.exists() or not path.is_file():
        raise ProtocolError(
            "session_log_not_found", f"Session log not found: {session_id}", request_id=request_id
        )
    return path


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


def _safe_config_payload(cfg: AppConfig) -> dict[str, Any]:
    payload = cfg.model_dump()
    for key in list(payload):
        if _unsafe_config_response_key(key):
            payload[key] = "<redacted>" if payload[key] else payload[key]
    extra = dict(cfg.extra_fields or {})
    profiles_raw = extra.get("profiles")
    if isinstance(profiles_raw, dict):
        extra["profiles"] = {
            name: _safe_profile_config_dict(name, value)
            for name, value in sorted(profiles_raw.items(), key=lambda item: str(item[0]))
            if isinstance(value, dict)
        }
    payload["extra_fields"] = _redact_mapping_by_key(extra)
    return payload


def _safe_profile_config_dict(name: object, value: dict[str, Any]) -> dict[str, Any]:
    safe = dict(value)
    env_var = safe.pop("api_key_env", None)
    if env_var:
        safe["env_var"] = str(env_var)
    headers = safe.get("extra_headers")
    if isinstance(headers, dict):
        safe["extra_headers"] = {
            "names": sorted(str(key) for key in headers),
            "count": len(headers),
            "values_redacted": True,
        }
    safe["name"] = str(name)
    return _redact_mapping_by_key(safe)


def _profile_payload(
    cfg: AppConfig,
    profile: ProfileSpec | None,
    *,
    include_details: bool = False,
) -> dict[str, Any]:
    if profile is None:
        return {}
    resolved = resolve_profile_api_key(cfg, profile.name)
    active = profile.name == str((cfg.extra_fields or {}).get("active_profile") or "")
    auth_provider = str(profile.auth_provider or "").strip() or None
    payload: dict[str, Any] = {
        "name": profile.name,
        "active": active,
        "protocol": profile.protocol,
        "base_url": profile.base_url,
        "base_url_host": _url_host(profile.base_url),
        "default_model": profile.default_model,
        # These are selection metadata, not credentials. The IDE needs the
        # authoritative active-subscription verdict so it does not enable a
        # composer merely because an account profile exists while its required
        # model/reasoning selection is still incomplete.
        "auth_provider": auth_provider,
        "reasoning_effort": profile.reasoning_effort,
        "subscription_selection_ready": (
            active_subscription_selection_ready(cfg)
            if active and auth_provider is not None
            else None
        ),
        "web_search_adapter": profile.web_search_adapter,
        "web_search_model": profile.web_search_model,
        "notes": profile.notes,
        "key_env_var": profile.api_key_env,
        "api_key": _api_key_status(resolved),
        "extra_headers": {
            "names": sorted(profile.extra_headers),
            "count": len(profile.extra_headers),
            "values_redacted": True,
        },
    }
    if include_details:
        payload["protocol_kind"] = (
            "native" if profile.protocol != "openai_compat" else "compatibility"
        )
    return payload


def _preset_payload(preset: Any) -> dict[str, Any]:
    return {
        "key": preset.key,
        "label": preset.label,
        "selection_label": preset_selection_label(preset),
        "protocol": preset.protocol,
        "protocol_kind": preset_protocol_kind(preset),
        "base_url": preset.base_url,
        "base_url_host": _url_host(preset.base_url),
        "key_env_var": preset.api_key_env,
        "suggested_models": list(preset.suggested_models),
        # Forwarded so IDE pickers can label a model with what it is for instead of showing a
        # bare slug. Both maps are static catalog prose - never credential-bearing.
        "suggested_model_descriptions": dict(preset.suggested_model_descriptions or {}),
        "model_aliases": dict(preset.model_aliases or {}),
        "validation_model": preset.validation_model,
        "web_search_adapter": preset.web_search_adapter,
        "web_search_model": preset.web_search_model,
        "setup_warning": preset.setup_warning,
        "notes": preset.notes,
    }


def _secret_action_for_profile(profile: ProfileSpec) -> dict[str, Any] | None:
    if not profile.api_key_env:
        return None
    return {
        "kind": "open_configure_provider",
        "reason": "Provider credentials must be supplied through VS Code SecretStorage or existing CLI credential sources.",
        "profile": profile.name,
        "env_var": profile.api_key_env,
    }


def _api_key_status(value: Any) -> dict[str, Any]:
    return {
        "present": bool(getattr(value, "key", None)),
        "source": str(getattr(value, "source", "missing") or "missing"),
    }


def _extra_headers_secret_storage_action(
    params: dict[str, Any], *, request_id: RequestId
) -> dict[str, Any] | None:
    raw = params.get("extra_headers")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtocolError(
            "invalid_field",
            "extra_headers must be an object.",
            request_id=request_id,
        )
    if not raw:
        return None
    header_names: list[str] = []
    for key in raw:
        key_text = str(key or "").strip()
        if not key_text:
            raise ProtocolError(
                "invalid_field",
                "extra_headers keys must be non-empty strings.",
                request_id=request_id,
            )
        header_names.append(key_text)
    return {
        "kind": "requires_secret_storage",
        "method": "profile.add",
        "reason": "Provider extra header values cannot be passed through IDE protocol params.",
        "next_step": "open_configure_provider",
        "header_names": sorted(dict.fromkeys(header_names)),
        "values_redacted": True,
    }


def _extra_headers_param(params: dict[str, Any], *, request_id: RequestId) -> dict[str, str]:
    raw = params.get("extra_headers")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProtocolError(
            "invalid_field",
            "extra_headers must be an object.",
            request_id=request_id,
        )
    headers: dict[str, str] = {}
    for key, value in raw.items():
        key_text = str(key or "").strip()
        value_text = str(value or "").strip()
        if not key_text or not value_text:
            raise ProtocolError(
                "invalid_field",
                "extra_headers keys and values must be non-empty strings.",
                request_id=request_id,
            )
        headers[key_text] = value_text
    return headers


def _reject_secret_bearing_value(
    value: Any,
    *,
    field: str,
    request_id: RequestId,
) -> None:
    if _redacted_value_differs(value):
        raise ProtocolError(
            "inline_secret_rejected",
            f"{field} contains a secret-looking value; use configured credential storage instead.",
            request_id=request_id,
        )


def _redacted_value_differs(value: Any) -> bool:
    return _json_safe(redact_secrets(value)) != _json_safe(value)


def _reject_url_userinfo_value(
    value: Any,
    *,
    field: str,
    request_id: RequestId,
) -> None:
    if not isinstance(value, str):
        return
    parsed = urlparse(value.strip())
    if parsed.username is None and parsed.password is None:
        return
    raise ProtocolError(
        "invalid_base_url",
        f"{field} must not include URL userinfo.",
        request_id=request_id,
    )


def _custom_tool_payload(
    entry: CustomToolCatalogEntry,
    *,
    workspace_root: Path,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": entry.name,
        "source_scope": entry.source_scope,
        "trust": entry.trust,
        "status": entry.status,
        "source_path": str(entry.source_path),
        "detail": entry.detail,
        "manifest_version": entry.manifest_version,
        "exposed": entry.exposed,
    }
    if entry.spec is not None:
        payload.update(
            {
                "description": entry.spec.description,
                "file_hash": entry.spec.file_hash,
                "relative_tool_path": entry.spec.relative_tool_path,
                "capabilities": entry.spec.capabilities.to_dict(),
                "timeout_s": entry.spec.timeout_s,
                "required_env": list(entry.spec.required_env),
                "missing_env": list(entry.spec.missing_env),
                "enabled_in": list(entry.spec.enabled_in),
                "isolation": entry.spec.isolation,
                "input_schema": entry.spec.input_schema,
                "output_schema": entry.spec.output_schema,
            }
        )
    if entry.issue is not None:
        payload["issue"] = {
            "code": entry.issue.code,
            "message": entry.issue.message,
            "relative_tool_path": entry.issue.relative_tool_path,
        }
    try:
        payload["workspace_relative_path"] = (
            entry.source_path.resolve().relative_to(workspace_root.resolve()).as_posix()
        )
    except ValueError:
        payload["workspace_relative_path"] = None
    return payload


def _skill_entry_payload(entry: Any) -> dict[str, Any]:
    skill = entry.skill
    payload = _skill_payload(skill)
    payload.update(
        {
            "enabled": bool(entry.enabled),
            "managed": bool(entry.managed),
            "disabled_by": entry.disabled_by,
            "install_record": _dataclass_payload(entry.install_record)
            if entry.install_record is not None
            else None,
        }
    )
    return payload


def _skill_payload(skill: Any) -> dict[str, Any]:
    return {
        "name": skill.name,
        "description": skill.description,
        "bundle_name": skill.bundle_name,
        "bundle_path": str(skill.bundle_path),
        "entry_path": str(skill.entry_path),
        "source_scope": skill.source_scope,
        "source_kind": skill.source_kind,
        "source_family": skill.source_family,
        "source_path": str(skill.source_path),
        "trust_level": skill.trust_level,
        "ancestor_distance": skill.ancestor_distance,
        "aliases": list(skill.aliases),
        "metadata": dict(skill.metadata),
    }


def _skill_issues_payload(catalog: Any) -> dict[str, Any]:
    return {
        "discovery": [
            {"source_path": str(issue.source_path), "message": issue.message}
            for issue in getattr(catalog.effective, "issues", ())
        ],
        "lifecycle": [
            {"source_path": str(issue.source_path), "message": issue.message}
            for issue in getattr(catalog, "lifecycle_issues", ())
        ],
    }


def _validation_payload(result: Any) -> dict[str, Any]:
    return {
        "bundle_path": str(result.bundle_path),
        "entry_path": str(result.entry_path),
        "valid": bool(result.valid),
        "name": result.name,
        "description": result.description,
        "metadata": dict(result.metadata),
        "issues": [
            {
                "severity": issue.severity,
                "message": issue.message,
                "path": str(issue.path),
            }
            for issue in result.issues
        ],
    }


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


def _rows_to_dict(rows: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {str(key): str(value) for key, value in rows}


def _url_host(value: str | None) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(str(value or "")).netloc
    except ValueError:
        return ""


def _redact_mapping_by_key(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if _unsafe_config_response_key(key_text):
                out[key_text] = "<redacted>" if child else child
            else:
                out[key_text] = _redact_mapping_by_key(child)
        return out
    if isinstance(value, list):
        return [_redact_mapping_by_key(child) for child in value]
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


def _json_size_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _truncate_text_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _unsafe_config_response_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in {"api_key_env", "key_env_var"}:
        return False
    return normalized in {"prompt_cache_key"} or any(
        marker in normalized
        for marker in ("api_key", "token", "secret", "password", "credential", "authorization")
    )


def _unsafe_config_set_key(key: str) -> bool:
    normalized = key.strip().lower()
    return normalized in {"prompt_cache_key"} or any(
        marker in normalized
        for marker in ("api_key", "token", "secret", "password", "credential", "authorization")
    )


def _config_value_to_cli_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return str(value)


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


def _required_str_any(
    params: dict[str, Any],
    keys: tuple[str, ...],
    *,
    request_id: RequestId,
) -> str:
    for key in keys:
        if key in params:
            return _required_str(params, key, request_id=request_id)
    raise ProtocolError(
        "missing_param",
        f"Missing required string param: {' or '.join(keys)}.",
        request_id=request_id,
    )


def _optional_str(params: dict[str, Any], key: str, *, default: str) -> str:
    value = params.get(key)
    if value is None:
        return default
    return str(value)


def _optional_str_or_none(params: dict[str, Any], key: str) -> str | None:
    if key not in params or params.get(key) is None:
        return None
    text = str(params.get(key) or "").strip()
    return text or None


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


def _positive_int(
    params: dict[str, Any],
    key: str,
    *,
    default: int,
    upper: int,
) -> int:
    value = params.get(key)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(parsed, upper))


def _string_list_param(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _ext_install_trust_approval_param(
    params: dict[str, Any], *, request_id: RequestId
) -> dict[str, Any] | None:
    raw = params.get("trust_approval")
    if raw is None:
        raw = params.get("trustApproval")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtocolError(
            "invalid_field",
            "trust_approval must be an object returned from an ext.install trust review.",
            request_id=request_id,
        )
    if raw.get("approved") is not True:
        raise ProtocolError(
            "extension_trust_required",
            "trust_approval.approved must be true to install extension packages.",
            request_id=request_id,
        )
    required = ("plugin_id", "commit", "manifest_sha256", "approval_fingerprint")
    approval: dict[str, Any] = {"approved": True}
    for field in required:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ProtocolError(
                "invalid_field",
                f"trust_approval.{field} must be a non-empty string.",
                request_id=request_id,
            )
        approval[field] = value.strip()
    if "project" in raw:
        if not isinstance(raw["project"], bool):
            raise ProtocolError(
                "invalid_field",
                "trust_approval.project must be a boolean when provided.",
                request_id=request_id,
            )
        approval["project"] = raw["project"]
    for optional in ("source", "source_url"):
        value = raw.get(optional)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ProtocolError(
                    "invalid_field",
                    f"trust_approval.{optional} must be a non-empty string when provided.",
                    request_id=request_id,
                )
            approval[optional] = value.strip()
    return approval


def _ext_install_trust_review_result(
    request: Any,
    *,
    source: str,
    project: bool,
) -> dict[str, Any]:
    trust_request = _redact_payload(_dataclass_payload(request))
    approval = _ext_install_required_approval(
        trust_request,
        source=source,
        project=project,
    )
    return {
        "changed": False,
        "action": {
            "kind": "requires_extension_trust_review",
            "method": "ext.install",
            "reason": "extension package install requires explicit review of manifest, source, permissions, and components",
            "approval_schema": "extension_install_trust_approval_v1",
            "required_approval": approval,
        },
        "trust_request": trust_request,
        "secret_values_included": False,
    }


def _validate_ext_install_trust_approval(
    request: Any,
    approval: dict[str, Any],
    *,
    source: str,
    project: bool,
    request_id: RequestId,
) -> None:
    trust_request = _redact_payload(_dataclass_payload(request))
    expected = _ext_install_required_approval(
        trust_request,
        source=source,
        project=project,
    )
    for field in ("plugin_id", "commit", "manifest_sha256", "approval_fingerprint"):
        if approval.get(field) != expected[field]:
            raise ProtocolError(
                "extension_trust_mismatch",
                f"trust_approval.{field} does not match the extension package being installed.",
                request_id=request_id,
            )
    if "project" in approval and approval["project"] != expected["project"]:
        raise ProtocolError(
            "extension_trust_mismatch",
            "trust_approval.project does not match the requested install scope.",
            request_id=request_id,
        )
    if "source" in approval and approval["source"] != expected["source"]:
        raise ProtocolError(
            "extension_trust_mismatch",
            "trust_approval.source does not match the requested install source.",
            request_id=request_id,
        )
    if "source_url" in approval and approval["source_url"] != expected["source_url"]:
        raise ProtocolError(
            "extension_trust_mismatch",
            "trust_approval.source_url does not match the resolved package source.",
            request_id=request_id,
        )


def _ext_install_required_approval(
    trust_request: dict[str, Any],
    *,
    source: str,
    project: bool,
) -> dict[str, Any]:
    approval = {
        "approved": True,
        "plugin_id": str(trust_request.get("plugin_id") or ""),
        "commit": str(trust_request.get("commit") or ""),
        "manifest_sha256": str(trust_request.get("manifest_sha256") or ""),
        "source": str(source or ""),
        "source_url": str(trust_request.get("source_url") or ""),
        "project": bool(project),
    }
    approval["approval_fingerprint"] = _ext_install_approval_fingerprint(approval)
    return approval


def _ext_install_approval_fingerprint(approval: dict[str, Any]) -> str:
    payload = {
        key: approval[key]
        for key in (
            "plugin_id",
            "commit",
            "manifest_sha256",
            "source",
            "source_url",
            "project",
        )
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _confirmed(params: dict[str, Any]) -> bool:
    return _optional_bool(params, "yes", default=False) or _optional_bool(
        params, "confirm", default=False
    )


def _confirmation_required(method: str, reason: str) -> dict[str, Any]:
    return {
        "changed": False,
        "action": {
            "kind": "requires_confirmation",
            "method": method,
            "reason": reason,
        },
        "secret_values_included": False,
    }


_HANDLERS: dict[str, Callable[[dict[str, Any], RequestId], dict[str, Any]]] = {
    "skill.import.describe": _skill_import_describe,
    "skill.import.package": _skill_import_package,
    "skill.import.preview": _skill_import_preview,
    "skill.import.install": _skill_import_install,
    "skill.import.discard": _skill_import_discard,
    "skill.document.read": _skill_document_read,
    "skill.document.save": _skill_document_save,
    "skill.update.preview": _skill_update_preview,
    "hooks.document.read": _hooks_document_read,
    "hooks.document.save": _hooks_document_save,
    "provider.auth.status": _provider_auth_status,
    "config.get": _config_get,
    "config.set": _config_set,
    "config.schema": _config_schema,
    "config.validate": _config_validate,
    "profile.list": _profile_list,
    "profile.show": _profile_show,
    "profile.add": _profile_add,
    "profile.remove": _profile_remove,
    "profile.use": _profile_use,
    "profile.rename": _profile_rename,
    "profile.presets": _profile_presets,
    "profile.preset": _profile_preset,
    "profile.convert": _profile_convert,
    "session.show": _session_show,
    "session.usage": _session_usage,
    "session.score": _session_score,
    "tools.catalog": _tools_catalog,
    "tool.list": _tool_list,
    "tool.info": _tool_info,
    "tool.trust": _tool_trust,
    "tool.untrust": _tool_untrust,
    "commands.list": _commands_list,
    "agents.list": _agents_list,
    "skill.list": _skill_list,
    "skill.info": _skill_info,
    "skill.init": _skill_init,
    "skill.validate": _skill_validate,
    "skill.install": _skill_install,
    "skill.enable": _skill_enable,
    "skill.disable": _skill_disable,
    "skill.remove": _skill_remove,
    "doctor.summary": _doctor_summary,
    "doctor.providers": _doctor_providers,
    "doctor.providers.live": _doctor_providers_live,
    "doctor.bundle": _doctor_bundle,
    "sandbox.doctor": _sandbox_doctor,
    "sandbox.setup": _sandbox_setup,
    "sandbox.pull": _sandbox_pull,
    "update.check": _update_check,
    "report.create": _report_create,
    "mcp.status": _mcp_status,
    "mcp.prompts.list": _mcp_prompts_list,
    "mcp.prompts.get": _mcp_prompts_get,
    "mcp.auth.status": _mcp_auth_status,
    "mcp.auth.login.start": _mcp_auth_login_start,
    "mcp.auth.login.status": _mcp_auth_login_status,
    "mcp.auth.login.cancel": _mcp_auth_login_cancel,
    "mcp.auth.logout": _mcp_auth_logout,
    "hooks.list": _hooks_list,
    "hooks.doctor": _hooks_doctor,
    "hooks.trace": _hooks_trace,
    "hooks.test": _hooks_test,
    "hooks.trust": _hooks_trust,
    "hooks.untrust": _hooks_untrust,
    "hooks.init": _hooks_init,
    "hooks.effective": _hooks_effective,
    "hooks.enable": _hooks_enable,
    "hooks.disable": _hooks_disable,
    "conventions.list": _conventions_list,
    "conventions.render": _conventions_render,
    "ext.search": _ext_search,
    "ext.list": _ext_list,
    "ext.info": _ext_info,
    "ext.install": _ext_install,
    "ext.custom.save": _ext_custom_save,
    "ext.uninstall": _ext_uninstall,
    "ext.enable": _ext_enable,
    "ext.disable": _ext_disable,
}
