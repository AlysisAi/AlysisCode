from __future__ import annotations

from enum import StrEnum

from .config_errors import ConfigError


class RuntimeKind(StrEnum):
    INTERACTIVE_CHAT = "interactive_chat"
    ONE_SHOT = "one_shot"
    FORGE_EXEC = "forge_exec"
    SWARM_WORKER = "swarm_worker"
    SUBAGENT = "subagent"
    CONFLICT_AUTO_RESOLVE = "conflict_auto_resolve"

    def __str__(self) -> str:
        return self.value


def runtime_kind_values() -> tuple[str, ...]:
    return tuple(kind.value for kind in RuntimeKind)


def normalize_runtime_kind(
    value: RuntimeKind | str | None,
    *,
    fallback: RuntimeKind | None = None,
) -> RuntimeKind:
    if isinstance(value, RuntimeKind):
        return value
    if value is None:
        if fallback is None:
            raise ConfigError("Missing runtime kind.")
        return fallback
    normalized = str(value).strip().lower()
    for kind in RuntimeKind:
        if kind.value == normalized:
            return kind
    allowed = ", ".join(runtime_kind_values())
    raise ConfigError(f"Invalid runtime kind: {value!r}. Expected one of: {allowed}")


def resolve_session_runtime_kind(
    *,
    runtime_kind: RuntimeKind | str | None,
    one_shot_execution: bool,
    subagent_depth: int,
) -> RuntimeKind:
    fallback = RuntimeKind.ONE_SHOT if one_shot_execution else RuntimeKind.INTERACTIVE_CHAT
    if subagent_depth > 0:
        fallback = RuntimeKind.SUBAGENT
    return normalize_runtime_kind(runtime_kind, fallback=fallback)
