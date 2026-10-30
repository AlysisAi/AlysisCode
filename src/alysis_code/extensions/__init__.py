"""Public plugin API without loading installation code during activation."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .activation import (
        ActivationDecision,
        WorkspaceTrustPromptFn,
        WorkspaceTrustPromptRequest,
        resolve_active_plugins,
    )
    from .install import (
        ComponentInstallSummary,
        EnableResult,
        PermissionsSummary,
        PluginInstallError,
        PluginInstallResult,
        PluginUninstallResult,
        TrustPromptRequest,
        disable_plugin,
        enable_plugin,
        install_plugin,
        uninstall_plugin,
    )
    from .manifest import PluginManifest, PluginManifestError, load_manifest
    from .models import (
        ExtensionState,
        ProjectExtensionOverrides,
        RegistryEntry,
        RegistryFile,
        normalize_extension_id,
        plugin_slug_from_id,
    )
    from .registry import find_by_id, load_registry, search
    from .state import (
        compute_effective_enabled,
        load_global_state,
        load_project_overrides,
        load_project_state,
        save_global_state,
        save_project_state,
    )
    from .workspace_trust import (
        WORKSPACE_TRUST_SCHEMA_VERSION,
        WorkspaceTrustEntry,
        WorkspaceTrustState,
        grant_workspace_trust,
        is_workspace_trusted,
        load_workspace_trust,
        workspace_trust_key,
        workspace_trust_path,
    )

_EXPORT_MODULES = {
    "ActivationDecision": "activation",
    "WorkspaceTrustPromptFn": "activation",
    "WorkspaceTrustPromptRequest": "activation",
    "resolve_active_plugins": "activation",
    "ComponentInstallSummary": "install",
    "EnableResult": "install",
    "PermissionsSummary": "install",
    "PluginInstallError": "install",
    "PluginInstallResult": "install",
    "PluginUninstallResult": "install",
    "TrustPromptRequest": "install",
    "disable_plugin": "install",
    "enable_plugin": "install",
    "install_plugin": "install",
    "uninstall_plugin": "install",
    "PluginManifest": "manifest",
    "PluginManifestError": "manifest",
    "load_manifest": "manifest",
    "ExtensionState": "models",
    "ProjectExtensionOverrides": "models",
    "RegistryEntry": "models",
    "RegistryFile": "models",
    "normalize_extension_id": "models",
    "plugin_slug_from_id": "models",
    "find_by_id": "registry",
    "load_registry": "registry",
    "search": "registry",
    "compute_effective_enabled": "state",
    "load_global_state": "state",
    "load_project_overrides": "state",
    "load_project_state": "state",
    "save_global_state": "state",
    "save_project_state": "state",
    "WORKSPACE_TRUST_SCHEMA_VERSION": "workspace_trust",
    "WorkspaceTrustEntry": "workspace_trust",
    "WorkspaceTrustState": "workspace_trust",
    "grant_workspace_trust": "workspace_trust",
    "is_workspace_trusted": "workspace_trust",
    "load_workspace_trust": "workspace_trust",
    "workspace_trust_key": "workspace_trust",
    "workspace_trust_path": "workspace_trust",
}

__all__ = [
    "ActivationDecision",
    "ComponentInstallSummary",
    "EnableResult",
    "PluginManifest",
    "PluginManifestError",
    "PermissionsSummary",
    "PluginInstallError",
    "PluginInstallResult",
    "PluginUninstallResult",
    "TrustPromptRequest",
    "WORKSPACE_TRUST_SCHEMA_VERSION",
    "WorkspaceTrustEntry",
    "WorkspaceTrustPromptFn",
    "WorkspaceTrustPromptRequest",
    "WorkspaceTrustState",
    "ExtensionState",
    "ProjectExtensionOverrides",
    "RegistryEntry",
    "RegistryFile",
    "compute_effective_enabled",
    "disable_plugin",
    "enable_plugin",
    "find_by_id",
    "grant_workspace_trust",
    "is_workspace_trusted",
    "load_global_state",
    "load_project_overrides",
    "load_project_state",
    "load_registry",
    "load_workspace_trust",
    "normalize_extension_id",
    "plugin_slug_from_id",
    "resolve_active_plugins",
    "save_global_state",
    "save_project_state",
    "search",
    "install_plugin",
    "load_manifest",
    "uninstall_plugin",
    "workspace_trust_key",
    "workspace_trust_path",
]


def __getattr__(name: str) -> Any:
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
