"""Which model each subagent uses: the list hosts show, and the one change they make.

The list is the registry a session loads (built-ins, the project's ``.alysis_agents/``,
then the user's ``agents/`` folder), with the model and provider each subagent would
run on, resolved the way a session resolves them. A change rewrites only the ``model``
and ``profile`` lines of the subagent's file in the user's ``agents/`` folder. A
built-in without a file gets a metadata-only override, which keeps its prompt, tools
and permissions.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_text
from .config import AppConfig, ConfigError, resolve_api_key
from .frontmatter_utils import split_frontmatter
from .model_router import PREFER_CONTEXT_FORGE, resolve_model_for_role
from .profiles import get_active_profile, get_profile, list_profiles
from .subagents import (
    SubagentDefinition,
    built_in_subagents,
    canonical_subagent_name,
    load_subagent_registry,
    sanitize_subagent_name,
    subagent_unavailability,
    user_agents_directory,
)

# One frontmatter line each: the engine's frontmatter reader takes `key: value` lines.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")
_OVERRIDE_KEYS = ("model", "profile")


def _resolved_choice(
    definition: SubagentDefinition, cfg: AppConfig
) -> tuple[str | None, str | None, str | None]:
    """(model, profile, problem) as a run of this subagent would resolve them now."""

    from .agent.subagent_execution import SubagentLauncher

    try:
        child_cfg, _api_key, _role = SubagentLauncher._resolve_subagent_config(
            definition, cfg, api_key=resolve_api_key(cfg).key
        )
        return str(child_cfg.model or "").strip() or None, get_active_profile(child_cfg).name, None
    except ConfigError as exc:
        return None, None, str(exc)


def _frontmatter_name(path: Path) -> str | None:
    try:
        frontmatter, _body = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    if frontmatter is None:
        return None
    for line in frontmatter.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "name":
            return sanitize_subagent_name(value.strip().strip('"').strip("'"))
    return sanitize_subagent_name(path.stem)


def _user_files_by_name() -> dict[str, Path]:
    """The user's file for each subagent name; a later file wins, as when loading."""

    directory = user_agents_directory()
    if not directory.is_dir():
        return {}
    files: dict[str, Path] = {}
    for path in sorted(directory.glob("*.md")):
        name = _frontmatter_name(path)
        canonical = canonical_subagent_name(name) if name else None
        if canonical:
            files[canonical] = path
    return files


def _safe_role_model(cfg: AppConfig, role: str, *, forge: bool) -> str | None:
    try:
        model = resolve_model_for_role(
            cfg=cfg,
            role=role,
            plan=None,
            prefer_context=PREFER_CONTEXT_FORGE if forge else "default",
        )
    except ConfigError:
        return None
    return str(model or "").strip() or None


def _roles_payload(cfg: AppConfig) -> list[dict[str, Any]]:
    from .cli_impl.config_menu import FORGE_ROLE_ORDER, ROLE_ORDER, _role_description

    extra = cfg.extra_fields if isinstance(cfg.extra_fields, dict) else {}
    saved = extra.get("role_models") if isinstance(extra.get("role_models"), dict) else {}
    forge_saved = (
        extra.get("forge_role_models") if isinstance(extra.get("forge_role_models"), dict) else {}
    )
    roles: list[dict[str, Any]] = []
    for role in ROLE_ORDER:
        in_forge = role in FORGE_ROLE_ORDER
        roles.append(
            {
                "role": role,
                "description": _role_description(role),
                "model": str(saved.get(role) or "").strip() or None,
                "effective_model": _safe_role_model(cfg, role, forge=False),
                "forge": in_forge,
                "forge_model": (str(forge_saved.get(role) or "").strip() or None)
                if in_forge
                else None,
                "forge_effective_model": _safe_role_model(cfg, role, forge=True)
                if in_forge
                else None,
            }
        )
    return roles


def _profiles_payload(cfg: AppConfig) -> list[dict[str, Any]]:
    """Each saved provider profile, and whether a subagent can run on it.

    A profile other than the active one needs its own base URL and its own key or account;
    the check is the one a run makes first, with no model call.
    """

    from .agent.subagent_execution import SubagentLauncher

    try:
        active = get_active_profile(cfg).name
    except ConfigError:
        active = ""
    probe = built_in_subagents()["explorer"]
    api_key = resolve_api_key(cfg).key
    profiles: list[dict[str, Any]] = []
    for spec in list_profiles(cfg):
        reason: str | None = None
        try:
            # A model is named so a profile without a default model is still judged on its
            # connection alone.
            SubagentLauncher._resolve_subagent_config(
                replace(probe, profile=spec.name, model="probe"), cfg, api_key=api_key
            )
        except ConfigError as exc:
            reason = str(exc)
        profiles.append(
            {
                "name": spec.name,
                "active": spec.name == active,
                "usable": reason is None,
                "reason": reason,
            }
        )
    return profiles


def subagent_catalog(cfg: AppConfig, *, root: Path | None = None) -> dict[str, Any]:
    """Every subagent a session in ``root`` would load, with the model it would use."""

    registry = load_subagent_registry(root=root)
    builtins = built_in_subagents()
    user_files = _user_files_by_name()
    agents: list[dict[str, Any]] = []
    for name, definition in registry.items():
        unavailable = subagent_unavailability(name, registry=registry, cfg=cfg)
        model, profile, problem = _resolved_choice(definition, cfg)
        builtin = name in builtins
        user_file = user_files.get(name)
        settable = builtin or user_file is not None
        agents.append(
            {
                "name": name,
                "builtin": builtin,
                "description": definition.description,
                "mode": definition.mode,
                "allow_workspace_writes": bool(definition.allow_workspace_writes),
                "model_role": definition.model_role or "coding",
                "model": definition.model or None,
                "profile": definition.profile or None,
                "enabled": definition.enabled,
                "available": unavailable is None,
                "unavailable": unavailable.to_payload() if unavailable is not None else None,
                "effective_model": model,
                "effective_profile": profile,
                "problem": problem,
                "user_file": str(user_file) if user_file is not None else None,
                "settable": settable,
                "not_settable_reason": None
                if settable
                else (f"Defined by this project. Set its model in .alysis_agents/{name}.md."),
            }
        )
    return {
        "subagents_enabled": bool(cfg.subagents_enabled),
        "user_agents_dir": str(user_agents_directory()),
        "agents": agents,
        "roles": _roles_payload(cfg),
        "profiles": _profiles_payload(cfg),
    }


def _clean(value: str | None, *, field: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not _MODEL_RE.fullmatch(text):
        raise ConfigError(
            f"{field} must be one word of letters, digits and . _ : / @ + - (at most 200)."
        )
    return text


def _with_values(frontmatter: str, values: dict[str, str | None]) -> list[str]:
    """The frontmatter lines with each key in ``values`` set once, or removed when None."""

    lines: list[str] = []
    written: set[str] = set()
    for line in frontmatter.splitlines():
        key, separator, _value = line.partition(":")
        key = key.strip()
        if separator and key in values and not line.lstrip().startswith("#"):
            if key not in written and values[key] is not None:
                lines.append(f"{key}: {values[key]}")
            written.add(key)
            continue
        lines.append(line)
    for key, value in values.items():
        if key not in written and value is not None:
            lines.append(f"{key}: {value}")
    return lines


def _sets_anything(lines: list[str]) -> bool:
    for line in lines:
        key, separator, _value = line.partition(":")
        if separator and not line.lstrip().startswith("#") and key.strip() not in {"", "name"}:
            return True
    return False


def set_subagent_model(
    cfg: AppConfig,
    name: str,
    *,
    model: str | None,
    profile: str | None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Save which model (and provider profile) a subagent uses; None for both clears it.

    The choice is checked the way a run checks it first: the profile must exist, and a
    profile other than the active one needs its own base URL and its own key or account.
    """

    canonical = canonical_subagent_name(name)
    registry = load_subagent_registry(root=root)
    if canonical is None or canonical not in registry:
        raise ConfigError(f"Unknown subagent: {name}.")
    model_text = _clean(model, field="model")
    profile_text = _clean(profile, field="profile")
    profile_name: str | None = None
    if profile_text is not None:
        spec = get_profile(cfg, profile_text.lower())
        if spec is None:
            raise ConfigError(f"Profile not found: {profile_text}")
        profile_name = spec.name
    if model_text is not None or profile_name is not None:
        from .agent.subagent_execution import SubagentLauncher

        SubagentLauncher._resolve_subagent_config(
            replace(registry[canonical], model=model_text, profile=profile_name),
            cfg,
            api_key=resolve_api_key(cfg).key,
        )

    values = {"model": model_text, "profile": profile_name}
    path = _user_files_by_name().get(canonical)
    if path is None:
        if canonical not in built_in_subagents():
            raise ConfigError(
                f"{canonical} is defined by this project. "
                f"Set its model in .alysis_agents/{canonical}.md."
            )
        path = user_agents_directory() / f"{canonical}.md"
        if model_text is not None or profile_name is not None:
            lines = _with_values(f"name: {canonical}", values)
            atomic_write_text(path, "---\n" + "\n".join(lines) + "\n---\n")
    else:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(text)
        if frontmatter is None:
            raise ConfigError(f"{path} has no frontmatter. Change its model in the file.")
        lines = _with_values(frontmatter, values)
        if not body.strip() and not _sets_anything(lines):
            # Nothing left but the name: the built-in applies again.
            path.unlink()
        else:
            tail = body + ("\n" if body and text.endswith(("\n", "\r")) else "")
            atomic_write_text(path, "---\n" + "\n".join(lines) + "\n---\n" + tail)

    catalog = subagent_catalog(cfg, root=root)
    entry = next(item for item in catalog["agents"] if item["name"] == canonical)
    return {"agent": entry, "path": str(path), "changed": True}
