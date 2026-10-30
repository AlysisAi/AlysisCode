from __future__ import annotations

from pathlib import Path

from ..branding import canonical_user_config_dir
from ..config import AppConfig
from .loader import load_skill_bundle
from .models import DiscoveredSkills, SkillBundle, SkillDiscoveryIssue
from .paths import bundled_skill_root

_PROJECT_SKILL_ROOT_SPECS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("native", (".alysis_skills",), ".alysis_skills"),
    ("interop", (".agents", "skills"), ".agents/skills"),
    ("interop", (".claude", "skills"), ".claude/skills"),
    ("interop", (".github", "skills"), ".github/skills"),
)
_USER_SKILL_ROOT_SPECS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("native", ("skills",), "skills"),
    ("interop", (".config", "agents", "skills"), ".config/agents/skills"),
    ("interop", (".claude", "skills"), ".claude/skills"),
    ("interop", (".copilot", "skills"), ".copilot/skills"),
)


def project_skill_root_relative_paths() -> tuple[tuple[str, ...], ...]:
    return tuple(parts for _, parts, _ in _PROJECT_SKILL_ROOT_SPECS)


def resolve_skills_enabled(cfg: AppConfig | None) -> bool:
    if cfg is None:
        return True
    return bool(getattr(cfg, "skills_enabled", True))


def resolve_bundled_skills_enabled(cfg: AppConfig | None) -> bool:
    if cfg is None:
        return True
    return bool(getattr(cfg, "bundled_skills_enabled", True))


def discover_skills(
    *,
    focus_path: Path,
    workspace_root: Path | None = None,
    user_config_dir: Path | None = None,
    home_dir: Path | None = None,
    cfg: AppConfig | None = None,
) -> DiscoveredSkills:
    resolved_focus = focus_path.expanduser().resolve()
    if resolved_focus.is_file():
        resolved_focus = resolved_focus.parent
    resolved_workspace_root = (
        workspace_root.expanduser().resolve() if workspace_root is not None else resolved_focus
    )
    if resolved_workspace_root.is_file():
        resolved_workspace_root = resolved_workspace_root.parent
    project_ancestors = _project_search_ancestors(
        focus_path=resolved_focus,
        workspace_root=resolved_workspace_root,
    )
    issues: list[SkillDiscoveryIssue] = []
    resolved: dict[str, SkillBundle] = {}
    ordered: list[SkillBundle] = []

    def _consider(skill: SkillBundle) -> None:
        primary_key = skill.name.casefold()
        if primary_key in resolved:
            return
        resolved[primary_key] = skill
        ordered.append(skill)

    for ancestor_distance, ancestor_path in project_ancestors:
        for source_kind, parts, source_family in _PROJECT_SKILL_ROOT_SPECS:
            root = ancestor_path.joinpath(*parts)
            try:
                if not root.exists() or not root.is_dir():
                    continue
                bundle_paths = sorted(child for child in root.iterdir() if child.is_dir())
            except OSError as exc:
                issues.append(
                    SkillDiscoveryIssue(
                        source_path=root,
                        message=f"failed to scan skill root: {exc}",
                    )
                )
                continue
            for bundle_path in bundle_paths:
                skill, issue = load_skill_bundle(
                    bundle_path=bundle_path,
                    source_scope="project",
                    source_kind="native" if source_kind == "native" else "interop",
                    source_family=source_family,
                    ancestor_distance=ancestor_distance,
                )
                if issue is not None:
                    issues.append(issue)
                    continue
                assert skill is not None
                if skill.name.casefold() in resolved:
                    continue
                _consider(skill)

    canonical_user_root = (
        user_config_dir.expanduser().resolve()
        if user_config_dir is not None
        else canonical_user_config_dir().resolve()
    )
    resolved_home_dir = (
        home_dir.expanduser().resolve() if home_dir is not None else Path.home().resolve()
    )
    for source_kind, parts, source_family in _USER_SKILL_ROOT_SPECS:
        base_root = canonical_user_root if source_kind == "native" else resolved_home_dir
        root = base_root.joinpath(*parts)
        try:
            if not root.exists() or not root.is_dir():
                continue
            bundle_paths = sorted(child for child in root.iterdir() if child.is_dir())
        except OSError as exc:
            issues.append(
                SkillDiscoveryIssue(
                    source_path=root,
                    message=f"failed to scan skill root: {exc}",
                )
            )
            continue
        for bundle_path in bundle_paths:
            skill, issue = load_skill_bundle(
                bundle_path=bundle_path,
                source_scope="user",
                source_kind="native" if source_kind == "native" else "interop",
                source_family=source_family,
                ancestor_distance=None,
            )
            if issue is not None:
                issues.append(issue)
                continue
            assert skill is not None
            if skill.name.casefold() in resolved:
                continue
            _consider(skill)

    if resolve_bundled_skills_enabled(cfg):
        root = bundled_skill_root()
        try:
            bundle_paths = (
                sorted(child for child in root.iterdir() if child.is_dir())
                if root.exists() and root.is_dir()
                else []
            )
        except OSError as exc:
            issues.append(
                SkillDiscoveryIssue(
                    source_path=root,
                    message=f"failed to scan bundled skill root: {exc}",
                )
            )
            bundle_paths = []
        for bundle_path in bundle_paths:
            from ..computer_use_platform_policy import computer_use_blocked

            if bundle_path.name == "computer-use" and computer_use_blocked():
                continue
            skill, issue = load_skill_bundle(
                bundle_path=bundle_path,
                source_scope="bundled",
                source_kind="bundled",
                source_family="bundled",
                ancestor_distance=None,
            )
            if issue is not None:
                issues.append(issue)
                continue
            assert skill is not None
            _consider(skill)

    return DiscoveredSkills(
        skills=dict(sorted(resolved.items(), key=lambda item: item[0])),
        ordered=tuple(ordered),
        issues=tuple(issues),
    )


def resolve_skill_by_name(
    skill_registry: dict[str, SkillBundle] | None,
    raw_name: str,
) -> SkillBundle | None:
    if not skill_registry:
        return None
    candidate = str(raw_name or "").strip()
    if not candidate:
        return None
    lowered = candidate.casefold()
    direct = skill_registry.get(lowered)
    if direct is not None:
        return direct
    for skill in skill_registry.values():
        if lowered in skill.lookup_keys():
            return skill
    return None


def _project_search_ancestors(
    *,
    focus_path: Path,
    workspace_root: Path,
) -> list[tuple[int, Path]]:
    ancestors: list[tuple[int, Path]] = []
    current = focus_path
    distance = 0
    while True:
        ancestors.append((distance, current))
        if current == workspace_root:
            break
        if workspace_root not in current.parents:
            break
        current = current.parent
        distance += 1
    return ancestors
