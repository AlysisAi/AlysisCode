from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from ..atomic_io import atomic_write_json
from .models import DiscoveredSkills, SkillBundle
from .paths import global_skills_state_path, project_skills_state_path

SkillInstallSourceKind = Literal["dir", "zip", "git", "scaffold"]


class SkillLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillLifecycleIssue:
    source_path: Path
    message: str


@dataclass(frozen=True)
class ManagedSkillRecord:
    name: str
    bundle_dir: str
    scope: Literal["project", "user"]
    source_kind: SkillInstallSourceKind | None = None
    source: str | None = None
    source_subdir: str | None = None
    source_commit: str | None = None
    installed_at: str | None = None
    family: str = "native"
    source_digest: str | None = None
    source_ref: str | None = None


@dataclass(frozen=True)
class SkillLifecycleState:
    managed_installs: dict[str, ManagedSkillRecord] = field(default_factory=dict)
    disabled_names: tuple[str, ...] = ()
    enabled_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillCatalogEntry:
    skill: SkillBundle
    enabled: bool
    managed: bool
    disabled_by: str | None = None
    install_record: ManagedSkillRecord | None = None


@dataclass(frozen=True)
class SkillCatalog:
    effective: DiscoveredSkills
    entries: tuple[SkillCatalogEntry, ...]
    global_state: SkillLifecycleState
    project_state: SkillLifecycleState
    lifecycle_issues: tuple[SkillLifecycleIssue, ...] = ()


def normalize_skill_name(raw: str) -> str:
    candidate = str(raw or "").strip().casefold()
    if not candidate:
        raise SkillLifecycleError("Skill name cannot be empty.")
    return candidate


def load_global_skill_state(*, user_config_dir: Path | None = None) -> SkillLifecycleState:
    return _load_state(global_skills_state_path(user_config_dir=user_config_dir))


def load_global_skill_state_tolerant(
    *,
    user_config_dir: Path | None = None,
) -> tuple[SkillLifecycleState, tuple[SkillLifecycleIssue, ...]]:
    return _load_state_tolerant(global_skills_state_path(user_config_dir=user_config_dir))


def save_global_skill_state(
    state: SkillLifecycleState,
    *,
    user_config_dir: Path | None = None,
) -> None:
    _save_state(global_skills_state_path(user_config_dir=user_config_dir), state)


def load_project_skill_state(workspace_root: Path) -> SkillLifecycleState:
    return _load_state(project_skills_state_path(workspace_root))


def load_project_skill_state_tolerant(
    workspace_root: Path,
) -> tuple[SkillLifecycleState, tuple[SkillLifecycleIssue, ...]]:
    return _load_state_tolerant(project_skills_state_path(workspace_root))


def save_project_skill_state(workspace_root: Path, state: SkillLifecycleState) -> None:
    _save_state(project_skills_state_path(workspace_root), state)


def with_managed_install(
    state: SkillLifecycleState,
    record: ManagedSkillRecord,
) -> SkillLifecycleState:
    managed = dict(state.managed_installs)
    managed[normalize_skill_name(record.name)] = record
    return SkillLifecycleState(
        managed_installs=dict(sorted(managed.items(), key=lambda item: item[0])),
        disabled_names=state.disabled_names,
        enabled_names=state.enabled_names,
    )


def without_managed_install(state: SkillLifecycleState, name: str) -> SkillLifecycleState:
    managed = dict(state.managed_installs)
    managed.pop(normalize_skill_name(name), None)
    return SkillLifecycleState(
        managed_installs=dict(sorted(managed.items(), key=lambda item: item[0])),
        disabled_names=tuple(
            item for item in state.disabled_names if item != normalize_skill_name(name)
        ),
        enabled_names=tuple(
            item for item in state.enabled_names if item != normalize_skill_name(name)
        ),
    )


def set_global_skill_disabled(
    state: SkillLifecycleState, *, name: str, disabled: bool
) -> SkillLifecycleState:
    key = normalize_skill_name(name)
    disabled_names = set(state.disabled_names)
    if disabled:
        disabled_names.add(key)
    else:
        disabled_names.discard(key)
    return SkillLifecycleState(
        managed_installs=state.managed_installs,
        disabled_names=tuple(sorted(disabled_names)),
        enabled_names=(),
    )


def set_project_skill_override(
    state: SkillLifecycleState,
    *,
    name: str,
    enabled: bool,
) -> SkillLifecycleState:
    key = normalize_skill_name(name)
    disabled_names = set(state.disabled_names)
    enabled_names = set(state.enabled_names)
    if enabled:
        disabled_names.discard(key)
        enabled_names.add(key)
    else:
        enabled_names.discard(key)
        disabled_names.add(key)
    return SkillLifecycleState(
        managed_installs=state.managed_installs,
        disabled_names=tuple(sorted(disabled_names)),
        enabled_names=tuple(sorted(enabled_names)),
    )


def resolve_skill_catalog(
    *,
    discovered: DiscoveredSkills,
    workspace_root: Path,
    user_config_dir: Path | None = None,
) -> SkillCatalog:
    global_state, global_issues = load_global_skill_state_tolerant(user_config_dir=user_config_dir)
    project_state, project_issues = load_project_skill_state_tolerant(workspace_root)
    entries: list[SkillCatalogEntry] = []
    effective_skills: dict[str, SkillBundle] = {}
    effective_ordered: list[SkillBundle] = []
    for skill in discovered.ordered:
        key = normalize_skill_name(skill.name)
        global_record = global_state.managed_installs.get(key)
        project_record = project_state.managed_installs.get(key)
        install_record = (
            project_record
            if skill.source_scope == "project"
            else global_record
            if skill.source_scope == "user"
            else None
        )
        enabled = True
        disabled_by: str | None = None
        if skill.source_scope in {"user", "bundled"} and key in global_state.disabled_names:
            enabled = False
            disabled_by = "global"
        if key in project_state.enabled_names:
            enabled = True
            disabled_by = None
        if key in project_state.disabled_names:
            enabled = False
            disabled_by = "project"
        entry = SkillCatalogEntry(
            skill=skill,
            enabled=enabled,
            managed=install_record is not None,
            disabled_by=disabled_by,
            install_record=install_record,
        )
        entries.append(entry)
        if enabled:
            effective_skills[key] = skill
            effective_ordered.append(skill)
    return SkillCatalog(
        effective=DiscoveredSkills(
            skills=dict(sorted(effective_skills.items(), key=lambda item: item[0])),
            ordered=tuple(effective_ordered),
            issues=discovered.issues,
        ),
        entries=tuple(entries),
        global_state=global_state,
        project_state=project_state,
        lifecycle_issues=(*global_issues, *project_issues),
    )


def resolve_skill_catalog_entry(
    *,
    entries: tuple[SkillCatalogEntry, ...],
    raw_name: str,
) -> SkillCatalogEntry | None:
    needle = str(raw_name or "").strip().casefold()
    if not needle:
        return None
    for entry in entries:
        if needle in entry.skill.lookup_keys():
            return entry
    return None


def _load_state(path: Path) -> SkillLifecycleState:
    if not path.exists():
        return SkillLifecycleState()
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillLifecycleError(f"Failed to read skills state: {path}") from exc
    except UnicodeDecodeError as exc:
        raise SkillLifecycleError(f"Invalid UTF-8 in skills state: {path}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SkillLifecycleError(f"Invalid JSON in skills state: {path}") from exc
    return _parse_state_payload(raw, path=path)


def _load_state_tolerant(path: Path) -> tuple[SkillLifecycleState, tuple[SkillLifecycleIssue, ...]]:
    if not path.exists():
        return SkillLifecycleState(), ()
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return SkillLifecycleState(), (
            SkillLifecycleIssue(
                source_path=path,
                message="Failed to read lifecycle state; using defaults.",
            ),
        )
    except UnicodeDecodeError:
        return SkillLifecycleState(), (
            SkillLifecycleIssue(
                source_path=path,
                message="Invalid UTF-8 in lifecycle state; using defaults.",
            ),
        )
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError:
        return SkillLifecycleState(), (
            SkillLifecycleIssue(
                source_path=path,
                message="Invalid JSON in lifecycle state; using defaults.",
            ),
        )
    return _parse_state_payload_tolerant(raw, path=path)


def resolve_managed_bundle_path(*, managed_root: Path, bundle_dir: str) -> Path:
    safe_dir = _validated_bundle_dir_name(bundle_dir)
    resolved_root = managed_root.expanduser().resolve()
    target = (resolved_root / safe_dir).resolve()
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise SkillLifecycleError(
            f"Managed skill bundle escapes managed root: {bundle_dir!r}"
        ) from exc
    return target


def _parse_state_payload(raw: object, *, path: Path) -> SkillLifecycleState:
    if not isinstance(raw, dict):
        raise SkillLifecycleError(f"Invalid skills state payload: {path}")
    installs_raw = raw.get("managed_installs", {})
    if not isinstance(installs_raw, dict):
        raise SkillLifecycleError(f"Invalid managed_installs payload in skills state: {path}")
    installs, _issues = _parse_managed_installs(installs_raw, path=path, tolerant=False)
    return SkillLifecycleState(
        managed_installs=dict(sorted(installs.items(), key=lambda item: item[0])),
        disabled_names=_normalized_name_tuple(raw.get("disabled_names")),
        enabled_names=_normalized_name_tuple(raw.get("enabled_names")),
    )


def _parse_state_payload_tolerant(
    raw: object,
    *,
    path: Path,
) -> tuple[SkillLifecycleState, tuple[SkillLifecycleIssue, ...]]:
    if not isinstance(raw, dict):
        return SkillLifecycleState(), (
            SkillLifecycleIssue(
                source_path=path,
                message="Invalid lifecycle state payload; using defaults.",
            ),
        )
    installs_raw = raw.get("managed_installs", {})
    issues: list[SkillLifecycleIssue] = []
    installs: dict[str, ManagedSkillRecord] = {}
    if not isinstance(installs_raw, dict):
        issues.append(
            SkillLifecycleIssue(
                source_path=path,
                message="Invalid managed_installs payload in lifecycle state; ignoring it.",
            )
        )
    else:
        installs, install_issues = _parse_managed_installs(installs_raw, path=path, tolerant=True)
        issues.extend(install_issues)
    return (
        SkillLifecycleState(
            managed_installs=dict(sorted(installs.items(), key=lambda item: item[0])),
            disabled_names=_normalized_name_tuple(raw.get("disabled_names")),
            enabled_names=_normalized_name_tuple(raw.get("enabled_names")),
        ),
        tuple(issues),
    )


def _parse_managed_installs(
    installs_raw: dict[object, object],
    *,
    path: Path,
    tolerant: bool,
) -> tuple[dict[str, ManagedSkillRecord], tuple[SkillLifecycleIssue, ...]]:
    installs: dict[str, ManagedSkillRecord] = {}
    issues: list[SkillLifecycleIssue] = []
    for key, payload in installs_raw.items():
        if not isinstance(payload, dict):
            if tolerant:
                issues.append(
                    SkillLifecycleIssue(
                        source_path=path,
                        message=f"Skipping invalid managed skill record {key!r}; expected an object.",
                    )
                )
                continue
            raise SkillLifecycleError(f"Invalid managed skill record in skills state: {path}")
        try:
            normalized = normalize_skill_name(str(payload.get("name") or key))
            bundle_dir = _validated_bundle_dir_name(payload.get("bundle_dir") or normalized)
        except SkillLifecycleError as exc:
            if tolerant:
                issues.append(
                    SkillLifecycleIssue(
                        source_path=path,
                        message=f"Skipping invalid managed skill record {key!r}: {exc}",
                    )
                )
                continue
            raise SkillLifecycleError(f"{exc} ({path})") from exc
        installs[normalized] = ManagedSkillRecord(
            name=str(payload.get("name") or normalized),
            bundle_dir=bundle_dir,
            scope=("project" if str(payload.get("scope") or "") == "project" else "user"),
            source_kind=_optional_source_kind(payload.get("source_kind")),
            source=str(payload.get("source") or "").strip() or None,
            source_subdir=str(payload.get("source_subdir") or "").strip() or None,
            source_commit=str(payload.get("source_commit") or "").strip() or None,
            installed_at=str(payload.get("installed_at") or "").strip() or None,
            family=str(payload.get("family") or "native").strip() or "native",
            source_digest=str(payload.get("source_digest") or "").strip() or None,
            source_ref=str(payload.get("source_ref") or "").strip() or None,
        )
    return installs, tuple(issues)


def _save_state(path: Path, state: SkillLifecycleState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "managed_installs": {
            name: {
                "name": record.name,
                "bundle_dir": record.bundle_dir,
                "scope": record.scope,
                "source_kind": record.source_kind,
                "source": record.source,
                "source_subdir": record.source_subdir,
                "source_commit": record.source_commit,
                "installed_at": record.installed_at,
                "family": record.family,
                "source_digest": record.source_digest,
                "source_ref": record.source_ref,
            }
            for name, record in sorted(state.managed_installs.items(), key=lambda item: item[0])
        },
        "disabled_names": list(state.disabled_names),
        "enabled_names": list(state.enabled_names),
    }
    atomic_write_json(path, payload)


def _normalized_name_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        candidate = str(item or "").strip().casefold()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return tuple(sorted(out))


def _optional_source_kind(raw: object) -> SkillInstallSourceKind | None:
    candidate = str(raw or "").strip().lower()
    if candidate in {"dir", "zip", "git", "scaffold"}:
        return candidate  # type: ignore[return-value]
    return None


def _validated_bundle_dir_name(raw: object) -> str:
    candidate = str(raw or "").strip()
    if not candidate:
        raise SkillLifecycleError("Managed skill record is missing bundle_dir.")
    pure = PurePosixPath(candidate.replace("\\", "/"))
    parts = [part for part in pure.parts if part not in {"", "."}]
    if pure.is_absolute() or any(part == ".." for part in parts) or len(parts) != 1:
        raise SkillLifecycleError(f"Managed skill record has invalid bundle_dir: {candidate!r}")
    return parts[0]
