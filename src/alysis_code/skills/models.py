from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SkillSourceScope = Literal["project", "user", "bundled"]
SkillSourceKind = Literal["native", "interop", "bundled"]
SkillTrustLevel = Literal["untrusted"]


@dataclass(frozen=True)
class SkillBundle:
    name: str
    description: str
    instructions: str
    bundle_name: str
    bundle_path: Path
    entry_path: Path
    source_scope: SkillSourceScope
    source_kind: SkillSourceKind
    source_family: str
    source_path: Path
    trust_level: SkillTrustLevel
    ancestor_distance: int | None = None
    aliases: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    #: Tools the skill's instructions tell the model to call (``uses_tools`` in SKILL.md).
    uses_tools: tuple[str, ...] = ()

    @property
    def source_label(self) -> str:
        return f"{self.source_scope}/{self.source_kind}"

    def usable_with(self, tool_names: Collection[str]) -> bool:
        """Whether a session with these tools can follow this skill: it has every declared one."""
        return all(name in tool_names for name in self.uses_tools)

    def lookup_keys(self) -> tuple[str, ...]:
        keys: list[str] = []
        for value in (self.name, self.bundle_name, *self.aliases):
            candidate = str(value or "").strip()
            if not candidate:
                continue
            lowered = candidate.casefold()
            if lowered in keys:
                continue
            keys.append(lowered)
        return tuple(keys)


@dataclass(frozen=True)
class SkillDiscoveryIssue:
    source_path: Path
    message: str


@dataclass(frozen=True)
class DiscoveredSkills:
    skills: dict[str, SkillBundle]
    ordered: tuple[SkillBundle, ...]
    issues: tuple[SkillDiscoveryIssue, ...] = ()


@dataclass(frozen=True)
class SkillMatch:
    skill: SkillBundle
    score: int
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConventionDocument:
    name: str
    path: Path
    content: str
    trust_level: SkillTrustLevel = "untrusted"
