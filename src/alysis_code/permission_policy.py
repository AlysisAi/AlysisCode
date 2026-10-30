from __future__ import annotations

import fnmatch
import hashlib
import json
import ntpath
import os
import posixpath
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from io import BufferedRandom
from pathlib import Path
from typing import Any, Literal

from .approval_scope import SUPPORTED_APPROVAL_SCOPES, ApprovalSessionScope
from .atomic_io import atomic_write_json
from .branding import canonical_user_config_dir, env_get

PERMISSION_POLICY_SCHEMA_VERSION = 1
_MAX_RULES = 1_000
_MAX_SELECTOR_LENGTH = 4_096
_MAX_SOURCE_LENGTH = 128
_RULE_ID_RE = re.compile(r"^pr_[a-f0-9]{32}$")
_SESSION_GRANT_ID_RE = re.compile(r"^sg_[a-f0-9]{32}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^(?:[a-zA-Z]:[\\/]|[\\/]{2})")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_POLICY_STORE_LOCK = threading.RLock()


def default_permission_policy_path() -> Path:
    """Return the user-scoped policy path, honoring the standard config override."""

    override = env_get("ALYSIS_CONFIG_DIR")
    root = Path(override).expanduser() if override else canonical_user_config_dir()
    return root / "permission_policy.json"


if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no cover - exercised on POSIX
    import fcntl


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionPolicyError(RuntimeError):
    """Base error that deliberately does not include policy selector contents."""


class PermissionPolicyValidationError(PermissionPolicyError):
    pass


class PermissionPolicyCorruptError(PermissionPolicyError):
    def __init__(self) -> None:
        super().__init__("Permission policy is invalid or unreadable; access is denied.")


@dataclass(frozen=True, slots=True)
class PermissionRule:
    id: str
    effect: PolicyEffect
    order: int
    source: str
    tool_pattern: str | None = None
    path_pattern: str | None = None
    command_pattern: str | None = field(default=None, repr=False)

    def to_public_dict(self, *, reveal_command_pattern: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "effect": self.effect.value,
            "order": self.order,
            "source": self.source,
            "tool_pattern": self.tool_pattern,
            "path_pattern": self.path_pattern,
            "has_command_pattern": self.command_pattern is not None,
        }
        if reveal_command_pattern and self.command_pattern is not None:
            result["command_pattern"] = self.command_pattern
        return result

    def _to_storage_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "effect": self.effect.value,
            "order": self.order,
            "source": self.source,
            "tool_pattern": self.tool_pattern,
            "path_pattern": self.path_pattern,
            "command_pattern": self.command_pattern,
        }


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    tool_name: str
    paths: tuple[str, ...] = ()
    command: str | None = field(default=None, repr=False)
    workspace_root: str | None = None
    platform: Literal["auto", "windows", "posix"] = "auto"
    sensitive: bool = False
    external_directory: bool = False

    @classmethod
    def create(
        cls,
        tool_name: str,
        *,
        path: str | None = None,
        paths: Sequence[str] = (),
        command: str | None = None,
        workspace_root: str | os.PathLike[str] | None = None,
        platform: Literal["auto", "windows", "posix"] = "auto",
        sensitive: bool = False,
        external_directory: bool = False,
    ) -> PermissionRequest:
        combined_paths = ((path,) if path is not None else ()) + tuple(paths)
        return cls(
            tool_name=tool_name,
            paths=combined_paths,
            command=command,
            workspace_root=os.fspath(workspace_root) if workspace_root is not None else None,
            platform=platform,
            sensitive=sensitive,
            external_directory=external_directory,
        )


@dataclass(frozen=True, slots=True)
class PermissionEvaluation:
    decision: PolicyEffect
    reason: str
    matched_rule_id: str | None
    matched_rule_source: str | None
    specificity: int

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyEffect.ALLOW


@dataclass(frozen=True, slots=True)
class _NormalizedRequest:
    tool_name: str
    paths: tuple[str, ...]
    command: str | None = field(repr=False)
    platform: Literal["windows", "posix"]
    sensitive: bool
    external_directory: bool


class PermissionPolicy:
    """Ordered, explainable permission rules with non-bypassable safety overrides."""

    def __init__(
        self,
        rules: Sequence[PermissionRule] = (),
        *,
        sensitive_override: PolicyEffect | str = PolicyEffect.ASK,
        external_directory_override: PolicyEffect | str = PolicyEffect.ASK,
    ) -> None:
        self._lock = threading.RLock()
        self._rules = list(rules)
        self.sensitive_override = _validate_safety_override(sensitive_override)
        self.external_directory_override = _validate_safety_override(external_directory_override)
        _validate_rule_collection(self._rules)

    def list_rules(self) -> tuple[PermissionRule, ...]:
        with self._lock:
            return tuple(sorted(self._rules, key=lambda rule: rule.order))

    def grant(
        self,
        effect: PolicyEffect | str,
        *,
        tool_pattern: str | None = None,
        path_pattern: str | None = None,
        command_pattern: str | None = None,
        source: str = "user",
    ) -> PermissionRule:
        normalized_effect = _parse_effect(effect)
        normalized_tool = _normalize_optional_tool_pattern(tool_pattern)
        normalized_path = _normalize_optional_path_pattern(path_pattern)
        normalized_command = _normalize_optional_command_pattern(command_pattern)
        normalized_source = _validate_source(source)
        if normalized_tool is None and normalized_path is None and normalized_command is None:
            raise PermissionPolicyValidationError("A permission rule requires a selector.")
        with self._lock:
            if len(self._rules) >= _MAX_RULES:
                raise PermissionPolicyValidationError("Permission policy contains too many rules.")
            next_order = max((rule.order for rule in self._rules), default=-1) + 1
            rule = PermissionRule(
                id=f"pr_{uuid.uuid4().hex}",
                effect=normalized_effect,
                order=next_order,
                source=normalized_source,
                tool_pattern=normalized_tool,
                path_pattern=normalized_path,
                command_pattern=normalized_command,
            )
            self._rules.append(rule)
            return rule

    def revoke(self, rule_id: str) -> bool:
        _validate_rule_id(rule_id)
        with self._lock:
            for index, rule in enumerate(self._rules):
                if rule.id == rule_id:
                    del self._rules[index]
                    return True
        return False

    def set_safety_overrides(
        self,
        *,
        sensitive: PolicyEffect | str | None = None,
        external_directory: PolicyEffect | str | None = None,
    ) -> None:
        with self._lock:
            if sensitive is not None:
                self.sensitive_override = _validate_safety_override(sensitive)
            if external_directory is not None:
                self.external_directory_override = _validate_safety_override(external_directory)

    def evaluate(
        self,
        request: PermissionRequest,
        *,
        review_external: bool = False,
        review_authentication: bool = False,
    ) -> PermissionEvaluation:
        normalized = _normalize_request(request)
        safety = self._evaluate_safety_overrides(
            normalized, review_external=review_external, review_authentication=review_authentication
        )
        if safety is not None and safety.decision is PolicyEffect.DENY:
            return safety

        matches: list[tuple[int, int, int, PermissionRule]] = []
        for rule in self.list_rules():
            if not _rule_matches(rule, normalized):
                continue
            specificity = _rule_specificity(rule)
            # At equal specificity, fail closed: deny, then ask, then allow.
            effect_precedence = {
                PolicyEffect.ALLOW: 0,
                PolicyEffect.ASK: 1,
                PolicyEffect.DENY: 2,
            }[rule.effect]
            # Earlier rules win only after specificity and safety precedence tie.
            matches.append((specificity, effect_precedence, -rule.order, rule))
        if not matches:
            if safety is not None:
                return safety
            return PermissionEvaluation(
                decision=PolicyEffect.ASK,
                reason="no_matching_rule",
                matched_rule_id=None,
                matched_rule_source=None,
                specificity=0,
            )
        specificity, _, _, winner = max(matches, key=lambda item: item[:3])
        # A mandatory question can restrict an allow rule, but must never turn
        # the winning deny rule into an action a person could approve.
        if safety is not None and winner.effect is not PolicyEffect.DENY:
            return safety
        return PermissionEvaluation(
            decision=winner.effect,
            reason="matched_rule",
            matched_rule_id=winner.id,
            matched_rule_source=winner.source,
            specificity=specificity,
        )

    def _evaluate_safety_overrides(
        self,
        request: _NormalizedRequest,
        *,
        review_external: bool = False,
        review_authentication: bool = False,
    ) -> PermissionEvaluation | None:
        candidates: list[tuple[PolicyEffect, str, str]] = []
        # The legacy review_authentication argument cannot suppress credential safety.
        if request.sensitive:
            candidates.append(
                (
                    self.sensitive_override,
                    "sensitive_resource_requires_approval",
                    "override:sensitive",
                )
            )
        if request.external_directory and not (
            review_external and self.external_directory_override is PolicyEffect.ASK
        ):
            candidates.append(
                (
                    self.external_directory_override,
                    "external_directory_requires_approval",
                    "override:external_directory",
                )
            )
        if not candidates:
            return None
        effect, reason, rule_id = max(
            candidates,
            key=lambda item: (item[0] is PolicyEffect.DENY, item[2] == "override:sensitive"),
        )
        return PermissionEvaluation(
            decision=effect,
            reason=reason,
            matched_rule_id=rule_id,
            matched_rule_source="builtin_safety",
            specificity=2_147_483_647,
        )

    def _to_storage_payload(self) -> dict[str, Any]:
        return {
            "schema_version": PERMISSION_POLICY_SCHEMA_VERSION,
            "safety_overrides": {
                "sensitive": self.sensitive_override.value,
                "external_directory": self.external_directory_override.value,
            },
            "rules": [rule._to_storage_dict() for rule in self.list_rules()],
        }


class PermissionPolicyStore:
    """Atomic JSON-backed policy store. Corruption always evaluates to deny."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> PermissionPolicy:
        with _POLICY_STORE_LOCK:
            return _load_policy_from_path(self.path)

    def save(self, policy: PermissionPolicy) -> None:
        with _locked_policy_state(self.path):
            _save_policy_to_path(self.path, policy)

    def list_rules(self) -> tuple[PermissionRule, ...]:
        return self.load().list_rules()

    def grant(
        self,
        effect: PolicyEffect | str,
        *,
        tool_pattern: str | None = None,
        path_pattern: str | None = None,
        command_pattern: str | None = None,
        source: str = "user",
    ) -> PermissionRule:
        with _locked_policy_state(self.path):
            policy = _load_policy_from_path(self.path)
            rule = policy.grant(
                effect,
                tool_pattern=tool_pattern,
                path_pattern=path_pattern,
                command_pattern=command_pattern,
                source=source,
            )
            _save_policy_to_path(self.path, policy)
            return rule

    def revoke(self, rule_id: str) -> bool:
        with _locked_policy_state(self.path):
            policy = _load_policy_from_path(self.path)
            removed = policy.revoke(rule_id)
            if removed:
                _save_policy_to_path(self.path, policy)
            return removed

    def set_safety_overrides(
        self,
        *,
        sensitive: PolicyEffect | str | None = None,
        external_directory: PolicyEffect | str | None = None,
    ) -> None:
        with _locked_policy_state(self.path):
            policy = _load_policy_from_path(self.path)
            policy.set_safety_overrides(
                sensitive=sensitive,
                external_directory=external_directory,
            )
            _save_policy_to_path(self.path, policy)

    def evaluate(
        self,
        request: PermissionRequest,
        *,
        review_external: bool = False,
        review_authentication: bool = False,
    ) -> PermissionEvaluation:
        try:
            return self.load().evaluate(
                request,
                review_external=review_external,
                review_authentication=review_authentication,
            )
        except PermissionPolicyCorruptError:
            return PermissionEvaluation(
                decision=PolicyEffect.DENY,
                reason="permission_policy_corrupt",
                matched_rule_id="override:corrupt_policy",
                matched_rule_source="builtin_safety",
                specificity=2_147_483_647,
            )


@dataclass(frozen=True, slots=True)
class SessionPermissionGrant:
    id: str
    kind: str
    scope_type: str
    source: str
    _scope_fingerprint: str = field(repr=False)

    def to_public_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "kind": self.kind,
            "scope_type": self.scope_type,
            "source": self.source,
        }


class InMemorySessionGrantAdapter:
    """Inspectable opaque IDs around existing exact approval-session scopes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._grants: list[SessionPermissionGrant] = []

    def grant(
        self,
        *,
        kind: str,
        scope: Mapping[str, Any],
        source: str = "session",
    ) -> SessionPermissionGrant:
        normalized_kind = _validate_kind(kind)
        scope_type, fingerprint = _validated_scope_fingerprint(scope)
        declared_kind = str(scope.get("kind") or "").strip().casefold()
        if declared_kind and declared_kind != normalized_kind:
            raise PermissionPolicyValidationError("Approval session scope kind does not match.")
        normalized_source = _validate_source(source)
        with self._lock:
            for grant in self._grants:
                if grant.kind == normalized_kind and grant._scope_fingerprint == fingerprint:
                    return grant
            grant = SessionPermissionGrant(
                id=f"sg_{uuid.uuid4().hex}",
                kind=normalized_kind,
                scope_type=scope_type,
                source=normalized_source,
                _scope_fingerprint=fingerprint,
            )
            self._grants.append(grant)
            return grant

    def grant_approval_scope(
        self,
        approval_scope: ApprovalSessionScope,
        *,
        kind: str | None = None,
        source: str = "session",
    ) -> SessionPermissionGrant:
        if not approval_scope.supported or approval_scope.scope is None:
            raise PermissionPolicyValidationError("Approval session scope is not safe to grant.")
        resolved_kind = kind or str(
            approval_scope.scope.get("kind") or approval_scope.scope.get("safe_kind") or ""
        )
        return self.grant(kind=resolved_kind, scope=approval_scope.scope, source=source)

    def list_grants(self) -> tuple[SessionPermissionGrant, ...]:
        with self._lock:
            return tuple(self._grants)

    def revoke(self, grant_id: str) -> bool:
        if _SESSION_GRANT_ID_RE.fullmatch(str(grant_id)) is None:
            raise PermissionPolicyValidationError("Session grant id is invalid.")
        with self._lock:
            for index, grant in enumerate(self._grants):
                if grant.id == grant_id:
                    del self._grants[index]
                    return True
        return False

    def evaluate(
        self,
        *,
        kind: str,
        scope: Mapping[str, Any],
        sensitive: bool = False,
        external_directory: bool = False,
    ) -> PermissionEvaluation:
        if sensitive:
            return _session_safety_evaluation(
                "sensitive_resource_requires_approval", "override:sensitive"
            )
        if external_directory:
            return _session_safety_evaluation(
                "external_directory_requires_approval", "override:external_directory"
            )
        normalized_kind = _validate_kind(kind)
        _, fingerprint = _validated_scope_fingerprint(scope)
        with self._lock:
            for grant in self._grants:
                if grant.kind == normalized_kind and grant._scope_fingerprint == fingerprint:
                    return PermissionEvaluation(
                        decision=PolicyEffect.ALLOW,
                        reason="session_exact_grant",
                        matched_rule_id=grant.id,
                        matched_rule_source=grant.source,
                        specificity=2_000_000_000,
                    )
        return PermissionEvaluation(
            decision=PolicyEffect.ASK,
            reason="session_grant_not_found",
            matched_rule_id=None,
            matched_rule_source=None,
            specificity=0,
        )


def normalize_path_pattern(pattern: str) -> str:
    return _normalize_path_pattern(pattern)


def normalize_command_pattern(
    pattern: str, *, platform: Literal["windows", "posix"] = "posix"
) -> str:
    normalized = _normalize_command_text(pattern)
    return normalized.casefold() if platform == "windows" else normalized


def normalize_workspace_path(
    path: str,
    *,
    workspace_root: str | None = None,
    platform: Literal["auto", "windows", "posix"] = "auto",
) -> tuple[str | None, bool]:
    selected_platform = _select_platform(platform, path, workspace_root)
    return _normalize_workspace_path(path, workspace_root, selected_platform)


def _load_policy_from_path(path: Path) -> PermissionPolicy:
    if not path.exists():
        return PermissionPolicy()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _policy_from_payload(raw)
    except PermissionPolicyCorruptError:
        raise
    except Exception:  # noqa: BLE001 - persistent policy must fail closed
        raise PermissionPolicyCorruptError() from None


def _save_policy_to_path(path: Path, policy: PermissionPolicy) -> None:
    if not isinstance(policy, PermissionPolicy):
        raise PermissionPolicyValidationError("Permission policy is invalid.")
    atomic_write_json(path, policy._to_storage_payload(), ensure_ascii=True)


@contextmanager
def _locked_policy_state(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with _POLICY_STORE_LOCK:
        with lock_path.open("a+b") as handle:
            _acquire_file_lock(handle)
            try:
                yield
            finally:
                _release_file_lock(handle)


def _acquire_file_lock(handle: BufferedRandom) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_file_lock(handle: BufferedRandom) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _policy_from_payload(raw: Any) -> PermissionPolicy:
    try:
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "safety_overrides",
            "rules",
        }:
            raise ValueError
        if (
            isinstance(raw["schema_version"], bool)
            or raw["schema_version"] != PERMISSION_POLICY_SCHEMA_VERSION
        ):
            raise ValueError
        overrides = raw["safety_overrides"]
        if not isinstance(overrides, dict) or set(overrides) != {
            "sensitive",
            "external_directory",
        }:
            raise ValueError
        rules_raw = raw["rules"]
        if not isinstance(rules_raw, list) or len(rules_raw) > _MAX_RULES:
            raise ValueError
        rules = tuple(_rule_from_payload(item) for item in rules_raw)
        return PermissionPolicy(
            rules,
            sensitive_override=overrides["sensitive"],
            external_directory_override=overrides["external_directory"],
        )
    except (PermissionPolicyValidationError, TypeError, ValueError):
        raise PermissionPolicyCorruptError() from None


def _rule_from_payload(raw: Any) -> PermissionRule:
    if not isinstance(raw, dict) or set(raw) != {
        "id",
        "effect",
        "order",
        "source",
        "tool_pattern",
        "path_pattern",
        "command_pattern",
    }:
        raise ValueError
    _validate_rule_id(raw["id"])
    if isinstance(raw["order"], bool) or not isinstance(raw["order"], int) or raw["order"] < 0:
        raise ValueError
    rule = PermissionRule(
        id=raw["id"],
        effect=_parse_effect(raw["effect"]),
        order=raw["order"],
        source=_validate_source(raw["source"]),
        tool_pattern=_normalize_optional_tool_pattern(raw["tool_pattern"]),
        path_pattern=_normalize_optional_path_pattern(raw["path_pattern"]),
        command_pattern=_normalize_optional_command_pattern(raw["command_pattern"]),
    )
    if rule.tool_pattern is None and rule.path_pattern is None and rule.command_pattern is None:
        raise ValueError
    return rule


def _validate_rule_collection(rules: Sequence[PermissionRule]) -> None:
    if len(rules) > _MAX_RULES:
        raise PermissionPolicyValidationError("Permission policy contains too many rules.")
    if any(not isinstance(rule, PermissionRule) for rule in rules):
        raise PermissionPolicyValidationError("Permission policy rule is invalid.")
    ids = [rule.id for rule in rules]
    orders = [rule.order for rule in rules]
    if len(ids) != len(set(ids)) or len(orders) != len(set(orders)):
        raise PermissionPolicyValidationError("Permission policy rule identity is invalid.")
    for rule in rules:
        _validate_rule_id(rule.id)
        if isinstance(rule.order, bool) or not isinstance(rule.order, int) or rule.order < 0:
            raise PermissionPolicyValidationError("Permission policy rule order is invalid.")
        _parse_effect(rule.effect)
        _validate_source(rule.source)
        tool_pattern = _normalize_optional_tool_pattern(rule.tool_pattern)
        path_pattern = _normalize_optional_path_pattern(rule.path_pattern)
        command_pattern = _normalize_optional_command_pattern(rule.command_pattern)
        if tool_pattern is None and path_pattern is None and command_pattern is None:
            raise PermissionPolicyValidationError("A permission rule requires a selector.")


def _validate_rule_id(rule_id: Any) -> None:
    if not isinstance(rule_id, str) or _RULE_ID_RE.fullmatch(rule_id) is None:
        raise PermissionPolicyValidationError("Permission rule id is invalid.")


def _parse_effect(effect: PolicyEffect | str) -> PolicyEffect:
    try:
        return effect if isinstance(effect, PolicyEffect) else PolicyEffect(effect)
    except (TypeError, ValueError):
        raise PermissionPolicyValidationError("Permission rule effect is invalid.") from None


def _validate_safety_override(effect: PolicyEffect | str) -> PolicyEffect:
    parsed = _parse_effect(effect)
    if parsed is PolicyEffect.ALLOW:
        raise PermissionPolicyValidationError("Safety overrides cannot allow access.")
    return parsed


def _validate_source(source: Any) -> str:
    if not isinstance(source, str):
        raise PermissionPolicyValidationError("Permission rule source is invalid.")
    normalized = source.strip()
    if (
        not normalized
        or len(normalized) > _MAX_SOURCE_LENGTH
        or _CONTROL_CHARACTER_RE.search(normalized)
    ):
        raise PermissionPolicyValidationError("Permission rule source is invalid.")
    return normalized


def _validate_kind(kind: Any) -> str:
    if not isinstance(kind, str):
        raise PermissionPolicyValidationError("Permission scope kind is invalid.")
    normalized = kind.strip().casefold()
    if (
        not normalized
        or len(normalized) > _MAX_SOURCE_LENGTH
        or _CONTROL_CHARACTER_RE.search(normalized)
    ):
        raise PermissionPolicyValidationError("Permission scope kind is invalid.")
    return normalized


def _normalize_optional_tool_pattern(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PermissionPolicyValidationError("Tool selector is invalid.")
    normalized = value.strip().casefold()
    _validate_selector(normalized, "Tool selector is invalid.")
    if "/" in normalized or "\\" in normalized:
        raise PermissionPolicyValidationError("Tool selector is invalid.")
    return normalized


def _normalize_optional_path_pattern(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PermissionPolicyValidationError("Path selector is invalid.")
    return _normalize_path_pattern(value)


def _normalize_path_pattern(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    _validate_selector(normalized, "Path selector is invalid.")
    if normalized.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(normalized):
        raise PermissionPolicyValidationError("Path selector must be workspace-relative.")
    if any(part == ".." for part in normalized.split("/")):
        raise PermissionPolicyValidationError("Path selector must be workspace-relative.")
    return normalized


def _normalize_optional_command_pattern(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PermissionPolicyValidationError("Command selector is invalid.")
    normalized = _normalize_command_text(value)
    _validate_selector(normalized, "Command selector is invalid.")
    return normalized


def _validate_selector(value: str, message: str) -> None:
    if not value or len(value) > _MAX_SELECTOR_LENGTH or "\x00" in value:
        raise PermissionPolicyValidationError(message)


def _normalize_command_text(value: str) -> str:
    # Collapse separators without changing whitespace inside quoted shell arguments.
    normalized: list[str] = []
    quote: str | None = None
    escaped = False
    separator_pending = False
    for char in value.replace("\r\n", "\n").replace("\r", "\n"):
        if quote is not None:
            normalized.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char.isspace():
            separator_pending = True
            continue
        if separator_pending and normalized:
            normalized.append(" ")
        separator_pending = False
        normalized.append(char)
        if char in {"'", '"'}:
            quote = char
    return "".join(normalized)


def _normalize_request(request: PermissionRequest) -> _NormalizedRequest:
    if not isinstance(request, PermissionRequest):
        raise PermissionPolicyValidationError("Permission request is invalid.")
    tool_name = _validate_kind(request.tool_name)
    selected_platform = _select_platform(
        request.platform,
        *(request.paths[:1]),
        request.workspace_root,
    )
    normalized_paths: list[str] = []
    detected_external = False
    for path in request.paths:
        normalized, external = _normalize_workspace_path(
            path,
            request.workspace_root,
            selected_platform,
        )
        detected_external = detected_external or external
        if normalized is not None:
            normalized_paths.append(normalized)
    command = _normalize_command_text(request.command) if request.command is not None else None
    if command is not None and selected_platform == "windows":
        command = command.casefold()
    return _NormalizedRequest(
        tool_name=tool_name,
        paths=tuple(normalized_paths),
        command=command,
        platform=selected_platform,
        sensitive=bool(request.sensitive),
        external_directory=bool(request.external_directory or detected_external),
    )


def _select_platform(
    platform: Literal["auto", "windows", "posix"], *values: str | None
) -> Literal["windows", "posix"]:
    if platform in {"windows", "posix"}:
        return platform
    if platform != "auto":
        raise PermissionPolicyValidationError("Permission request platform is invalid.")
    if any(value and _WINDOWS_ABSOLUTE_RE.match(value) for value in values):
        return "windows"
    return "windows" if os.name == "nt" else "posix"


def _normalize_workspace_path(
    value: str,
    workspace_root: str | None,
    platform: Literal["windows", "posix"],
) -> tuple[str | None, bool]:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise PermissionPolicyValidationError("Permission request path is invalid.")
    if platform == "windows":
        return _normalize_windows_workspace_path(value, workspace_root)
    return _normalize_posix_workspace_path(value, workspace_root)


def _normalize_windows_workspace_path(
    value: str, workspace_root: str | None
) -> tuple[str | None, bool]:
    raw = value.strip().replace("/", "\\")
    is_absolute = bool(_WINDOWS_ABSOLUTE_RE.match(raw))
    if is_absolute:
        if not workspace_root or not _WINDOWS_ABSOLUTE_RE.match(workspace_root):
            return None, True
        try:
            relative = ntpath.relpath(ntpath.normpath(raw), ntpath.normpath(workspace_root))
        except ValueError:
            return None, True
        if relative == ".." or relative.startswith(f"..{ntpath.sep}") or ntpath.isabs(relative):
            return None, True
        raw = relative
    normalized = ntpath.normpath(raw).replace("\\", "/")
    if normalized in {"", "."}:
        return ".", False
    if normalized == ".." or normalized.startswith("../") or _WINDOWS_ABSOLUTE_RE.match(normalized):
        return None, True
    return normalized.casefold(), False


def _normalize_posix_workspace_path(
    value: str, workspace_root: str | None
) -> tuple[str | None, bool]:
    raw = value.strip().replace("\\", "/")
    if posixpath.isabs(raw):
        if not workspace_root or not posixpath.isabs(workspace_root.replace("\\", "/")):
            return None, True
        root = posixpath.normpath(workspace_root.replace("\\", "/"))
        normalized_absolute = posixpath.normpath(raw)
        try:
            if posixpath.commonpath((root, normalized_absolute)) != root:
                return None, True
        except ValueError:
            return None, True
        raw = posixpath.relpath(normalized_absolute, root)
    normalized = posixpath.normpath(raw)
    if normalized in {"", "."}:
        return ".", False
    if normalized == ".." or normalized.startswith("../") or posixpath.isabs(normalized):
        return None, True
    return normalized, False


def _rule_matches(rule: PermissionRule, request: _NormalizedRequest) -> bool:
    if rule.tool_pattern is not None and not fnmatch.fnmatchcase(
        request.tool_name, rule.tool_pattern
    ):
        return False
    if rule.command_pattern is not None:
        if request.command is None:
            return False
        pattern = normalize_command_pattern(rule.command_pattern, platform=request.platform)
        if not fnmatch.fnmatchcase(request.command, pattern):
            return False
    if rule.path_pattern is not None:
        if not request.paths:
            return False
        pattern = (
            rule.path_pattern.casefold() if request.platform == "windows" else rule.path_pattern
        )
        matches = [_path_glob_matches(path, pattern) for path in request.paths]
        # A deny/ask guard protects any matching target; an allow must cover every target.
        if rule.effect is PolicyEffect.ALLOW:
            return all(matches)
        return any(matches)
    return True


def _path_glob_matches(path: str, pattern: str) -> bool:
    regex_parts: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*" and index + 1 < len(pattern) and pattern[index + 1] == "*":
            if index + 2 < len(pattern) and pattern[index + 2] == "/":
                regex_parts.append("(?:.*/)?")
                index += 3
                continue
            if index > 0 and pattern[index - 1] == "/" and index + 2 == len(pattern):
                regex_parts[-1] = "(?:/.*)?"
                index += 2
                continue
            regex_parts.append(".*")
            index += 2
            continue
        if char == "*":
            regex_parts.append("[^/]*")
        elif char == "?":
            regex_parts.append("[^/]")
        else:
            regex_parts.append(re.escape(char))
        index += 1
    regex_parts.append("$")
    return re.fullmatch("".join(regex_parts), path) is not None


def _rule_specificity(rule: PermissionRule) -> int:
    patterns = tuple(
        pattern
        for pattern in (rule.tool_pattern, rule.path_pattern, rule.command_pattern)
        if pattern is not None
    )
    selector_count = len(patterns)
    exact_count = sum(not any(char in pattern for char in "*?") for pattern in patterns)
    literal_count = sum(sum(char not in "*?" for char in pattern) for pattern in patterns)
    wildcard_count = sum(sum(char in "*?" for char in pattern) for pattern in patterns)
    return (
        selector_count * 1_000_000
        + exact_count * 100_000
        + min(literal_count, 9_999) * 10
        - min(wildcard_count, 9)
    )


def _validated_scope_fingerprint(scope: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(scope, Mapping):
        raise PermissionPolicyValidationError("Approval session scope is invalid.")
    try:
        copied = dict(scope)
        scope_type = str(copied.get("type") or "")
        if scope_type not in SUPPORTED_APPROVAL_SCOPES:
            raise ValueError
        if scope_type == "exact_command_hash":
            if _SHA256_RE.fullmatch(str(copied.get("command_hash") or "")) is None:
                raise ValueError
        elif scope_type == "exact_file_set":
            if _SHA256_RE.fullmatch(str(copied.get("files_hash") or "")) is None:
                raise ValueError
            files = copied.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError
        elif scope_type == "exact_verify_command_set":
            if _SHA256_RE.fullmatch(str(copied.get("commands_hash") or "")) is None:
                raise ValueError
        else:
            if copied.get("backend_owned") is not True or not str(copied.get("safe_kind") or ""):
                raise ValueError
        encoded = json.dumps(
            copied,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise PermissionPolicyValidationError("Approval session scope is invalid.") from None
    return scope_type, hashlib.sha256(encoded).hexdigest()


def _session_safety_evaluation(reason: str, rule_id: str) -> PermissionEvaluation:
    return PermissionEvaluation(
        decision=PolicyEffect.ASK,
        reason=reason,
        matched_rule_id=rule_id,
        matched_rule_source="builtin_safety",
        specificity=2_147_483_647,
    )


__all__ = [
    "InMemorySessionGrantAdapter",
    "PERMISSION_POLICY_SCHEMA_VERSION",
    "PermissionEvaluation",
    "PermissionPolicy",
    "PermissionPolicyCorruptError",
    "PermissionPolicyError",
    "PermissionPolicyStore",
    "PermissionPolicyValidationError",
    "PermissionRequest",
    "PermissionRule",
    "PolicyEffect",
    "SessionPermissionGrant",
    "normalize_command_pattern",
    "normalize_path_pattern",
    "normalize_workspace_path",
]
