from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from ..frontmatter_utils import split_frontmatter
from .prompting import (
    EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS,
    EXPLICIT_SKILL_DESCRIPTION_DISPLAY_MAX_CHARS,
    EXPLICIT_SKILL_ENTRYPOINT_MAX_CHARS,
    EXPLICIT_SKILL_NAME_DISPLAY_MAX_CHARS,
)

SKILL_ENTRYPOINT_WARNING_CHARS = EXPLICIT_SKILL_ENTRYPOINT_MAX_CHARS
SKILL_NAME_WARNING_CHARS = EXPLICIT_SKILL_NAME_DISPLAY_MAX_CHARS
SKILL_DESCRIPTION_WARNING_CHARS = EXPLICIT_SKILL_DESCRIPTION_DISPLAY_MAX_CHARS


@dataclass(frozen=True)
class SkillValidationIssue:
    severity: Literal["error", "warning"]
    message: str
    path: Path


@dataclass(frozen=True)
class SkillValidationResult:
    bundle_path: Path
    entry_path: Path
    valid: bool
    name: str = ""
    description: str = ""
    instructions: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    issues: tuple[SkillValidationIssue, ...] = ()

    @property
    def errors(self) -> tuple[SkillValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[SkillValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")


def validate_skill_bundle(bundle_path: Path) -> SkillValidationResult:
    """Validate a skill bundle without allowing filesystem faults to escape.

    Skill directories are untrusted workspace input.  On Windows in particular,
    even metadata probes such as ``is_symlink()`` can raise ``PermissionError``
    for a deny-ACL directory.  Discovery must fail that one bundle closed instead
    of making session creation fail.
    """

    try:
        return _validate_skill_bundle(bundle_path)
    except OSError as exc:
        try:
            reported_bundle = bundle_path.expanduser().absolute()
        except OSError:
            reported_bundle = bundle_path
        entry_path = reported_bundle / "SKILL.md"
        issue = SkillValidationIssue(
            severity="error",
            message=f"Failed to inspect skill bundle: {exc}",
            path=reported_bundle,
        )
        return SkillValidationResult(
            bundle_path=reported_bundle,
            entry_path=entry_path,
            valid=False,
            issues=(issue,),
        )


def _validate_skill_bundle(bundle_path: Path) -> SkillValidationResult:
    resolved_bundle = bundle_path.expanduser().resolve()
    entry_path = resolved_bundle / "SKILL.md"
    issues: list[SkillValidationIssue] = []
    if not resolved_bundle.exists() or not resolved_bundle.is_dir():
        issues.append(
            SkillValidationIssue(
                severity="error",
                message="Skill bundle path is not a directory.",
                path=resolved_bundle,
            )
        )
        return SkillValidationResult(
            bundle_path=resolved_bundle,
            entry_path=entry_path,
            valid=False,
            issues=tuple(issues),
        )
    if entry_path.is_symlink():
        issues.append(
            SkillValidationIssue(
                severity="error",
                message="SKILL.md may not be a symlink.",
                path=entry_path,
            )
        )
    if not entry_path.exists() or not entry_path.is_file():
        issues.append(
            SkillValidationIssue(
                severity="error",
                message="SKILL.md is missing.",
                path=entry_path,
            )
        )
        return SkillValidationResult(
            bundle_path=resolved_bundle,
            entry_path=entry_path,
            valid=False,
            issues=tuple(issues),
        )
    try:
        raw_text = entry_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        issues.append(
            SkillValidationIssue(
                severity="error",
                message="SKILL.md is not valid UTF-8 text.",
                path=entry_path,
            )
        )
        return SkillValidationResult(
            bundle_path=resolved_bundle,
            entry_path=entry_path,
            valid=False,
            issues=tuple(issues),
        )
    except OSError as exc:
        issues.append(
            SkillValidationIssue(
                severity="error",
                message=f"Failed to read SKILL.md: {exc}",
                path=entry_path,
            )
        )
        return SkillValidationResult(
            bundle_path=resolved_bundle,
            entry_path=entry_path,
            valid=False,
            issues=tuple(issues),
        )

    frontmatter, body = split_frontmatter(raw_text)
    if frontmatter is None:
        issues.append(
            SkillValidationIssue(
                severity="error",
                message="missing YAML-style frontmatter in SKILL.md",
                path=entry_path,
            )
        )
        return SkillValidationResult(
            bundle_path=resolved_bundle,
            entry_path=entry_path,
            valid=False,
            issues=tuple(issues),
        )

    try:
        if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(frontmatter)):
            raise ValueError("Expand YAML aliases before importing this skill.")
        meta = yaml.safe_load(frontmatter)
        if not isinstance(meta, dict) or any(not isinstance(key, str) for key in meta):
            raise ValueError("Frontmatter must be a mapping with string keys.")
        for key in ("name", "description"):
            if key in meta and not isinstance(meta[key], str):
                raise ValueError(f"Frontmatter {key} must be text.")
        if "aliases" in meta and (
            not isinstance(meta["aliases"], list)
            or any(not isinstance(value, str) for value in meta["aliases"])
        ):
            raise ValueError("Frontmatter aliases must be a list of strings.")
        # YAML dates are metadata, not runtime objects; recursive aliases are invalid.
        meta = json.loads(json.dumps(meta, default=str))
    except (yaml.YAMLError, ValueError) as exc:
        issues.append(
            SkillValidationIssue(
                severity="error", message=f"Invalid skill frontmatter: {exc}", path=entry_path
            )
        )
        return SkillValidationResult(
            bundle_path=resolved_bundle, entry_path=entry_path, valid=False, issues=tuple(issues)
        )
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    instructions = body.strip()
    if not name:
        issues.append(
            SkillValidationIssue(
                severity="error",
                message="Missing required frontmatter field: name.",
                path=entry_path,
            )
        )
    if not description:
        issues.append(
            SkillValidationIssue(
                severity="error",
                message="Missing required frontmatter field: description.",
                path=entry_path,
            )
        )
    if not instructions:
        issues.append(
            SkillValidationIssue(
                severity="error",
                message="SKILL.md body/instructions are empty.",
                path=entry_path,
            )
        )
    if name and len(name) > SKILL_NAME_WARNING_CHARS:
        issues.append(
            SkillValidationIssue(
                severity="warning",
                message=(
                    "Skill name is unusually large "
                    f"(>{SKILL_NAME_WARNING_CHARS:,} chars); explicit /skill metadata display is "
                    "bounded and may truncate the attached name preview."
                ),
                path=entry_path,
            )
        )
    if description and len(description) > SKILL_DESCRIPTION_WARNING_CHARS:
        issues.append(
            SkillValidationIssue(
                severity="warning",
                message=(
                    "Skill description is unusually large "
                    f"(>{SKILL_DESCRIPTION_WARNING_CHARS:,} chars); explicit /skill metadata display "
                    "is bounded and may truncate the attached description preview."
                ),
                path=entry_path,
            )
        )
    if len(raw_text) > SKILL_ENTRYPOINT_WARNING_CHARS:
        issues.append(
            SkillValidationIssue(
                severity="warning",
                message=(
                    "SKILL.md entrypoint is unusually large "
                    f"(>{SKILL_ENTRYPOINT_WARNING_CHARS:,} chars); explicit /skill context is "
                    f"bounded to a {EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS:,}-char wrapper and may "
                    "truncate the attached preview."
                ),
                path=entry_path,
            )
        )
    for optional_name in ("references", "scripts", "assets"):
        optional_path = resolved_bundle / optional_name
        if optional_path.is_symlink():
            issues.append(
                SkillValidationIssue(
                    severity="error",
                    message=f"{optional_name}/ may not be a symlink.",
                    path=optional_path,
                )
            )
        elif optional_path.exists() and not optional_path.is_dir():
            issues.append(
                SkillValidationIssue(
                    severity="error",
                    message=f"{optional_name}/ must be a directory when present.",
                    path=optional_path,
                )
            )
    for candidate in resolved_bundle.rglob("*"):
        if candidate == entry_path:
            continue
        if candidate.is_symlink():
            issues.append(
                SkillValidationIssue(
                    severity="error",
                    message="Skill bundles may not contain symlinks.",
                    path=candidate,
                )
            )
            break
    total_bytes = 0
    file_count = 0
    for candidate in resolved_bundle.rglob("*"):
        if not candidate.is_file():
            continue
        file_count += 1
        try:
            total_bytes += candidate.stat().st_size
        except OSError:
            continue
    if total_bytes > 5 * 1024 * 1024:
        issues.append(
            SkillValidationIssue(
                severity="warning",
                message="Skill bundle is unusually large (>5 MiB).",
                path=resolved_bundle,
            )
        )
    if file_count > 256:
        issues.append(
            SkillValidationIssue(
                severity="warning",
                message="Skill bundle contains an unusually high file count (>256 files).",
                path=resolved_bundle,
            )
        )

    return SkillValidationResult(
        bundle_path=resolved_bundle,
        entry_path=entry_path,
        valid=not any(issue.severity == "error" for issue in issues),
        name=name,
        description=description,
        instructions=instructions,
        metadata=meta,
        issues=tuple(issues),
    )
