from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from ..branding import env_get
from .base import AgentRuntimeAdapter
from .registry import AgentRuntimeRegistry, DuplicateRuntimeError

RUNTIME_ENTRY_POINT_GROUP = "alysis.agent_runtimes"
RUNTIME_PLUGIN_OPT_IN_ENV = "ALYSIS_ENABLE_AGENT_RUNTIME_PLUGINS"


@dataclass(frozen=True, slots=True)
class RuntimeSetupOption:
    """Provider-neutral metadata rendered by setup and configuration UIs."""

    id: str
    label: str
    description: str
    adapter: str
    default_executable: str
    auth_hint: str


# Alysis ships no delegated runtime of its own; installed plugins may add them.
_BUILTIN_OPTIONS: tuple[RuntimeSetupOption, ...] = ()


def runtime_setup_options() -> tuple[RuntimeSetupOption, ...]:
    """Return runtimes supported by this installation.

    Setup/config code depends only on this metadata contract. Additional adapters
    can extend discovery later without introducing provider conditionals there.
    """

    options = list(_BUILTIN_OPTIONS)
    known = {option.id for option in options}
    for adapter in _plugin_runtime_adapters():
        runtime_id = str(adapter.runtime_id or "").strip()
        if not runtime_id or runtime_id in known:
            continue
        options.append(_option_from_adapter(adapter))
        known.add(runtime_id)
    return tuple(options)


def runtime_setup_option(runtime_id: str) -> RuntimeSetupOption | None:
    normalized = str(runtime_id or "").strip()
    return next((option for option in runtime_setup_options() if option.id == normalized), None)


def create_builtin_runtime_registry() -> AgentRuntimeRegistry:
    """Build a fresh registry so tests and callers never share mutable state."""

    return AgentRuntimeRegistry(())


def create_runtime_registry() -> AgentRuntimeRegistry:
    """Build the installed registry, including trusted Python entry points."""

    registry = create_builtin_runtime_registry()
    for adapter in _plugin_runtime_adapters():
        try:
            registry.register(adapter)
        except DuplicateRuntimeError:
            continue
    return registry


def _plugin_runtime_adapters() -> tuple[AgentRuntimeAdapter, ...]:
    enabled = str(env_get(RUNTIME_PLUGIN_OPT_IN_ENV) or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return ()
    try:
        selected = entry_points().select(group=RUNTIME_ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 - broken package metadata must not break setup
        return ()
    adapters: list[AgentRuntimeAdapter] = []
    for entry_point in selected:
        try:
            loaded: Any = entry_point.load()
            candidate = loaded() if isinstance(loaded, type) else loaded
            if callable(candidate) and not hasattr(candidate, "runtime_id"):
                candidate = candidate()
            if isinstance(candidate, AgentRuntimeAdapter):
                adapters.append(candidate)
        except Exception:  # noqa: BLE001 - one optional plugin cannot block built-ins
            continue
    return tuple(adapters)


def _option_from_adapter(adapter: AgentRuntimeAdapter) -> RuntimeSetupOption:
    runtime_id = str(adapter.runtime_id).strip()
    return RuntimeSetupOption(
        id=runtime_id,
        label=str(adapter.display_name or runtime_id),
        description=str(getattr(adapter, "description", "Provider-managed agent runtime.")),
        adapter=str(getattr(adapter, "adapter_id", runtime_id)),
        default_executable=str(getattr(adapter, "default_executable", runtime_id)),
        auth_hint=str(
            getattr(
                adapter,
                "auth_hint",
                "Authentication is managed by the provider runtime; Alysis Code does not copy tokens.",
            )
        ),
    )


__all__ = [
    "RuntimeSetupOption",
    "RUNTIME_ENTRY_POINT_GROUP",
    "RUNTIME_PLUGIN_OPT_IN_ENV",
    "create_builtin_runtime_registry",
    "create_runtime_registry",
    "runtime_setup_option",
    "runtime_setup_options",
]
