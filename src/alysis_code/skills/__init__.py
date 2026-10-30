from importlib import import_module
from typing import TYPE_CHECKING

from .conventions import load_repo_conventions, render_repo_conventions_context
from .discovery import (
    discover_skills,
    resolve_bundled_skills_enabled,
    resolve_skill_by_name,
    resolve_skills_enabled,
)
from .loader import SkillReadError, read_skill_bundle_file
from .matching import match_skills
from .models import (
    ConventionDocument,
    DiscoveredSkills,
    SkillBundle,
    SkillDiscoveryIssue,
    SkillMatch,
)
from .paths import (
    bundled_skill_root,
    global_managed_skill_root,
    global_skills_state_path,
    project_managed_skill_root,
    project_skill_root_for_family,
    project_skills_state_path,
    skill_bundle_dir_name,
)
from .prompting import (
    build_explicit_skill_context_message,
    build_matched_skill_context,
    build_skill_advertise_block,
    render_skill_info_text,
)
from .state import (
    ManagedSkillRecord,
    SkillCatalog,
    SkillCatalogEntry,
    SkillLifecycleError,
    SkillLifecycleIssue,
    SkillLifecycleState,
    load_global_skill_state,
    load_global_skill_state_tolerant,
    load_project_skill_state,
    load_project_skill_state_tolerant,
    resolve_managed_bundle_path,
    resolve_skill_catalog,
    resolve_skill_catalog_entry,
    save_global_skill_state,
    save_project_skill_state,
    set_global_skill_disabled,
    set_project_skill_override,
)
from .validation import SkillValidationIssue, SkillValidationResult, validate_skill_bundle

if TYPE_CHECKING:
    from .install import (
        SkillInstallResult,
        SkillRemoveResult,
        install_skill_bundle,
        remove_managed_skill,
    )
    from .scaffold import SkillScaffoldResult, scaffold_skill_bundle


# Discovery and prompt construction do not install or scaffold bundles.
# Keep the existing public API while loading these implementations on first use.
_MANAGEMENT_EXPORTS = {
    "SkillInstallResult": "install",
    "SkillRemoveResult": "install",
    "install_skill_bundle": "install",
    "remove_managed_skill": "install",
    "SkillScaffoldResult": "scaffold",
    "scaffold_skill_bundle": "scaffold",
}


def __getattr__(name: str):
    try:
        module_name = _MANAGEMENT_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | _MANAGEMENT_EXPORTS.keys())


__all__ = [
    "ConventionDocument",
    "DiscoveredSkills",
    "ManagedSkillRecord",
    "SkillBundle",
    "SkillCatalog",
    "SkillCatalogEntry",
    "SkillDiscoveryIssue",
    "SkillInstallResult",
    "SkillLifecycleError",
    "SkillLifecycleIssue",
    "SkillLifecycleState",
    "SkillMatch",
    "SkillReadError",
    "SkillRemoveResult",
    "SkillScaffoldResult",
    "SkillValidationIssue",
    "SkillValidationResult",
    "build_explicit_skill_context_message",
    "build_matched_skill_context",
    "build_skill_advertise_block",
    "bundled_skill_root",
    "discover_skills",
    "global_managed_skill_root",
    "global_skills_state_path",
    "install_skill_bundle",
    "load_global_skill_state",
    "load_global_skill_state_tolerant",
    "load_project_skill_state",
    "load_project_skill_state_tolerant",
    "load_repo_conventions",
    "match_skills",
    "project_managed_skill_root",
    "project_skill_root_for_family",
    "project_skills_state_path",
    "read_skill_bundle_file",
    "render_repo_conventions_context",
    "render_skill_info_text",
    "remove_managed_skill",
    "resolve_bundled_skills_enabled",
    "resolve_managed_bundle_path",
    "resolve_skill_by_name",
    "resolve_skill_catalog",
    "resolve_skill_catalog_entry",
    "resolve_skills_enabled",
    "save_global_skill_state",
    "save_project_skill_state",
    "scaffold_skill_bundle",
    "set_global_skill_disabled",
    "set_project_skill_override",
    "skill_bundle_dir_name",
    "validate_skill_bundle",
]
