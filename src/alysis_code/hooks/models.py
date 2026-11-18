from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ..runtime_kind import RETIRED_RUNTIME_KINDS, normalize_runtime_kind

CanonicalHookEventName = Literal[
    "PreToolUse",
    "PostToolUse",
    "PostWrite",
    "SessionEnd",
    "SessionStart",
    "TurnComplete",
    "UserPromptSubmit",
    "Notification",
    "PreCompact",
    "SubagentStop",
]
HookEventName = Literal[
    "PreToolUse",
    "PostToolUse",
    "PostWrite",
    "SessionEnd",
    "SessionStart",
    "TurnComplete",
    "Stop",
    "UserPromptSubmit",
    "Notification",
    "PreCompact",
    "SubagentStop",
]
HookFailurePolicy = Literal["warn", "continue", "block"]
HookSessionSource = Literal["startup", "resume", "fork"]

_VALID_HOOK_EVENTS: set[str] = {
    "PreToolUse",
    "PostToolUse",
    "PostWrite",
    "SessionEnd",
    "SessionStart",
    "TurnComplete",
    "Stop",
    "UserPromptSubmit",
    "Notification",
    "PreCompact",
    "SubagentStop",
}
_CANONICAL_EVENT_NAME_BY_ALIAS: dict[str, CanonicalHookEventName] = {
    "PreToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse",
    "PostWrite": "PostWrite",
    "SessionEnd": "SessionEnd",
    "SessionStart": "SessionStart",
    "TurnComplete": "TurnComplete",
    "Stop": "TurnComplete",
    "UserPromptSubmit": "UserPromptSubmit",
    "Notification": "Notification",
    "PreCompact": "PreCompact",
    "SubagentStop": "SubagentStop",
}
_HOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_VALID_FAILURE_POLICIES = {"warn", "continue", "block"}
_VALID_SESSION_SOURCES: frozenset[str] = frozenset({"startup", "resume", "fork"})


def canonicalize_hook_event_name(event_name: str) -> CanonicalHookEventName:
    normalized = str(event_name or "").strip()
    if normalized not in _VALID_HOOK_EVENTS:
        raise ValueError(f"unsupported hook event: {event_name}")
    return _CANONICAL_EVENT_NAME_BY_ALIAS[normalized]


def _normalize_optional_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    return value.strip()


def _normalize_timeout(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a positive number.")
    if not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a positive number.")
    timeout = float(value)
    if timeout <= 0:
        raise ValueError(f"{field_name} must be > 0.")
    return timeout


def _normalize_optional_metadata_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    text = _normalize_optional_text(value, field_name=field_name)
    return text or None


def _normalize_priority(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer.")
    return value


def _normalize_failure_policy(value: object, *, field_name: str) -> HookFailurePolicy:
    text = _normalize_optional_text(value, field_name=field_name).lower()
    if text not in _VALID_FAILURE_POLICIES:
        raise ValueError(f"{field_name} must be one of: warn, continue, block.")
    return cast(HookFailurePolicy, text)


def _normalize_runtime_kinds(value: object) -> list[str] | None:
    if value is None:
        return None
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list | tuple):
        raise TypeError("runtimeKinds must be a string or list of runtime kinds.")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip().lower() if isinstance(item, str) else None
        # A retired runtime never runs again. Keeping its name means a hook scoped only to
        # retired runtimes matches nothing, instead of failing the whole hooks file or
        # silently widening to every runtime.
        kind = text if text in RETIRED_RUNTIME_KINDS else normalize_runtime_kind(item).value
        if kind in seen:
            continue
        seen.add(kind)
        normalized.append(kind)
    return normalized or None


def _normalize_session_sources(value: object) -> list[HookSessionSource] | None:
    if value is None:
        return None
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list | tuple):
        raise TypeError("sessionSource must be a string or list of strings.")
    normalized: list[HookSessionSource] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise TypeError("sessionSource entries must be strings.")
        source = item.strip().lower()
        if source not in _VALID_SESSION_SOURCES:
            raise ValueError("sessionSource must contain only: startup, resume, fork.")
        if source in seen:
            continue
        seen.add(source)
        normalized.append(cast(HookSessionSource, source))
    return normalized or None


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class CommandHookSpec(_StrictModel):
    type: Literal["command"] = "command"
    command: StrictStr
    timeout: float = 10.0
    enabled: StrictBool = True
    id: StrictStr | None = None
    description: StrictStr | None = None
    priority: StrictInt = 0
    failure_policy: HookFailurePolicy = Field(default="warn", alias="failurePolicy")
    runtime_kinds: list[StrictStr] | None = Field(default=None, alias="runtimeKinds")
    session_source: list[HookSessionSource] | None = Field(default=None, alias="sessionSource")
    env_passthrough: Literal["all", "safe", "explicit"] = Field(
        default="safe", alias="envPassthrough"
    )
    env_allow: list[StrictStr] | None = Field(default=None, alias="envAllow")
    env: dict[StrictStr, StrictStr] | None = None
    receives_full_payload: StrictBool = Field(default=False, alias="receivesFullPayload")

    @model_validator(mode="before")
    @classmethod
    def _apply_timeout_ms_alias(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        raw = dict(value)
        timeout_ms = raw.pop("timeoutMs", None)
        if timeout_ms is not None:
            if "timeout" in raw:
                raise ValueError("Specify only one of timeout or timeoutMs.")
            raw["timeout"] = _normalize_timeout(timeout_ms, field_name="timeoutMs") / 1000.0
        return raw

    @field_validator("command", mode="before")
    @classmethod
    def _validate_command(cls, value: object) -> str:
        command = _normalize_optional_text(value, field_name="command")
        if not command:
            raise ValueError("command cannot be empty.")
        return command

    @field_validator("timeout", mode="before")
    @classmethod
    def _validate_timeout(cls, value: object) -> float:
        return _normalize_timeout(value, field_name="timeout")

    @field_validator("id", mode="before")
    @classmethod
    def _validate_id(cls, value: object) -> str | None:
        hook_id = _normalize_optional_metadata_text(value, field_name="id")
        if hook_id is None:
            return None
        if not _HOOK_ID_RE.fullmatch(hook_id):
            raise ValueError("id must match ^[a-z0-9][a-z0-9._-]*$.")
        return hook_id

    @field_validator("description", mode="before")
    @classmethod
    def _validate_description(cls, value: object) -> str | None:
        return _normalize_optional_metadata_text(value, field_name="description")

    @field_validator("priority", mode="before")
    @classmethod
    def _validate_priority(cls, value: object) -> int:
        return _normalize_priority(value, field_name="priority")

    @field_validator("failure_policy", mode="before")
    @classmethod
    def _validate_failure_policy(cls, value: object) -> HookFailurePolicy:
        return _normalize_failure_policy(value, field_name="failurePolicy")

    @field_validator("runtime_kinds", mode="before")
    @classmethod
    def _validate_runtime_kinds(cls, value: object) -> list[str] | None:
        return _normalize_runtime_kinds(value)

    @field_validator("session_source", mode="before")
    @classmethod
    def _validate_session_source(cls, value: object) -> list[HookSessionSource] | None:
        return _normalize_session_sources(value)

    @field_validator("env_passthrough", mode="before")
    @classmethod
    def _validate_env_passthrough(cls, value: object) -> str:
        if value is None:
            return "safe"
        if not isinstance(value, str):
            raise TypeError("envPassthrough must be a string.")
        lowered = value.strip().lower()
        if lowered not in {"all", "safe", "explicit"}:
            raise ValueError("envPassthrough must be one of: all, safe, explicit.")
        return lowered

    @field_validator("env_allow", mode="before")
    @classmethod
    def _validate_env_allow(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise TypeError("envAllow must be a list of environment variable names.")
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise TypeError("envAllow entries must be strings.")
            name = item.strip()
            if not _ENV_KEY_RE.fullmatch(name):
                raise ValueError("envAllow entries must match ^[A-Z][A-Z0-9_]*$.")
            if name in seen:
                continue
            seen.add(name)
            normalized.append(name)
        return normalized or None

    @field_validator("env", mode="before")
    @classmethod
    def _validate_env_dict(cls, value: object) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise TypeError("env must be a mapping of environment variable names to strings.")
        normalized: dict[str, str] = {}
        for key, raw_value in value.items():
            if not isinstance(key, str):
                raise TypeError("env keys must be strings.")
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError("env keys must match ^[A-Z][A-Z0-9_]*$.")
            if not isinstance(raw_value, str):
                raise TypeError("env values must be strings.")
            normalized[key] = raw_value
        return normalized or None


class HookMatcherGroup(_StrictModel):
    matcher: StrictStr = ""
    hooks: list[CommandHookSpec] = Field(default_factory=list)
    enabled: StrictBool = True

    @field_validator("matcher", mode="before")
    @classmethod
    def _validate_matcher(cls, value: object) -> str:
        if value is None:
            return ""
        return _normalize_optional_text(value, field_name="matcher")

    @model_validator(mode="after")
    def _validate_unique_hook_ids(self) -> HookMatcherGroup:
        seen: set[str] = set()
        for hook in self.hooks:
            if hook.id is None:
                continue
            if hook.id in seen:
                raise ValueError(f"duplicate hook id in matcher group: {hook.id}")
            seen.add(hook.id)
        return self


class HookConfigFile(_StrictModel):
    schema_version: int = 1
    hooks: dict[str, list[HookMatcherGroup]] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("schema_version must be 1.")
        return value

    @field_validator("hooks")
    @classmethod
    def _validate_hooks(cls, value: dict[str, list[HookMatcherGroup]]) -> dict[str, Any]:
        normalized: dict[str, list[HookMatcherGroup]] = {}
        for event_name, groups in value.items():
            canonical = canonicalize_hook_event_name(event_name)
            normalized.setdefault(canonical, []).extend(groups)
        return normalized


@dataclass(frozen=True)
class HookDispatchResult:
    blocked: bool = False
    reason: str = ""
    modified_input: dict[str, Any] | None = None
    modified_prompt: str | None = None
    additional_system_messages: tuple[str, ...] = ()
    additional_user_messages: tuple[str, ...] = ()
    system_notices: tuple[str, ...] = ()
    stop_reason: str = ""
    permission_decision: str = ""
    permission_decision_reason: str = ""
    ask_requested: bool = False
    ask_reason: str = ""


@dataclass(frozen=True)
class ParsedHookOutput:
    blocked: bool = False
    reason: str = ""
    modified_input: dict[str, Any] | None = None
    modified_prompt: str | None = None
    additional_system_messages: tuple[str, ...] = ()
    additional_user_messages: tuple[str, ...] = ()
    system_notices: tuple[str, ...] = ()
    halt: bool = False
    stop_reason: str = ""
    allow_short_circuit: bool = False
    suppress_output: bool = False
    permission_decision: str = ""
    permission_decision_reason: str = ""
    stdout_chars: int = 0
    stderr_chars: int = 0
    status: str = "ok"
    warnings: tuple[str, ...] = ()
    ask_requested: bool = False
    ask_reason: str = ""


@dataclass(frozen=True)
class HookInvocationContext:
    event_name: CanonicalHookEventName
    source_path: str
    source_scope: str
    matcher: str
    hook_id: str
    priority: int
    failure_policy: HookFailurePolicy
    command: str
    timeout_s: float
    trusted: bool = True
    returncode: int | None = None
    blocked: bool = False
    modified_input: bool = False
    modified_input_fields: tuple[str, ...] = ()
    modified_prompt: bool = False
    modified_prompt_chars: int = 0
    additional_system_message_count: int = 0
    additional_user_message_count: int = 0
    system_notices_count: int = 0
    halt_requested: bool = False
    allow_short_circuited: bool = False
    suppress_output: bool = False
    permission_decision: str = ""
    stop_reason: str = ""
    stdout_chars: int = 0
    stderr_chars: int = 0
    duration_ms: int = 0
    status: str = "ok"
    warnings: tuple[str, ...] = ()
    payload_truncated: bool = False
    payload_bytes: int = 0


@dataclass
class MutableHookDispatchState:
    blocked: bool = False
    reason: str = ""
    modified_input: dict[str, Any] | None = None
    modified_prompt: str | None = None
    additional_system_messages: list[str] = field(default_factory=list)
    additional_user_messages: list[str] = field(default_factory=list)
    system_notices: list[str] = field(default_factory=list)
    stop_reason: str = ""
    permission_decision: str = ""
    permission_decision_reason: str = ""
    ask_requested: bool = False
    ask_reason: str = ""

    def freeze(self) -> HookDispatchResult:
        return HookDispatchResult(
            blocked=self.blocked,
            reason=self.reason,
            modified_input=self.modified_input,
            modified_prompt=self.modified_prompt,
            additional_system_messages=tuple(self.additional_system_messages),
            additional_user_messages=tuple(self.additional_user_messages),
            system_notices=tuple(self.system_notices),
            stop_reason=self.stop_reason,
            permission_decision=self.permission_decision,
            permission_decision_reason=self.permission_decision_reason,
            ask_requested=self.ask_requested,
            ask_reason=self.ask_reason,
        )
