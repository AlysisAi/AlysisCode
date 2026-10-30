from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import strftime
from urllib.parse import urlsplit

from ..installer_git import run_installer_git
from .paths import global_managed_skill_root, project_managed_skill_root, skill_bundle_dir_name
from .state import (
    ManagedSkillRecord,
    SkillLifecycleError,
    load_global_skill_state,
    load_project_skill_state,
    resolve_managed_bundle_path,
    save_global_skill_state,
    save_project_skill_state,
    with_managed_install,
    without_managed_install,
)
from .transactions import commit_managed_bundle_removal, commit_managed_bundle_update
from .validation import SkillValidationResult, validate_skill_bundle

MAX_SKILL_ZIP_FILES = 256
MAX_SKILL_ZIP_UNCOMPRESSED_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class SkillInstallResult:
    bundle_path: Path
    installed_name: str
    source_kind: str
    source_commit: str | None = None
    validation: SkillValidationResult | None = None


@dataclass(frozen=True)
class SkillRemoveResult:
    bundle_path: Path | None
    removed_name: str
    scope: str


def install_skill_bundle(
    *,
    source: str,
    workspace_root: Path,
    project: bool = False,
    subdir: str | None = None,
    force: bool = False,
    user_config_dir: Path | None = None,
) -> SkillInstallResult:
    with tempfile.TemporaryDirectory(prefix="alysis-skill-install-") as temp_dir:
        temp_root = Path(temp_dir)
        source_kind, source_root, source_commit = _materialize_install_source(
            source=source,
            temp_root=temp_root,
        )
        bundle = _resolve_install_bundle(source_root=source_root, subdir=subdir)
        validation = validate_skill_bundle(bundle)
        if not validation.valid:
            raise SkillLifecycleError(_format_validation_failure(validation))
        target_root = (
            project_managed_skill_root(workspace_root)
            if project
            else global_managed_skill_root(user_config_dir=user_config_dir)
        )
        bundle_dir = skill_bundle_dir_name(validation.name)
        target_path = resolve_managed_bundle_path(managed_root=target_root, bundle_dir=bundle_dir)
        record = ManagedSkillRecord(
            name=validation.name,
            bundle_dir=bundle_dir,
            scope=("project" if project else "user"),
            source_kind=source_kind,
            source=source,
            source_subdir=(str(subdir).strip() or None) if subdir is not None else None,
            source_commit=source_commit,
            installed_at=strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        if project:
            state = with_managed_install(load_project_skill_state(workspace_root), record)
            commit_managed_bundle_update(
                target_path=target_path,
                force=force,
                stage_bundle=lambda staged: shutil.copytree(bundle, staged),
                persist_state=lambda: save_project_skill_state(workspace_root, state),
            )
        else:
            state = with_managed_install(
                load_global_skill_state(user_config_dir=user_config_dir),
                record,
            )
            commit_managed_bundle_update(
                target_path=target_path,
                force=force,
                stage_bundle=lambda staged: shutil.copytree(bundle, staged),
                persist_state=lambda: save_global_skill_state(
                    state,
                    user_config_dir=user_config_dir,
                ),
            )
        return SkillInstallResult(
            bundle_path=target_path,
            installed_name=validation.name,
            source_kind=source_kind,
            source_commit=source_commit,
            validation=validation,
        )


def remove_managed_skill(
    *,
    name: str,
    workspace_root: Path,
    project: bool = False,
    user_config_dir: Path | None = None,
) -> SkillRemoveResult:
    key = str(name or "").strip().casefold()
    if not key:
        raise SkillLifecycleError("Skill name cannot be empty.")
    if project:
        state = load_project_skill_state(workspace_root)
        record = state.managed_installs.get(key)
        if record is None:
            raise SkillLifecycleError(f"Managed project skill not found: {name}")
        bundle_path = resolve_managed_bundle_path(
            managed_root=project_managed_skill_root(workspace_root),
            bundle_dir=record.bundle_dir,
        )
        commit_managed_bundle_removal(
            target_path=bundle_path,
            persist_state=lambda: save_project_skill_state(
                workspace_root,
                without_managed_install(state, name),
            ),
        )
        return SkillRemoveResult(bundle_path=bundle_path, removed_name=record.name, scope="project")
    state = load_global_skill_state(user_config_dir=user_config_dir)
    record = state.managed_installs.get(key)
    if record is None:
        raise SkillLifecycleError(f"Managed user skill not found: {name}")
    bundle_path = resolve_managed_bundle_path(
        managed_root=global_managed_skill_root(user_config_dir=user_config_dir),
        bundle_dir=record.bundle_dir,
    )
    commit_managed_bundle_removal(
        target_path=bundle_path,
        persist_state=lambda: save_global_skill_state(
            without_managed_install(state, name),
            user_config_dir=user_config_dir,
        ),
    )
    return SkillRemoveResult(bundle_path=bundle_path, removed_name=record.name, scope="user")


def _materialize_install_source(
    *,
    source: str,
    temp_root: Path,
) -> tuple[str, Path, str | None]:
    raw_source = str(source or "").strip()
    if not raw_source:
        raise SkillLifecycleError("Install source cannot be empty.")
    source_path = Path(raw_source).expanduser()
    if source_path.exists() and source_path.is_dir():
        return "dir", source_path.resolve(), None
    if source_path.exists() and source_path.is_file() and source_path.suffix.casefold() == ".zip":
        extract_root = temp_root / "zip"
        extract_root.mkdir(parents=True, exist_ok=True)
        _extract_skill_zip(source_path.resolve(), extract_root)
        return "zip", extract_root, None
    return _clone_git_source(raw_source, temp_root / "git")


def _resolve_install_bundle(*, source_root: Path, subdir: str | None) -> Path:
    root = source_root.resolve()
    if subdir:
        candidate = (root / Path(subdir)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise SkillLifecycleError("Install subdir escapes the source root.") from exc
        root = candidate
    if (root / "SKILL.md").is_file():
        return root
    candidates: list[Path] = []
    for entry in root.rglob("SKILL.md"):
        if ".git" in entry.parts:
            continue
        candidates.append(entry.parent)
    if not candidates:
        raise SkillLifecycleError("No skill bundle found in install source.")
    if len(candidates) > 1:
        raise SkillLifecycleError(
            "Install source contains multiple skill bundles; use --subdir to choose one."
        )
    return candidates[0]


def _extract_skill_zip(zip_path: Path, target_root: Path) -> None:
    try:
        archive = zipfile.ZipFile(zip_path)
    except OSError as exc:
        raise SkillLifecycleError(f"Failed to open zip archive: {zip_path}") from exc
    with archive:
        file_infos: list[tuple[zipfile.ZipInfo, list[str]]] = []
        total_uncompressed = 0
        for info in archive.infolist():
            if _is_zip_symlink(info):
                raise SkillLifecycleError(
                    "Zip archive contains symlink entries, which are not allowed."
                )
            if info.is_dir():
                continue
            parts = _sanitize_zip_parts(info.filename)
            if not parts:
                continue
            file_infos.append((info, parts))
            if len(file_infos) > MAX_SKILL_ZIP_FILES:
                raise SkillLifecycleError(
                    f"Zip archive exceeds file-count limit ({MAX_SKILL_ZIP_FILES} files)."
                )
            total_uncompressed += max(0, int(info.file_size))
            if total_uncompressed > MAX_SKILL_ZIP_UNCOMPRESSED_BYTES:
                raise SkillLifecycleError(
                    "Zip archive exceeds uncompressed-size limit "
                    f"({MAX_SKILL_ZIP_UNCOMPRESSED_BYTES} bytes)."
                )
        for info, parts in file_infos:
            destination = _resolve_zip_target(target_root, parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _clone_git_source(source: str, target_root: Path) -> tuple[str, Path, str | None]:
    try:
        remote = urlsplit(source)
        if remote.scheme != "https" or not remote.hostname:
            raise ValueError("unsupported transport")
    except ValueError as exc:
        raise SkillLifecycleError("Git skill sources must use an HTTPS URL.") from exc
    if shutil.which("git") is None:
        raise SkillLifecycleError("git is required for git-based skill installs.")
    try:
        run_installer_git(
            ["git", "clone", "--depth", "1", "--", source, str(target_root)],
            cwd=None,
        )
    except subprocess.TimeoutExpired as exc:
        raise SkillLifecycleError("Git skill clone timed out after 120s.") from exc
    except subprocess.CalledProcessError as exc:
        raise SkillLifecycleError("Failed to clone git skill source.") from exc
    commit = None
    try:
        completed = run_installer_git(
            ["git", "-C", str(target_root), "rev-parse", "HEAD"],
            cwd=None,
        )
        commit = str(completed.stdout or "").strip() or None
    except subprocess.TimeoutExpired as exc:
        raise SkillLifecycleError("Git skill revision check timed out after 120s.") from exc
    except subprocess.CalledProcessError:
        commit = None
    return "git", target_root, commit


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    return bool((info.external_attr >> 16) & 0o170000 == 0o120000)


def _sanitize_zip_parts(name: str) -> list[str]:
    clean_name = str(name or "").replace("\\", "/")
    path = PurePosixPath(clean_name)
    parts = [part for part in path.parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise SkillLifecycleError("Zip archive entry escapes extraction root.")
    if path.is_absolute():
        raise SkillLifecycleError("Zip archive contains absolute paths, which are not allowed.")
    return parts


def _resolve_zip_target(base: Path, rel_parts: list[str]) -> Path:
    target = base.joinpath(*rel_parts).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError as exc:
        raise SkillLifecycleError("Zip archive entry escapes extraction root.") from exc
    return target


def _format_validation_failure(result: SkillValidationResult) -> str:
    if not result.issues:
        return f"Skill validation failed: {result.bundle_path}"
    first = result.issues[0]
    return f"Skill validation failed: {first.message} ({first.path})"
