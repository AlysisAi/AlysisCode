from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, ConfigDict

from alysis_code import __version__

from ..atomic_io import atomic_write_json, atomic_write_text
from ..custom_tools.discovery import global_custom_tools_root, project_custom_tools_root
from ..custom_tools.trust import (
    ProjectToolTrustKey,
    ProjectToolTrustState,
)
from ..custom_tools.trust import (
    load_trust_state as load_tool_trust_state,
)
from ..custom_tools.trust import (
    save_trust_state as save_tool_trust_state,
)
from ..hooks.config import project_hooks_config_path, user_hooks_config_path
from ..hooks.models import HookConfigFile
from ..hooks.trust import (
    ProjectHooksTrustKey,
    ProjectHooksTrustState,
)
from ..hooks.trust import (
    load_trust_state as load_hooks_trust_state,
)
from ..hooks.trust import (
    save_trust_state as save_hooks_trust_state,
)
from ..installer_git import run_installer_git
from ..mcp.config import _load_user_mcp_config, user_mcp_config_path
from ..mcp.models import UserMcpConfigFile
from ..skills.install import install_skill_bundle, remove_managed_skill
from .bundled import bundled_manifest, parse_bundled_manifest
from .manifest import (
    CUSTOM_PLUGIN_PREFIX,
    PluginManifest,
    PluginManifestError,
    Security,
    load_manifest,
    resolve_manifest_path,
)
from .models import (
    ExtensionState,
    InstalledExtensionState,
    ProjectExtensionOverrides,
    normalize_extension_id,
    plugin_slug_from_id,
)
from .paths import installed_plugin_root, project_extensions_path
from .registry import find_by_id, load_registry
from .state import load_global_state, load_project_state, save_global_state, save_project_state

Scope = Literal["user", "project"]
RollbackFn = Callable[[], None]
TrustPromptFn = Callable[["TrustPromptRequest"], bool]

_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_-]*$")
_SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_UNSAFE_PATH_CHARS_RE = re.compile(r"[^a-z0-9._-]+")
_PLUGIN_MANAGED_TOOLS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ComponentInstallSummary:
    skill_ids: tuple[str, ...]
    tool_ids: tuple[str, ...]
    mcp_server_ids: tuple[str, ...]
    hook_ids: tuple[str, ...]


@dataclass(frozen=True)
class PluginInstallResult:
    plugin_id: str
    version: str
    commit: str
    manifest_sha256: str
    scope: Scope
    components_installed: ComponentInstallSummary
    trust_was_prompted: bool
    source_kind: str = "git"
    package_sha256: str | None = None
    upstream_commit: str | None = None


@dataclass(frozen=True)
class PluginUninstallResult:
    plugin_id: str
    scope: Scope
    components_removed: ComponentInstallSummary


@dataclass(frozen=True)
class EnableResult:
    plugin_id: str
    scope: Scope
    previous_state: Literal["enabled", "disabled", "absent"]
    new_state: Literal["enabled", "disabled"]
    no_op: bool


class PluginInstallError(RuntimeError):
    """Raised when install fails and rollback was performed."""

    def __init__(self, message: str, *, rollback_errors: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.rollback_errors = rollback_errors


class PermissionsSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    network: bool
    filesystem_write: bool
    required_env: tuple[str, ...]
    mcp_scopes: tuple[str, ...]
    hook_events: tuple[str, ...]
    host_managed_mcp_servers: tuple[str, ...] = ()


class TrustPromptRequest(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    plugin_id: str
    plugin_name: str
    version: str
    description: str
    source_url: str
    commit: str
    manifest_sha256: str
    components: ComponentInstallSummary
    permissions_summary: PermissionsSummary
    security: Security | None
    is_reinstall_with_new_commit: bool
    source_kind: str = "git"
    package_sha256: str | None = None
    upstream_commit: str | None = None


@dataclass(frozen=True)
class _ResolvedSource:
    display_url: str
    git_url: str
    commit: str
    bundled_content: bytes | None = None
    package_sha256: str | None = None
    upstream_commit: str | None = None
    registry_id: str | None = None
    #: The user's own online plugin (custom.<name>), whose descriptor the desktop app supplied.
    custom_id: str | None = None
    snapshot_path: Path | None = None

    @property
    def source_kind(self) -> str:
        if self.snapshot_path is not None:
            return "snapshot"
        if self.custom_id is not None:
            return "custom"
        return "bundled" if self.bundled_content is not None else "git"


@dataclass(frozen=True)
class _ToolRecord:
    plugin_id: str
    tool_id: str
    scope: Scope
    relative_path: str
    file_hash: str


@dataclass(frozen=True)
class _PluginManagedToolsState:
    tools: tuple[_ToolRecord, ...] = ()


def install_plugin(
    *,
    source: str,
    repo_root: Path,
    project: bool = False,
    trust_prompt: TrustPromptFn,
    user_config_dir: Path | None = None,
    custom_descriptor: bytes | None = None,
    reviewed_snapshot: Path | None = None,
    snapshot_digest: str | None = None,
) -> PluginInstallResult:
    scope: Scope = "project" if project else "user"
    resolved_repo_root = repo_root.expanduser().resolve()
    # Only the reviewed import service supplies a private, immutable materialization.
    # The public ext.install RPC never accepts this path or a caller-provided digest.
    if reviewed_snapshot is not None:
        if not snapshot_digest or custom_descriptor is not None:
            raise PluginInstallError("invalid reviewed package snapshot")
        source_ref = _ResolvedSource(
            display_url=source,
            git_url="",
            commit=f"sha256:{snapshot_digest}",
            snapshot_path=reviewed_snapshot,
            package_sha256=snapshot_digest,
        )
    else:
        source_ref = _resolve_source(source, custom_descriptor=custom_descriptor)
    if source_ref.custom_id is not None and project:
        raise PluginInstallError("a custom plugin is installed for the user, not a project")

    with tempfile.TemporaryDirectory(prefix="alysis-plugin-") as temp_dir:
        staging_root = Path(temp_dir)
        if source_ref.snapshot_path is not None:
            shutil.copytree(source_ref.snapshot_path, staging_root, dirs_exist_ok=True)
        elif source_ref.bundled_content is not None:
            (staging_root / "alysis-plugin.toml").write_bytes(source_ref.bundled_content)
        else:
            _clone_pinned_git_repo(
                git_url=source_ref.git_url,
                commit=source_ref.commit,
                destination=staging_root,
            )
        try:
            manifest = load_manifest(staging_root)
        except PluginManifestError as exc:
            raise PluginInstallError(f"manifest validation failed: {exc}") from exc

        _check_compatibility(manifest)
        _check_plugin_identity(manifest.plugin.id, source_ref, repo_root=resolved_repo_root)
        _check_project_mcp_scope(manifest, source_ref, project=project)
        _check_verified_connector_source(manifest, source_ref, project=project)
        # Hash whichever manifest was actually loaded — a legacy-named one still
        # has to produce a stable integrity hash for the install record.
        manifest_sha256 = _sha256_file(resolve_manifest_path(staging_root))
        component_summary = _component_summary(manifest)
        current_state = _load_extension_state(scope=scope, repo_root=resolved_repo_root)
        existing_record = current_state.installed.get(manifest.plugin.id)
        if (
            existing_record is not None
            and existing_record.commit == source_ref.commit
            and existing_record.manifest_sha256 == manifest_sha256
        ):
            return PluginInstallResult(
                plugin_id=manifest.plugin.id,
                version=existing_record.version or manifest.plugin.version,
                commit=source_ref.commit,
                manifest_sha256=manifest_sha256,
                scope=scope,
                components_installed=_summary_from_record(existing_record),
                trust_was_prompted=False,
                source_kind=source_ref.source_kind,
                package_sha256=source_ref.package_sha256,
                upstream_commit=source_ref.upstream_commit,
            )

        request = TrustPromptRequest(
            plugin_id=manifest.plugin.id,
            plugin_name=manifest.plugin.name,
            version=manifest.plugin.version,
            description=manifest.plugin.description,
            source_url=source_ref.display_url,
            commit=source_ref.commit,
            manifest_sha256=manifest_sha256,
            components=component_summary,
            permissions_summary=_permissions_summary(manifest),
            security=manifest.security,
            is_reinstall_with_new_commit=(
                existing_record is not None and existing_record.commit != source_ref.commit
            ),
            source_kind=source_ref.source_kind,
            package_sha256=source_ref.package_sha256,
            upstream_commit=source_ref.upstream_commit,
        )
        if not trust_prompt(request):
            raise PluginInstallError("install rejected by user")

        rollback: list[RollbackFn] = []
        cleanup_after_success: list[RollbackFn] = []
        try:
            plugin_root = installed_plugin_root(
                manifest.plugin.id,
                project=project,
                repo_root=resolved_repo_root,
            )
            _install_plugin_tree(
                source_root=staging_root,
                target_root=plugin_root,
                rollback=rollback,
                cleanup_after_success=cleanup_after_success,
            )
            installed_skills = _install_skills(
                manifest=manifest,
                staging_root=staging_root,
                repo_root=resolved_repo_root,
                project=project,
                user_config_dir=user_config_dir,
                force=existing_record is not None,
                rollback=rollback,
            )
            installed_tools = _install_tools(
                manifest=manifest,
                staging_root=staging_root,
                repo_root=resolved_repo_root,
                project=project,
                user_config_dir=user_config_dir,
                rollback=rollback,
            )
            installed_mcp_servers = _install_mcp_servers(
                manifest=manifest,
                rollback=rollback,
                user_config_dir=user_config_dir,
            )
            installed_hooks = _install_hooks(
                manifest=manifest,
                staging_root=staging_root,
                repo_root=resolved_repo_root,
                project=project,
                user_config_dir=user_config_dir,
                rollback=rollback,
            )
            installed_summary = ComponentInstallSummary(
                skill_ids=tuple(installed_skills),
                tool_ids=tuple(installed_tools),
                mcp_server_ids=tuple(installed_mcp_servers),
                hook_ids=tuple(installed_hooks),
            )
            updated_state = _state_with_installed_record(
                current_state,
                plugin_id=manifest.plugin.id,
                version=manifest.plugin.version,
                commit=source_ref.commit,
                source_url=source_ref.display_url,
                manifest_sha256=manifest_sha256,
                scope=scope,
                summary=installed_summary,
                source_kind=source_ref.source_kind,
                package_sha256=source_ref.package_sha256,
                upstream_commit=source_ref.upstream_commit,
            )
            _save_extension_state(scope=scope, repo_root=resolved_repo_root, state=updated_state)
        except Exception as exc:  # noqa: BLE001
            rollback_errors = _run_rollback(rollback)
            if isinstance(exc, PluginInstallError):
                raise PluginInstallError(str(exc), rollback_errors=rollback_errors) from exc
            raise PluginInstallError(str(exc), rollback_errors=rollback_errors) from exc

        for cleanup in reversed(cleanup_after_success):
            cleanup()

        return PluginInstallResult(
            plugin_id=manifest.plugin.id,
            version=manifest.plugin.version,
            commit=source_ref.commit,
            manifest_sha256=manifest_sha256,
            scope=scope,
            components_installed=installed_summary,
            trust_was_prompted=True,
            source_kind=source_ref.source_kind,
            package_sha256=source_ref.package_sha256,
            upstream_commit=source_ref.upstream_commit,
        )


def uninstall_plugin(
    *,
    plugin_id: str,
    repo_root: Path,
    project: bool = False,
    user_config_dir: Path | None = None,
) -> PluginUninstallResult:
    scope: Scope = "project" if project else "user"
    resolved_repo_root = repo_root.expanduser().resolve()
    state = _load_extension_state(scope=scope, repo_root=resolved_repo_root)
    record = state.installed.get(plugin_id)
    if record is None:
        raise PluginInstallError(f"plugin not installed in {scope}: {plugin_id}")

    summary = _summary_from_record(record)
    errors: list[str] = []
    for hook_id in reversed(summary.hook_ids):
        _collect_remove_error(
            errors,
            "hook",
            hook_id,
            lambda item=hook_id: _remove_hook_component(
                plugin_id=plugin_id,
                hook_id=item,
                repo_root=resolved_repo_root,
                project=project,
                user_config_dir=user_config_dir,
            ),
        )
    for server_id in reversed(summary.mcp_server_ids):
        _collect_remove_error(
            errors,
            "mcp server",
            server_id,
            lambda item=server_id: _remove_mcp_server_component(
                plugin_id=plugin_id,
                server_id=item,
                user_config_dir=user_config_dir,
            ),
        )
    for tool_id in reversed(summary.tool_ids):
        _collect_remove_error(
            errors,
            "tool",
            tool_id,
            lambda item=tool_id: _remove_tool_component(
                plugin_id=plugin_id,
                tool_id=item,
                repo_root=resolved_repo_root,
                project=project,
                user_config_dir=user_config_dir,
            ),
        )
    for skill_id in reversed(summary.skill_ids):
        _collect_remove_error(
            errors,
            "skill",
            skill_id,
            lambda item=skill_id: remove_managed_skill(
                name=item,
                workspace_root=resolved_repo_root,
                project=project,
                user_config_dir=user_config_dir,
            ),
        )

    plugin_root = installed_plugin_root(plugin_id, project=project, repo_root=resolved_repo_root)
    try:
        _remove_path(plugin_root)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"plugin root {plugin_root}: {exc}")

    updated_state = state.model_copy(deep=True)
    updated_state.installed.pop(plugin_id, None)
    updated_state.enabled = _without_normalized_id(updated_state.enabled, plugin_id)
    _save_extension_state(scope=scope, repo_root=resolved_repo_root, state=updated_state)

    if errors:
        raise PluginInstallError(
            f"uninstall completed with errors: {len(errors)} components failed to remove cleanly",
            rollback_errors=tuple(errors),
        )

    return PluginUninstallResult(
        plugin_id=plugin_id,
        scope=scope,
        components_removed=summary,
    )


def enable_plugin(
    *,
    plugin_id: str,
    repo_root: Path,
    project: bool = False,
    user_config_dir: Path | None = None,
) -> EnableResult:
    _ = user_config_dir
    return _set_plugin_enabled(
        plugin_id=plugin_id, repo_root=repo_root, project=project, enabled=True
    )


def disable_plugin(
    *,
    plugin_id: str,
    repo_root: Path,
    project: bool = False,
    user_config_dir: Path | None = None,
) -> EnableResult:
    _ = user_config_dir
    return _set_plugin_enabled(
        plugin_id=plugin_id,
        repo_root=repo_root,
        project=project,
        enabled=False,
    )


def _set_plugin_enabled(
    *,
    plugin_id: str,
    repo_root: Path,
    project: bool,
    enabled: bool,
) -> EnableResult:
    normalized = normalize_extension_id(plugin_id)
    if not normalized:
        raise PluginInstallError("plugin id cannot be empty")
    resolved_repo_root = repo_root.expanduser().resolve()
    if not _is_installed_any_scope(normalized, resolved_repo_root):
        if project:
            raise PluginInstallError("plugin not installed; run `alysis ext install <id>` first")
        raise PluginInstallError(f"plugin not installed: {plugin_id}")
    if project:
        return _set_project_override_enabled(
            plugin_id=normalized,
            repo_root=resolved_repo_root,
            enabled=enabled,
        )
    return _set_user_enabled(plugin_id=normalized, repo_root=resolved_repo_root, enabled=enabled)


def _is_installed_any_scope(plugin_id: str, repo_root: Path) -> bool:
    return _state_has_installed_plugin(
        load_global_state(), plugin_id
    ) or _state_has_installed_plugin(load_project_state(repo_root), plugin_id)


def _state_has_installed_plugin(state: ExtensionState, plugin_id: str) -> bool:
    return any(normalize_extension_id(ext_id) == plugin_id for ext_id in state.installed)


def _set_user_enabled(*, plugin_id: str, repo_root: Path, enabled: bool) -> EnableResult:
    state = load_global_state()
    record_key = _installed_record_key(state, plugin_id)
    normalized_enabled = _normalized_id_set(state.enabled)
    record_enabled = bool(record_key and state.installed[record_key].enabled)
    previous_state: Literal["enabled", "disabled", "absent"]
    if plugin_id in normalized_enabled or record_enabled:
        previous_state = "enabled"
    elif record_key is not None:
        previous_state = "disabled"
    else:
        previous_state = "absent"

    target_state: Literal["enabled", "disabled"] = "enabled" if enabled else "disabled"
    if previous_state == target_state:
        return EnableResult(
            plugin_id=plugin_id,
            scope="user",
            previous_state=previous_state,
            new_state=target_state,
            no_op=True,
        )

    updated = state.model_copy(deep=True)
    if enabled:
        updated.enabled = _with_normalized_id(updated.enabled, plugin_id)
        if record_key is not None:
            updated.installed[record_key].enabled = True
    else:
        updated.enabled = _without_normalized_id(updated.enabled, plugin_id)
        if record_key is not None:
            updated.installed[record_key].enabled = False
    _save_extension_state(scope="user", repo_root=repo_root, state=updated)
    return EnableResult(
        plugin_id=plugin_id,
        scope="user",
        previous_state=previous_state,
        new_state=target_state,
        no_op=False,
    )


def _set_project_override_enabled(
    *,
    plugin_id: str,
    repo_root: Path,
    enabled: bool,
) -> EnableResult:
    path = project_extensions_path(repo_root)
    raw = _read_project_extensions_raw(path)
    overrides = ProjectExtensionOverrides.model_validate(raw)
    current_enabled = _normalized_id_set(overrides.enabled)
    current_disabled = _normalized_id_set(overrides.disabled)
    if plugin_id in current_enabled:
        previous_state: Literal["enabled", "disabled", "absent"] = "enabled"
    elif plugin_id in current_disabled:
        previous_state = "disabled"
    else:
        previous_state = "absent"

    target_state: Literal["enabled", "disabled"] = "enabled" if enabled else "disabled"
    if previous_state == target_state:
        return EnableResult(
            plugin_id=plugin_id,
            scope="project",
            previous_state=previous_state,
            new_state=target_state,
            no_op=True,
        )

    raw["enabled"] = (
        _with_normalized_id(overrides.enabled, plugin_id)
        if enabled
        else _without_normalized_id(overrides.enabled, plugin_id)
    )
    raw["disabled"] = (
        _without_normalized_id(overrides.disabled, plugin_id)
        if enabled
        else _with_normalized_id(overrides.disabled, plugin_id)
    )
    raw.setdefault("schema_version", 1)
    atomic_write_json(path, raw)
    return EnableResult(
        plugin_id=plugin_id,
        scope="project",
        previous_state=previous_state,
        new_state=target_state,
        no_op=False,
    )


def _read_project_extensions_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"Failed to read {path}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"Invalid JSON format in {path}: expected object.")
    return raw


def _installed_record_key(state: ExtensionState, plugin_id: str) -> str | None:
    for ext_id in state.installed:
        if normalize_extension_id(ext_id) == plugin_id:
            return ext_id
    return None


def _normalized_id_set(values: list[str]) -> set[str]:
    return {normalized for value in values if (normalized := normalize_extension_id(value))}


def _with_normalized_id(values: list[str], plugin_id: str) -> list[str]:
    return sorted({*_normalized_id_set(values), plugin_id})


def _without_normalized_id(values: list[str], plugin_id: str) -> list[str]:
    return sorted(value for value in _normalized_id_set(values) if value != plugin_id)


_FIRST_PARTY_SLUG_PREFIX = "alysis-"


def _check_plugin_identity(plugin_id: str, source_ref: _ResolvedSource, *, repo_root: Path) -> None:
    """A plugin may not claim another plugin's id, install folder, or the first-party namespace."""
    normalized = normalize_extension_id(plugin_id)
    # Custom descriptors use bundled_content as their materialization buffer;
    # that does not make them bundled publishers. They cannot also be a catalog
    # or Git source, and must retain the descriptor supplied by the custom route.
    if source_ref.custom_id is not None and (
        source_ref.registry_id is not None
        or source_ref.git_url
        or source_ref.bundled_content is None
    ):
        raise PluginInstallError("plugin source identity is inconsistent")
    if (
        source_ref.registry_id is not None
        and normalize_extension_id(source_ref.registry_id) != normalized
    ):
        raise PluginInstallError(
            f"catalog entry {source_ref.registry_id} contains a different plugin id: {plugin_id}"
        )
    if source_ref.custom_id is not None:
        if normalized != normalize_extension_id(source_ref.custom_id):
            raise PluginInstallError(
                f"custom descriptor {source_ref.custom_id} contains a different plugin id: "
                f"{plugin_id}"
            )
    elif normalized.startswith(CUSTOM_PLUGIN_PREFIX):
        raise PluginInstallError(f"plugin id {plugin_id} is reserved for the user's own plugins")
    slug = plugin_slug_from_id(normalized)
    if source_ref.source_kind != "bundled" and slug.startswith(_FIRST_PARTY_SLUG_PREFIX):
        raise PluginInstallError(
            f"plugin id {plugin_id} is reserved for plugins bundled with Alysis Code"
        )
    known = {normalize_extension_id(entry.id) for entry in load_registry().extensions}
    for state in _states_for_identity_check(repo_root):
        for raw_id, record in state.installed.items():
            installed_id = normalize_extension_id(record.id or raw_id)
            known.add(installed_id)
            # The model deliberately defaults pre-source-kind records to git.
            if installed_id == normalized and record.source_kind != source_ref.source_kind:
                raise PluginInstallError("plugin source kind conflicts with installed identity")
    for other in sorted(known - {normalized}):
        if plugin_slug_from_id(other) == slug:
            raise PluginInstallError(
                f"plugin id {plugin_id} collides with {other}; both would use the folder {slug}"
            )


def _check_project_mcp_scope(
    manifest: PluginManifest, source_ref: _ResolvedSource, *, project: bool
) -> None:
    """Preflight only; the early install caller is a separately owned handoff.

    A project direct definition currently writes the user MCP file. Refuse that
    scope crossing; only the exact content-pinned bundled host declaration is
    exempt. A manifest's host_managed flag alone is not provenance.
    """
    if not project:
        return
    enabled = [server for server in manifest.components.mcp_server if server.enabled]
    if not enabled:
        return
    error = "project plugin MCP definitions are not supported"
    if any(not server.host_managed for server in enabled):
        raise PluginInstallError(error)
    if (
        source_ref.custom_id is not None
        or source_ref.git_url
        or source_ref.registry_id is None
        or source_ref.bundled_content is None
    ):
        raise PluginInstallError(error)
    try:
        entry = find_by_id(load_registry(), source_ref.registry_id)
        if (
            entry is None
            or entry.source_kind != "bundled"
            or entry.id != manifest.plugin.id
            or entry.commit != source_ref.commit
            or entry.package_sha256 != source_ref.package_sha256
        ):
            raise PluginInstallError(error)
        verified_content = bundled_manifest(entry)
        if (
            verified_content != source_ref.bundled_content
            or parse_bundled_manifest(verified_content) != manifest
        ):
            raise PluginInstallError(error)
    except (OSError, RuntimeError, ValueError):
        raise PluginInstallError(error) from None


def _check_verified_connector_source(
    manifest: PluginManifest, source_ref: _ResolvedSource, *, project: bool
) -> None:
    """A verified connector comes only from its content-pinned bundled catalog entry.

    Alysis reviewed its tool list and read marks, so no other source may carry one; it is
    bound for the user only, like the user's own online plugins.
    """
    if not manifest.components.verified_connector:
        return
    error = "a verified connector installs only from its built-in catalog entry"
    if project:
        raise PluginInstallError("a verified connector is installed for the user, not a project")
    if (
        source_ref.custom_id is not None
        or source_ref.git_url
        or source_ref.registry_id is None
        or source_ref.bundled_content is None
    ):
        raise PluginInstallError(error)
    try:
        entry = find_by_id(load_registry(), source_ref.registry_id)
        if (
            entry is None
            or entry.source_kind != "bundled"
            or entry.id != manifest.plugin.id
            or entry.commit != source_ref.commit
            or entry.package_sha256 != source_ref.package_sha256
            or bundled_manifest(entry) != source_ref.bundled_content
        ):
            raise PluginInstallError(error)
    except (OSError, RuntimeError, ValueError):
        raise PluginInstallError(error) from None


def _states_for_identity_check(repo_root: Path) -> list[ExtensionState]:
    # Missing state is handled by the loaders. Malformed or unreadable state
    # cannot prove that another plugin does not own the same install identity.
    return [load_global_state(), load_project_state(repo_root)]


def _resolve_source(source: str, *, custom_descriptor: bytes | None = None) -> _ResolvedSource:
    raw = str(source or "").strip()
    if raw.startswith("custom:") or custom_descriptor is not None:
        plugin_id = raw.removeprefix("custom:")
        if (
            not raw.startswith("custom:")
            or custom_descriptor is None
            or not plugin_id.startswith(CUSTOM_PLUGIN_PREFIX)
        ):
            raise PluginInstallError(f"unsupported install source: {raw}")
        digest = hashlib.sha256(custom_descriptor).hexdigest()
        return _ResolvedSource(
            display_url=f"custom://{plugin_id}/sha256/{digest}",
            git_url="",
            commit=f"sha256:{digest}",
            bundled_content=custom_descriptor,
            package_sha256=digest,
            custom_id=plugin_id,
        )
    if _SOURCE_ID_RE.fullmatch(raw):
        registry = load_registry()
        entry = find_by_id(registry, raw)
        if entry is None:
            raise PluginInstallError(f"registry id not found: {raw}")
        if entry.source_kind == "bundled":
            content = bundled_manifest(entry)
            return _ResolvedSource(
                display_url=f"bundled://{entry.id}/sha256/{entry.package_sha256}",
                git_url="",
                commit=entry.commit,
                bundled_content=content,
                package_sha256=entry.package_sha256,
                upstream_commit=entry.upstream_commit,
                registry_id=entry.id,
            )
        if not _SHA40_RE.fullmatch(entry.commit or ""):
            raise PluginInstallError(f"registry entry has invalid commit for {raw}: {entry.commit}")
        return _ResolvedSource(
            display_url=f"{entry.repo}@{entry.commit}",
            git_url=entry.repo,
            commit=entry.commit.lower(),
            registry_id=entry.id,
        )

    if raw.startswith("git+https://"):
        base, marker, commit = raw.rpartition("@")
        if not marker or not _SHA40_RE.fullmatch(commit):
            raise PluginInstallError(f"unsupported install source: {raw}")
        return _ResolvedSource(
            display_url=raw,
            git_url=base.removeprefix("git+"),
            commit=commit.lower(),
        )

    split = urlsplit(raw)
    if split.scheme == "https" and split.netloc and split.fragment:
        commit = split.fragment
        if not _SHA40_RE.fullmatch(commit):
            raise PluginInstallError(f"unsupported install source: {raw}")
        git_url = urlunsplit((split.scheme, split.netloc, split.path, split.query, ""))
        return _ResolvedSource(display_url=raw, git_url=git_url, commit=commit.lower())

    raise PluginInstallError(f"unsupported install source: {raw}")


def _clone_pinned_git_repo(*, git_url: str, commit: str, destination: Path) -> None:
    _run_git(["git", "init", os.fspath(destination)], cwd=None)
    _run_git(["git", "remote", "add", "origin", git_url], cwd=destination)
    _run_git(["git", "fetch", "--depth", "1", "origin", commit], cwd=destination)
    _run_git(["git", "checkout", "FETCH_HEAD"], cwd=destination)
    result = _run_git(["git", "rev-parse", "HEAD"], cwd=destination)
    actual = result.stdout.strip().lower()
    if actual != commit.lower():
        raise PluginInstallError(
            f"git checkout mismatch: expected {commit.lower()}, got {actual or '<empty>'}"
        )


def _run_git(args: list[str], *, cwd: Path | None) -> subprocess.CompletedProcess[str]:
    try:
        return run_installer_git(args, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise PluginInstallError(f"git command timed out after 120s: {' '.join(args)}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = str(exc.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise PluginInstallError(f"git command failed: {' '.join(args)}{detail}") from exc


def _check_compatibility(manifest: PluginManifest) -> None:
    specifier = SpecifierSet(manifest.compatibility.alysis)
    running = Version(__version__)
    if running not in specifier:
        raise PluginInstallError(
            "plugin is not compatible with this Alysis Code version: "
            f"running {running}, requires {manifest.compatibility.alysis}"
        )
    platform = _current_platform()
    if platform not in set(manifest.compatibility.platforms):
        allowed = ", ".join(manifest.compatibility.platforms)
        raise PluginInstallError(
            f"plugin is not compatible with this platform: running {platform}, supports {allowed}"
        )


def _current_platform() -> Literal["linux", "darwin", "windows"]:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith(("win", "cygwin", "msys")):
        return "windows"
    return "linux"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _component_summary(manifest: PluginManifest) -> ComponentInstallSummary:
    return ComponentInstallSummary(
        skill_ids=tuple(_effective_id(item.id, item.path) for item in manifest.components.skill),
        tool_ids=tuple(_effective_id(item.id, item.path) for item in manifest.components.tool),
        mcp_server_ids=tuple(item.id for item in manifest.components.mcp_server),
        hook_ids=tuple(_effective_id(item.id, item.path) for item in manifest.components.hook),
    )


def _permissions_summary(manifest: PluginManifest) -> PermissionsSummary:
    required_env = {
        env_name for tool in manifest.components.tool for env_name in tool.required_env
    } | {env_name for server in manifest.components.mcp_server for env_name in server.env}
    return PermissionsSummary(
        network=bool(manifest.components.mcp_server)
        or bool(manifest.components.verified_connector)
        or any(tool.network for tool in manifest.components.tool),
        filesystem_write=any(tool.filesystem == "write" for tool in manifest.components.tool),
        required_env=tuple(sorted(required_env)),
        mcp_scopes=tuple(
            sorted({scope for server in manifest.components.mcp_server for scope in server.scopes})
        ),
        hook_events=tuple(sorted({hook.event for hook in manifest.components.hook})),
        host_managed_mcp_servers=tuple(
            server.id for server in manifest.components.mcp_server if server.host_managed
        ),
    )


def _effective_id(component_id: str | None, path: str) -> str:
    return str(component_id or Path(path).name)


def _summary_from_record(record: InstalledExtensionState) -> ComponentInstallSummary:
    ids = record.component_ids or {}
    return ComponentInstallSummary(
        skill_ids=tuple(str(item) for item in ids.get("skill", [])),
        tool_ids=tuple(str(item) for item in ids.get("tool", [])),
        mcp_server_ids=tuple(str(item) for item in ids.get("mcp_server", [])),
        hook_ids=tuple(str(item) for item in ids.get("hook", [])),
    )


def _load_extension_state(*, scope: Scope, repo_root: Path) -> ExtensionState:
    return load_project_state(repo_root) if scope == "project" else load_global_state()


def _save_extension_state(*, scope: Scope, repo_root: Path, state: ExtensionState) -> None:
    if scope == "project":
        save_project_state(repo_root, state)
        return
    save_global_state(state)


def _state_with_installed_record(
    state: ExtensionState,
    *,
    plugin_id: str,
    version: str,
    commit: str,
    source_url: str,
    manifest_sha256: str,
    scope: Scope,
    summary: ComponentInstallSummary,
    source_kind: str = "git",
    package_sha256: str | None = None,
    upstream_commit: str | None = None,
) -> ExtensionState:
    previous = state.installed.get(plugin_id)
    # Installing over an existing record (an update) keeps a turned-off plugin off.
    enabled = previous is None or previous.enabled or plugin_id in _normalized_id_set(state.enabled)
    updated = state.model_copy(deep=True)
    updated.installed[plugin_id] = InstalledExtensionState(
        id=plugin_id,
        version=version,
        commit=commit,
        source=source_url,
        source_url=source_url,
        trust="manifest-sha256",
        enabled=enabled,
        manifest_sha256=manifest_sha256,
        installed_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        scope=scope,
        component_ids={
            "skill": list(summary.skill_ids),
            "tool": list(summary.tool_ids),
            "mcp_server": list(summary.mcp_server_ids),
            "hook": list(summary.hook_ids),
        },
        source_kind=source_kind,
        package_sha256=package_sha256,
        upstream_commit=upstream_commit,
    )
    updated.enabled = (
        _with_normalized_id(updated.enabled, plugin_id)
        if enabled
        else _without_normalized_id(updated.enabled, plugin_id)
    )
    return updated


def _install_plugin_tree(
    *,
    source_root: Path,
    target_root: Path,
    rollback: list[RollbackFn],
    cleanup_after_success: list[RollbackFn],
) -> None:
    target_root.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if target_root.exists() or target_root.is_symlink():
        backup_path = target_root.parent / f".{target_root.name}.backup-{os.getpid()}"
        if backup_path.exists():
            _remove_path(backup_path)
        os.replace(target_root, backup_path)

    def rollback_plugin_tree() -> None:
        _remove_path(target_root)
        if backup_path is not None and (backup_path.exists() or backup_path.is_symlink()):
            os.replace(backup_path, target_root)

    rollback.append(rollback_plugin_tree)
    shutil.copytree(source_root, target_root, ignore=shutil.ignore_patterns(".git"))
    if backup_path is not None:
        cleanup_after_success.append(lambda path=backup_path: _remove_path(path))


def _install_skills(
    *,
    manifest: PluginManifest,
    staging_root: Path,
    repo_root: Path,
    project: bool,
    user_config_dir: Path | None,
    force: bool,
    rollback: list[RollbackFn],
) -> list[str]:
    installed: list[str] = []
    for component in manifest.components.skill:
        if not component.enabled:
            continue
        result = install_skill_bundle(
            source=os.fspath(staging_root / component.path),
            workspace_root=repo_root,
            project=project,
            force=force,
            user_config_dir=user_config_dir,
        )
        installed.append(result.installed_name)
        rollback.append(
            lambda name=result.installed_name: remove_managed_skill(
                name=name,
                workspace_root=repo_root,
                project=project,
                user_config_dir=user_config_dir,
            )
        )
    return installed


def _install_tools(
    *,
    manifest: PluginManifest,
    staging_root: Path,
    repo_root: Path,
    project: bool,
    user_config_dir: Path | None,
    rollback: list[RollbackFn],
) -> list[str]:
    installed: list[str] = []
    tools_root = (
        project_custom_tools_root(repo_root)
        if project
        else global_custom_tools_root(user_config_dir=user_config_dir)
    )
    plugin_dir = tools_root / "plugins" / plugin_slug_from_id(manifest.plugin.id)
    for component in manifest.components.tool:
        if not component.enabled:
            continue
        tool_id = _effective_id(component.id, component.path)
        source_path = staging_root / component.path
        dest_path = plugin_dir / f"{_safe_component_slug(tool_id)}{source_path.suffix or '.py'}"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, dest_path)
        file_hash = _sha256_file(dest_path)
        record = _ToolRecord(
            plugin_id=manifest.plugin.id,
            tool_id=tool_id,
            scope="project" if project else "user",
            relative_path=dest_path.relative_to(tools_root).as_posix(),
            file_hash=file_hash,
        )
        _mutate_plugin_managed_tools_state(
            tools_root,
            lambda current, item=record: _with_tool_record(current, item),
        )
        trust_snapshot = load_tool_trust_state(user_config_dir=user_config_dir)
        if project:
            _trust_project_plugin_tool(
                repo_root=repo_root,
                dest_path=dest_path,
                file_hash=file_hash,
                user_config_dir=user_config_dir,
            )

        rollback.append(
            lambda item=record, path=dest_path, snapshot=trust_snapshot: _rollback_tool_install(
                tools_root=tools_root,
                record=item,
                path=path,
                project=project,
                user_config_dir=user_config_dir,
                trust_snapshot=snapshot,
            )
        )
        installed.append(tool_id)
    return installed


def _install_mcp_servers(
    *,
    manifest: PluginManifest,
    rollback: list[RollbackFn],
    user_config_dir: Path | None,
) -> list[str]:
    installed = [server.id for server in manifest.components.mcp_server if server.enabled]
    direct_servers = [
        server
        for server in manifest.components.mcp_server
        if server.enabled and not server.host_managed
    ]
    if not direct_servers:
        return installed
    path = _user_mcp_config_path(user_config_dir=user_config_dir)
    snapshot = _read_optional_text(path)
    config = _load_user_mcp_config(path) if path.exists() else UserMcpConfigFile()
    dumped = config.model_dump(mode="json", exclude_none=True)
    servers = dict(dumped.get("servers", {}))
    for server in direct_servers:
        key = _mcp_server_key(manifest.plugin.id, server.id)
        payload: dict[str, Any] = {
            "transport": server.transport,
            "enabled": True,
            "trust": "explicit",
        }
        if server.transport == "stdio":
            command = list(server.command or [])
            payload["command"] = command[0]
            payload["args"] = command[1:]
            if server.env:
                payload["env"] = {name: f"${{{name}}}" for name in server.env}
        else:
            payload["url"] = str(server.url)
            if server.oauth is not None:
                payload["oauth"] = server.oauth
        servers[key] = payload
    updated = UserMcpConfigFile.model_validate(
        {"schema_version": config.schema_version, "servers": servers}
    )
    _write_model_json(path, updated)
    rollback.append(lambda target=path, text=snapshot: _restore_optional_text(target, text))
    return installed


def _install_hooks(
    *,
    manifest: PluginManifest,
    staging_root: Path,
    repo_root: Path,
    project: bool,
    user_config_dir: Path | None,
    rollback: list[RollbackFn],
) -> list[str]:
    installed: list[str] = []
    enabled_hooks = [hook for hook in manifest.components.hook if hook.enabled]
    if not enabled_hooks:
        return installed

    # The destination keeps this manifest-controlled suffix. list2cmdline is
    # argv quoting, not cmd.exe escaping: metacharacters/expansions in a suffix
    # could become shell syntax on Windows. Preflight the whole batch before
    # copying any hooks, including when installing on another platform.
    for hook in enabled_hooks:
        suffix = Path(hook.path).suffix
        if suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix) is None:
            raise PluginInstallError("unsupported hook file suffix")

    config_path = _hooks_config_path(
        repo_root=repo_root,
        project=project,
        user_config_dir=user_config_dir,
    )
    hook_root = _hooks_component_root(
        manifest.plugin.id,
        repo_root=repo_root,
        project=project,
        user_config_dir=user_config_dir,
    )
    snapshot = _read_optional_text(config_path)
    raw_config = _read_json_config(config_path, default={"schema_version": 1, "hooks": {}})
    hooks_root = raw_config.setdefault("hooks", {})
    if not isinstance(hooks_root, dict):
        raise PluginInstallError(f"Invalid hooks config format: {config_path}")

    for component in enabled_hooks:
        hook_id = _effective_id(component.id, component.path)
        source_path = staging_root / component.path
        dest_path = hook_root / f"{_safe_component_slug(hook_id)}{source_path.suffix}"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, dest_path)
        command = _hook_command(dest_path)
        event = _hook_event_name(component.event)
        group = {
            "hooks": [
                {
                    "type": "command",
                    "id": _hook_config_id(manifest.plugin.id, hook_id),
                    "description": f"Plugin hook from {manifest.plugin.id}",
                    "command": command,
                    "enabled": True,
                }
            ]
        }
        hooks_root.setdefault(event, []).append(group)
        installed.append(hook_id)

    HookConfigFile.model_validate(raw_config)
    atomic_write_json(config_path, raw_config)
    # Plugin consent does not review the entire project hooks configuration.
    rollback.append(
        lambda target=config_path, text=snapshot, root=hook_root: (
            _restore_optional_text(target, text),
            _remove_path(root),
        )
    )
    return installed


def _remove_tool_component(
    *,
    plugin_id: str,
    tool_id: str,
    repo_root: Path,
    project: bool,
    user_config_dir: Path | None,
) -> None:
    tools_root = (
        project_custom_tools_root(repo_root)
        if project
        else global_custom_tools_root(user_config_dir=user_config_dir)
    )
    state = _load_plugin_managed_tools_state(tools_root)
    matching = [
        item for item in state.tools if item.plugin_id == plugin_id and item.tool_id == tool_id
    ]
    for record in matching:
        _remove_path(tools_root / record.relative_path)
        if project:
            _untrust_project_plugin_tool(
                repo_root=repo_root,
                relative_path=(tools_root / record.relative_path).relative_to(repo_root).as_posix(),
                user_config_dir=user_config_dir,
            )
    _mutate_plugin_managed_tools_state(
        tools_root,
        lambda current: _without_tool_record(current, plugin_id=plugin_id, tool_id=tool_id),
    )


def _remove_mcp_server_component(
    *,
    plugin_id: str,
    server_id: str,
    user_config_dir: Path | None,
) -> None:
    path = _user_mcp_config_path(user_config_dir=user_config_dir)
    if not path.exists():
        return
    config = _load_user_mcp_config(path)
    dumped = config.model_dump(mode="json", exclude_none=True)
    servers = dict(dumped.get("servers", {}))
    servers.pop(_mcp_server_key(plugin_id, server_id), None)
    updated = UserMcpConfigFile.model_validate(
        {"schema_version": config.schema_version, "servers": servers}
    )
    _write_model_json(path, updated)


def _remove_hook_component(
    *,
    plugin_id: str,
    hook_id: str,
    repo_root: Path,
    project: bool,
    user_config_dir: Path | None,
) -> None:
    config_path = _hooks_config_path(
        repo_root=repo_root,
        project=project,
        user_config_dir=user_config_dir,
    )
    if config_path.exists():
        raw = _read_json_config(config_path, default={"schema_version": 1, "hooks": {}})
        config_hook_id = _hook_config_id(plugin_id, hook_id)
        hooks_root = raw.get("hooks")
        if isinstance(hooks_root, dict):
            for event_name, groups in list(hooks_root.items()):
                if not isinstance(groups, list):
                    continue
                retained_groups: list[Any] = []
                for group in groups:
                    if not isinstance(group, dict):
                        retained_groups.append(group)
                        continue
                    hook_list = group.get("hooks")
                    if not isinstance(hook_list, list):
                        retained_groups.append(group)
                        continue
                    retained_hooks = [
                        hook
                        for hook in hook_list
                        if not (isinstance(hook, dict) and hook.get("id") == config_hook_id)
                    ]
                    if retained_hooks:
                        group["hooks"] = retained_hooks
                        retained_groups.append(group)
                if retained_groups:
                    hooks_root[event_name] = retained_groups
                else:
                    hooks_root.pop(event_name, None)
        HookConfigFile.model_validate(raw)
        atomic_write_json(config_path, raw)
        # Removal cannot grant trust to unrelated remaining project hooks.
    hook_root = _hooks_component_root(
        plugin_id,
        repo_root=repo_root,
        project=project,
        user_config_dir=user_config_dir,
    )
    for candidate in hook_root.glob(f"{_safe_component_slug(hook_id)}*"):
        _remove_path(candidate)


def _collect_remove_error(
    errors: list[str],
    kind: str,
    component_id: str,
    remove: Callable[[], object],
) -> None:
    try:
        remove()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{kind} {component_id}: {exc}")


def _run_rollback(rollback: list[RollbackFn]) -> tuple[str, ...]:
    errors: list[str] = []
    for action in reversed(rollback):
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    return tuple(errors)


def _plugin_managed_tools_state_path(tools_root: Path) -> Path:
    return tools_root / "plugin_managed_tools.json"


def _load_plugin_managed_tools_state(tools_root: Path) -> _PluginManagedToolsState:
    path = _plugin_managed_tools_state_path(tools_root)
    if not path.exists():
        return _PluginManagedToolsState()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PluginInstallError(f"Invalid plugin-managed tools state: {path}")
    if raw.get("schema_version", _PLUGIN_MANAGED_TOOLS_SCHEMA_VERSION) != (
        _PLUGIN_MANAGED_TOOLS_SCHEMA_VERSION
    ):
        raise PluginInstallError(f"Unsupported plugin-managed tools state schema: {path}")
    tools_raw = raw.get("tools", [])
    if not isinstance(tools_raw, list):
        raise PluginInstallError(f"Invalid plugin-managed tools state tools payload: {path}")
    records: list[_ToolRecord] = []
    for item in tools_raw:
        if not isinstance(item, dict):
            continue
        records.append(
            _ToolRecord(
                plugin_id=str(item.get("plugin_id") or ""),
                tool_id=str(item.get("tool_id") or ""),
                scope="project" if item.get("scope") == "project" else "user",
                relative_path=str(item.get("relative_path") or ""),
                file_hash=str(item.get("file_hash") or ""),
            )
        )
    return _PluginManagedToolsState(tools=tuple(record for record in records if record.plugin_id))


def _save_plugin_managed_tools_state(
    tools_root: Path,
    state: _PluginManagedToolsState,
) -> None:
    payload = {
        "schema_version": _PLUGIN_MANAGED_TOOLS_SCHEMA_VERSION,
        "tools": [
            {
                "plugin_id": record.plugin_id,
                "tool_id": record.tool_id,
                "scope": record.scope,
                "relative_path": record.relative_path,
                "file_hash": record.file_hash,
            }
            for record in sorted(
                state.tools,
                key=lambda item: (item.plugin_id, item.tool_id, item.relative_path),
            )
        ],
    }
    atomic_write_json(_plugin_managed_tools_state_path(tools_root), payload)


def _mutate_plugin_managed_tools_state(
    tools_root: Path,
    mutation: Callable[[_PluginManagedToolsState], _PluginManagedToolsState],
) -> None:
    current = _load_plugin_managed_tools_state(tools_root)
    updated = mutation(current)
    _save_plugin_managed_tools_state(tools_root, updated)


def _with_tool_record(
    state: _PluginManagedToolsState,
    record: _ToolRecord,
) -> _PluginManagedToolsState:
    retained = [
        item
        for item in state.tools
        if not (item.plugin_id == record.plugin_id and item.tool_id == record.tool_id)
    ]
    retained.append(record)
    return _PluginManagedToolsState(tools=tuple(retained))


def _without_tool_record(
    state: _PluginManagedToolsState,
    *,
    plugin_id: str,
    tool_id: str,
) -> _PluginManagedToolsState:
    return _PluginManagedToolsState(
        tools=tuple(
            item
            for item in state.tools
            if not (item.plugin_id == plugin_id and item.tool_id == tool_id)
        )
    )


def _rollback_tool_install(
    *,
    tools_root: Path,
    record: _ToolRecord,
    path: Path,
    project: bool,
    user_config_dir: Path | None,
    trust_snapshot: ProjectToolTrustState,
) -> None:
    _remove_path(path)
    _mutate_plugin_managed_tools_state(
        tools_root,
        lambda current: _without_tool_record(
            current,
            plugin_id=record.plugin_id,
            tool_id=record.tool_id,
        ),
    )
    if project:
        save_tool_trust_state(trust_snapshot, user_config_dir=user_config_dir)


def _trust_project_plugin_tool(
    *,
    repo_root: Path,
    dest_path: Path,
    file_hash: str,
    user_config_dir: Path | None,
) -> None:
    state = load_tool_trust_state(user_config_dir=user_config_dir)
    key = ProjectToolTrustKey(
        workspace_root=os.fspath(repo_root.resolve()),
        relative_tool_path=dest_path.relative_to(repo_root.resolve()).as_posix(),
        file_hash=file_hash,
    )
    updated = ProjectToolTrustState(
        trusted_tools=tuple(
            sorted(
                {*(state.trusted_tools), key},
                key=lambda item: (item.workspace_root, item.relative_tool_path, item.file_hash),
            )
        )
    )
    save_tool_trust_state(updated, user_config_dir=user_config_dir)


def _untrust_project_plugin_tool(
    *,
    repo_root: Path,
    relative_path: str,
    user_config_dir: Path | None,
) -> None:
    state = load_tool_trust_state(user_config_dir=user_config_dir)
    workspace = os.fspath(repo_root.resolve())
    updated = ProjectToolTrustState(
        trusted_tools=tuple(
            key
            for key in state.trusted_tools
            if not (key.workspace_root == workspace and key.relative_tool_path == relative_path)
        )
    )
    save_tool_trust_state(updated, user_config_dir=user_config_dir)


def _user_mcp_config_path(*, user_config_dir: Path | None) -> Path:
    if user_config_dir is not None:
        return user_config_dir.expanduser().resolve() / "mcp.json"
    return user_mcp_config_path()


def _mcp_server_key(plugin_id: str, server_id: str) -> str:
    return f"{plugin_id}/{server_id}".lower()


def _hooks_config_path(
    *,
    repo_root: Path,
    project: bool,
    user_config_dir: Path | None,
) -> Path:
    if project:
        return project_hooks_config_path(repo_root)
    if user_config_dir is not None:
        return user_config_dir.expanduser().resolve() / "hooks.json"
    return user_hooks_config_path()


def _hooks_component_root(
    plugin_id: str,
    *,
    repo_root: Path,
    project: bool,
    user_config_dir: Path | None,
) -> Path:
    base = (
        repo_root.resolve() / ".alysis"
        if project
        else (
            user_config_dir.expanduser().resolve()
            if user_config_dir is not None
            else user_hooks_config_path().parent
        )
    )
    return base / "hooks" / "plugins" / plugin_slug_from_id(plugin_id)


def _hook_event_name(event_name: str) -> str:
    if event_name == "SessionStop":
        return "SessionEnd"
    return event_name


def _hook_command(path: Path) -> str:
    args = [os.fspath(path)]
    if path.suffix.casefold() == ".py":
        args.insert(0, sys.executable)
    # The hook dispatcher runs this string through the platform shell. Windows
    # quoting leaves substitutions and quote characters active in a POSIX shell.
    if sys.platform == "win32":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _hook_config_id(plugin_id: str, hook_id: str) -> str:
    return f"{plugin_slug_from_id(plugin_id)}.{_safe_component_slug(hook_id)}"


def _trust_project_hooks_config(
    *,
    workspace_root: Path,
    config_path: Path,
    user_config_dir: Path | None,
) -> None:
    state = load_hooks_trust_state(user_config_dir=user_config_dir)
    key = ProjectHooksTrustKey(
        workspace_root=os.fspath(workspace_root.resolve()),
        relative_config_path=config_path.resolve().relative_to(workspace_root.resolve()).as_posix(),
        file_hash=_sha256_file(config_path),
    )
    updated = ProjectHooksTrustState(
        trusted_configs=tuple(
            sorted(
                {*(state.trusted_configs), key},
                key=lambda item: (item.workspace_root, item.relative_config_path, item.file_hash),
            )
        )
    )
    save_hooks_trust_state(updated, user_config_dir=user_config_dir)


def _safe_component_slug(value: str) -> str:
    candidate = _UNSAFE_PATH_CHARS_RE.sub("-", str(value or "").strip().lower()).strip(".-")
    return candidate or "component"


def _read_json_config(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PluginInstallError(f"Invalid JSON config format: {path}")
    return raw


def _write_model_json(path: Path, model: BaseModel) -> None:
    atomic_write_json(path, model.model_dump(mode="json", exclude_none=True))


def _read_optional_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _restore_optional_text(path: Path, text: str | None) -> None:
    if text is None:
        if path.exists():
            path.unlink()
        return
    atomic_write_text(path, text)


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onerror=_rmtree_onerror)
        return
    path.unlink()


def _rmtree_onerror(
    func: Callable[[str], object],
    path: str,
    _exc_info: object,
) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


__all__ = [
    "ComponentInstallSummary",
    "PermissionsSummary",
    "PluginInstallError",
    "PluginInstallResult",
    "PluginUninstallResult",
    "TrustPromptFn",
    "TrustPromptRequest",
    "install_plugin",
    "uninstall_plugin",
]
