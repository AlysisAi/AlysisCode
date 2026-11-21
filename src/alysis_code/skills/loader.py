from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import SkillBundle, SkillDiscoveryIssue, SkillSourceKind, SkillSourceScope
from .validation import validate_skill_bundle


class SkillReadError(RuntimeError):
    pass


def load_skill_bundle(
    *,
    bundle_path: Path,
    source_scope: SkillSourceScope,
    source_kind: SkillSourceKind,
    source_family: str,
    ancestor_distance: int | None,
) -> tuple[SkillBundle | None, SkillDiscoveryIssue | None]:
    validation = validate_skill_bundle(bundle_path)
    if not validation.valid:
        first_error = next(iter(validation.errors))
        return None, SkillDiscoveryIssue(
            source_path=bundle_path,
            message=first_error.message,
        )

    bundle_name = bundle_path.name.strip() or validation.name
    aliases = _ordered_unique_strings(
        [validation.name, bundle_name, *_metadata_aliases(validation.metadata)]
    )
    return (
        SkillBundle(
            name=validation.name,
            description=validation.description,
            instructions=validation.instructions,
            bundle_name=bundle_name,
            bundle_path=bundle_path,
            entry_path=validation.entry_path,
            source_scope=source_scope,
            source_kind=source_kind,
            source_family=source_family,
            source_path=bundle_path,
            trust_level="untrusted",
            ancestor_distance=ancestor_distance,
            aliases=tuple(aliases),
            metadata={
                key: str(value)
                for key, value in validation.metadata.items()
                if key in {"name", "description"} and str(value).strip()
            },
            uses_tools=tuple(dict.fromkeys(validation.metadata.get("uses_tools") or ())),
        ),
        None,
    )


def read_skill_bundle_file(skill: SkillBundle, path: str | None = None) -> dict[str, Any]:
    requested_path = str(path or "").strip()
    if not requested_path:
        target_path = skill.entry_path
        relative_path = "SKILL.md"
    else:
        candidate = Path(requested_path)
        if candidate.is_absolute():
            raise SkillReadError("skill_read path must be relative to the skill bundle root")
        target_path = (skill.bundle_path / candidate).resolve()
        bundle_root = skill.bundle_path.resolve()
        try:
            target_path.relative_to(bundle_root)
        except ValueError as exc:
            raise SkillReadError("skill_read path escapes the skill bundle root") from exc
        try:
            relative_path = target_path.relative_to(bundle_root).as_posix()
        except ValueError as exc:
            raise SkillReadError("skill_read path escapes the skill bundle root") from exc
    if not target_path.exists() or not target_path.is_file():
        raise SkillReadError(f"Skill file not found: {requested_path or 'SKILL.md'}")
    try:
        content = target_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillReadError(f"Skill file is not readable as UTF-8 text: {relative_path}") from exc
    except OSError as exc:
        raise SkillReadError(f"Failed to read skill file: {relative_path}") from exc
    return {
        "name": skill.name,
        "bundle_name": skill.bundle_name,
        "path": relative_path,
        "content": content,
        "source_scope": skill.source_scope,
        "source_kind": skill.source_kind,
        "source_family": skill.source_family,
        "source_path": skill.source_path.as_posix(),
        "trust_level": skill.trust_level,
    }


def _ordered_unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        lowered = value.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(value)
    return out


def _metadata_aliases(metadata: dict[str, Any]) -> list[str]:
    raw = metadata.get("aliases")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []
