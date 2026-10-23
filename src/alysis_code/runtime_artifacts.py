from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

ROOT_RUNTIME_ARTIFACT_DIR_NAMES = frozenset(
    {
        ".alysis",
        ".alysis_images",
        ".git",
        "alysis-feedback",
        # The desktop host holds this lock during an explicit project-data
        # migration to ``.alysis``. Keep it outside user changes.
        ".alysis-migration.lock",
    }
)
RUNTIME_CACHE_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
RUST_RUNTIME_ARTIFACT_DIR_NAME = "target"
RUST_MANIFEST_FILENAME = "Cargo.toml"
RUNTIME_ARTIFACT_FILE_NAMES = frozenset({".coverage"})
RUNTIME_ARTIFACT_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})
RUNTIME_ARTIFACT_DIR_NAMES = ROOT_RUNTIME_ARTIFACT_DIR_NAMES | RUNTIME_CACHE_DIR_NAMES
RUNTIME_ARTIFACT_GIT_EXCLUDE_ENTRIES = (
    "/.alysis/",
    "/.alysis_images/",
    "/alysis-feedback/",
    "/.alysis-migration.lock/",
    "__pycache__/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".coverage",
    "*.pyc",
    "*.pyo",
)


def normalize_runtime_artifact_path(value: str) -> str:
    cleaned = str(value).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _is_rust_target_runtime_artifact(
    *,
    parts: tuple[str, ...],
    root: Path | None,
) -> bool:
    if not parts:
        return False
    if root is None:
        return False

    root_resolved = root.resolve()
    if parts[0] == RUST_RUNTIME_ARTIFACT_DIR_NAME:
        return (root_resolved / RUST_MANIFEST_FILENAME).is_file()
    for index, segment in enumerate(parts[:-1]):
        if segment != RUST_RUNTIME_ARTIFACT_DIR_NAME:
            continue
        target_parent = root_resolved.joinpath(*parts[:index])
        if (target_parent / RUST_MANIFEST_FILENAME).is_file():
            return True
    return False


def _iter_rust_manifest_dirs(root: Path) -> tuple[Path, ...]:
    root_resolved = root.resolve()
    manifest_dirs: set[Path] = set()
    for current_root, dirnames, filenames in os.walk(root_resolved):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in ROOT_RUNTIME_ARTIFACT_DIR_NAMES
            and dirname not in RUNTIME_CACHE_DIR_NAMES
            and dirname != RUST_RUNTIME_ARTIFACT_DIR_NAME
        ]
        if RUST_MANIFEST_FILENAME not in filenames:
            continue
        manifest_dirs.add(Path(current_root))
    return tuple(sorted(manifest_dirs))


def runtime_artifact_git_exclude_entries(root: Path) -> tuple[str, ...]:
    root_resolved = root.resolve()
    rust_target_entries: list[str] = []
    for manifest_dir in _iter_rust_manifest_dirs(root_resolved):
        relative_dir = manifest_dir.relative_to(root_resolved).as_posix()
        if relative_dir in {"", "."}:
            rust_target_entries.append("/target/")
        else:
            rust_target_entries.append(f"/{relative_dir}/target/")
    return (*RUNTIME_ARTIFACT_GIT_EXCLUDE_ENTRIES, *rust_target_entries)


def has_grounded_rust_target_runtime_artifacts(root: Path) -> bool:
    return bool(_iter_rust_manifest_dirs(root))


def is_runtime_artifact_path(path: str, *, root: Path | None = None) -> bool:
    cleaned = normalize_runtime_artifact_path(path)
    if not cleaned:
        return False

    parts = tuple(segment for segment in PurePosixPath(cleaned).parts if segment not in {"", "."})
    if not parts:
        return False
    if parts[0] in ROOT_RUNTIME_ARTIFACT_DIR_NAMES:
        return True
    if any(segment in RUNTIME_CACHE_DIR_NAMES for segment in parts):
        return True
    if _is_rust_target_runtime_artifact(parts=parts, root=root):
        return True

    filename = parts[-1]
    if filename in RUNTIME_ARTIFACT_FILE_NAMES:
        return True
    return any(filename.endswith(suffix) for suffix in RUNTIME_ARTIFACT_FILE_SUFFIXES)
