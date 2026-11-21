"""Desktop settings and skill catalog operations shared by both bridge roles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..computer_use_attribution import phase
from ..config import (
    AppConfig,
    config_path,
    credentials_path,
    load_config,
    resolve_api_key,
    resolve_profile_api_key,
)
from ..profiles import ProfileSpec, active_subscription_selection_ready, list_profiles
from .management_common import (
    _dataclass_payload,
    _optional_bool,
    _optional_str,
    _required_str,
    _unsafe_config_response_key,
    _workspace_root_from_params,
)
from .protocol import ProtocolError, RequestId


def _provider_auth_status(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    from ..provider_auth import create_provider_auth
    from ..provider_auth.status import native_provider_status_fields

    provider_id = _required_str(params, "provider_id", request_id=request_id)
    status = native_provider_status_fields(provider_id, create_provider_auth)
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


def _provider_auth_select_model(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    # ``alysis auth select-model`` without a new process (one shared implementation).
    from ..cli_impl.commands.auth import select_native_subscription_model

    payload = select_native_subscription_model(
        _required_str(params, "provider_id", request_id=request_id),
        _required_str(params, "model", request_id=request_id),
        _optional_str(params, "reasoning_effort", default="auto"),
    )
    return {**payload, "changed": True, "secret_values_included": False}


def _provider_auth_reasoning(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    # ``alysis auth reasoning MODEL --profile PROFILE``: the model's effort choices, saving nothing.
    from ..cli_impl.commands.auth import native_reasoning_capabilities

    capabilities = native_reasoning_capabilities(
        _required_str(params, "model", request_id=request_id),
        _required_str(params, "profile", request_id=request_id),
    )
    return {"models": [capabilities], "secret_values_included": False}


def _config_get(_params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    from ..llm.metadata import credential_scope_fingerprint

    with phase("config"):
        cfg = load_config()
        api_key = resolve_api_key(cfg)
        return {
            "config": _safe_config_payload(cfg),
            "active_profile": str((cfg.extra_fields or {}).get("active_profile") or ""),
            "api_key": _api_key_status(api_key),
            # `config show`'s credential_id: the one-way route fingerprint of the key in use
            # (empty without one), never the key. With it the desktop app reads the account
            # here instead of starting a CLI process for every account read. As with
            # provider.auth.status, its wire name keeps clear of the credential-field
            # redactor, which hides any `credential*` field.
            "route_scope": credential_scope_fingerprint(api_key.key),
            "config_path": str(config_path()),
            "credentials_path": str(credentials_path()),
            # The saved-keys file (`credentials_path` above is hidden by that redactor): the
            # app watches it for key changes. A file location, not credential material.
            "saved_keys_path": str(credentials_path()),
            "secret_values_included": False,
        }


def _config_schema(_params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    return {
        "schema": AppConfig.model_json_schema(),
        "secret_values_included": False,
    }


def _profile_list(_params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    active = str((cfg.extra_fields or {}).get("active_profile") or "")
    return {
        "active_profile": active,
        "profiles": [_profile_payload(cfg, profile) for profile in list_profiles(cfg)],
        "secret_values_included": False,
    }


def _profile_select_model(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    # ``alysis profile select-model`` without a new process: the same single locked write that
    # activates the API profile, keeps the model as its default and routes execution natively.
    from ..cli_impl.commands.profile import select_api_profile_model

    payload = select_api_profile_model(
        _required_str(params, "name", request_id=request_id),
        _required_str(params, "model", request_id=request_id),
    )
    return {**payload, "changed": True, "secret_values_included": False}


def _commands_list(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    # Reuse the terminal catalog; hosts decide which commands have native handlers.
    from ..cli_impl.chat_slash_catalog import command_rows

    mode = params.get("mode", "chat")
    if mode != "chat":
        raise ProtocolError("invalid_field", "mode must be chat", request_id=request_id)
    return {"commands": command_rows()}


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


def _skill_list(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    cfg = load_config()
    workspace_root, catalog, available = _available_skill_catalog(
        params, request_id=request_id, cfg=cfg
    )
    return {
        "workspace_root": str(workspace_root),
        "skills_enabled": bool(cfg.skills_enabled),
        "skills": [
            {**_skill_entry_payload(entry), "available": entry.skill.name in available}
            for entry in catalog.entries
        ],
        "issues": _skill_issues_payload(catalog),
    }


def _skill_info(params: dict[str, Any], request_id: RequestId) -> dict[str, Any]:
    from ..skills import render_skill_info_text, resolve_skill_catalog_entry

    name = _required_str(params, "name", request_id=request_id)
    workspace_root, catalog, available = _available_skill_catalog(
        params, request_id=request_id, cfg=load_config()
    )
    entry = resolve_skill_catalog_entry(entries=catalog.entries, raw_name=name)
    if entry is None:
        raise ProtocolError("skill_not_found", f"Skill not found: {name}", request_id=request_id)
    return {
        "workspace_root": str(workspace_root),
        "skill": {**_skill_entry_payload(entry), "available": entry.skill.name in available},
        "info_text": render_skill_info_text(entry.skill, catalog_entry=entry),
        "issues": _skill_issues_payload(catalog),
    }


def _sandbox_doctor(params: dict[str, Any], _request_id: RequestId) -> dict[str, Any]:
    from ..sandbox_doctor import (
        diagnose_sandbox,
        format_sandbox_problem_message,
        sandbox_env_summary,
    )

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


def _available_skill_catalog(
    params: dict[str, Any], *, request_id: RequestId, cfg: AppConfig
) -> tuple[Path, Any, frozenset[str]]:
    from ..extensions.activation import resolve_active_plugins
    from ..extensions.component_activation import (
        build_plugin_activation_index,
        filter_discovered_skills_for_plugins,
    )

    workspace_root, catalog = _skill_catalog(params, request_id=request_id, cfg=cfg)
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
        frozenset(skill.name for skill in effective.ordered) if cfg.skills_enabled else frozenset()
    )
    return workspace_root, catalog, available


def _skill_catalog(
    params: dict[str, Any], *, request_id: RequestId, cfg: AppConfig | None = None
) -> tuple[Path, Any]:
    from ..skills import discover_skills, resolve_skill_catalog

    workspace_root = _workspace_root_from_params(params, request_id=request_id, required=True)
    discovered = discover_skills(
        focus_path=workspace_root,
        workspace_root=workspace_root,
        cfg=load_config() if cfg is None else cfg,
    )
    catalog = resolve_skill_catalog(discovered=discovered, workspace_root=workspace_root)
    return workspace_root, catalog


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


def _api_key_status(value: Any) -> dict[str, Any]:
    return {
        "present": bool(getattr(value, "key", None)),
        "source": str(getattr(value, "source", "missing") or "missing"),
    }


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


SETTINGS_HANDLERS = {
    "config.get": _config_get,
    "config.schema": _config_schema,
    "profile.list": _profile_list,
    "agents.list": _agents_list,
    "sandbox.doctor": _sandbox_doctor,
    "provider.auth.status": _provider_auth_status,
    "provider.auth.reasoning": _provider_auth_reasoning,
    "profile.selectModel": _profile_select_model,
    "provider.auth.selectModel": _provider_auth_select_model,
    "commands.list": _commands_list,
    "skill.list": _skill_list,
    "skill.info": _skill_info,
}
