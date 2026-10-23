from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path, PurePath

from ..atomic_io import atomic_write_json
from ..branding import canonical_user_config_dir, env_get

_TRUST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProjectHooksTrustKey:
    workspace_root: str
    relative_config_path: str
    file_hash: str


@dataclass(frozen=True)
class ProjectHooksTrustState:
    trusted_configs: tuple[ProjectHooksTrustKey, ...] = ()

    def contains(self, key: ProjectHooksTrustKey) -> bool:
        identity = _trust_identity(key)
        return any(_trust_identity(item) == identity for item in self.trusted_configs)


def _canonical_relative_config_path(value: str) -> str:
    """Normalize path separators in trust identities without rebasing locations."""
    return os.fspath(PurePath(value))


def _trust_identity(key: ProjectHooksTrustKey) -> ProjectHooksTrustKey:
    canonical = _canonical_relative_config_path(key.relative_config_path)
    if canonical == key.relative_config_path:
        return key
    return replace(key, relative_config_path=canonical)


def _config_dir() -> Path:
    override = env_get("ALYSIS_CONFIG_DIR")
    if override:
        return Path(override)
    return canonical_user_config_dir()


def trust_state_path(*, user_config_dir: Path | None = None) -> Path:
    base = user_config_dir.expanduser().resolve() if user_config_dir is not None else _config_dir()
    return base / "project_hooks_trust.json"


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_hooks_trust_key(*, workspace_root: Path, config_path: Path) -> ProjectHooksTrustKey:
    resolved_workspace = workspace_root.expanduser().resolve()
    resolved_path = config_path.expanduser().resolve()
    if not resolved_path.is_file():
        raise ValueError(f"Project hooks config not found: {resolved_path}")
    try:
        relative_config_path = os.fspath(resolved_path.relative_to(resolved_workspace))
    except ValueError as exc:
        raise ValueError(
            f"Project hooks config must live under the workspace root: {resolved_path}"
        ) from exc
    return ProjectHooksTrustKey(
        workspace_root=os.fspath(resolved_workspace),
        relative_config_path=relative_config_path,
        file_hash=_hash_file(resolved_path),
    )


def load_trust_state(*, user_config_dir: Path | None = None) -> ProjectHooksTrustState:
    path = trust_state_path(user_config_dir=user_config_dir)
    if not path.exists():
        return ProjectHooksTrustState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Failed to read project hooks trust state: {path}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("Invalid project hooks trust state format.")
    version = raw.get("schema_version", _TRUST_SCHEMA_VERSION)
    if version != _TRUST_SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported project hooks trust schema version: {version}")
    trusted_raw = raw.get("trusted_configs", [])
    if not isinstance(trusted_raw, list):
        raise RuntimeError("Invalid project hooks trust state: trusted_configs must be an array.")
    trusted: list[ProjectHooksTrustKey] = []
    for item in trusted_raw:
        if not isinstance(item, dict):
            continue
        workspace_root = str(item.get("workspace_root") or "").strip()
        relative_config_path = str(item.get("relative_config_path") or "").strip()
        file_hash = str(item.get("file_hash") or "").strip()
        if workspace_root and relative_config_path and file_hash:
            trusted.append(
                ProjectHooksTrustKey(
                    workspace_root=workspace_root,
                    relative_config_path=relative_config_path,
                    file_hash=file_hash,
                )
            )
    trusted_sorted = tuple(
        sorted(
            set(trusted),
            key=lambda key: (key.workspace_root, key.relative_config_path, key.file_hash),
        )
    )
    return ProjectHooksTrustState(trusted_configs=trusted_sorted)


def save_trust_state(
    state: ProjectHooksTrustState,
    *,
    user_config_dir: Path | None = None,
) -> None:
    path = trust_state_path(user_config_dir=user_config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _TRUST_SCHEMA_VERSION,
        "trusted_configs": [
            {
                "workspace_root": key.workspace_root,
                "relative_config_path": key.relative_config_path,
                "file_hash": key.file_hash,
            }
            for key in state.trusted_configs
        ],
    }
    atomic_write_json(path, payload, ensure_ascii=True)


def trust_project_hooks_config(
    *,
    workspace_root: Path,
    config_path: Path,
    user_config_dir: Path | None = None,
) -> ProjectHooksTrustState:
    key = project_hooks_trust_key(workspace_root=workspace_root, config_path=config_path)
    current = load_trust_state(user_config_dir=user_config_dir)
    trusted = set(current.trusted_configs)
    trusted.add(key)
    updated = ProjectHooksTrustState(
        trusted_configs=tuple(
            sorted(
                trusted,
                key=lambda item: (item.workspace_root, item.relative_config_path, item.file_hash),
            )
        )
    )
    save_trust_state(updated, user_config_dir=user_config_dir)
    return updated


def untrust_project_hooks_config(
    *,
    workspace_root: Path,
    config_path: Path,
    user_config_dir: Path | None = None,
) -> ProjectHooksTrustState:
    resolved_workspace = workspace_root.expanduser().resolve()
    resolved_path = config_path.expanduser().resolve()
    try:
        relative_config_path = os.fspath(resolved_path.relative_to(resolved_workspace))
    except ValueError as exc:
        raise ValueError(
            f"Project hooks config must live under the workspace root: {resolved_path}"
        ) from exc
    current = load_trust_state(user_config_dir=user_config_dir)
    canonical_relative = _canonical_relative_config_path(relative_config_path)
    remaining = tuple(
        key
        for key in current.trusted_configs
        if not (
            key.workspace_root == os.fspath(resolved_workspace)
            and _canonical_relative_config_path(key.relative_config_path) == canonical_relative
        )
    )
    updated = ProjectHooksTrustState(trusted_configs=remaining)
    save_trust_state(updated, user_config_dir=user_config_dir)
    return updated


def is_project_hooks_config_trusted(
    *,
    workspace_root: Path,
    config_path: Path,
    state: ProjectHooksTrustState | None = None,
    user_config_dir: Path | None = None,
) -> bool:
    resolved_path = config_path.expanduser().resolve()
    if not resolved_path.is_file():
        return False
    active_state = state if state is not None else load_trust_state(user_config_dir=user_config_dir)
    return active_state.contains(
        project_hooks_trust_key(
            workspace_root=workspace_root,
            config_path=config_path,
        )
    )


__all__ = [
    "ProjectHooksTrustKey",
    "ProjectHooksTrustState",
    "is_project_hooks_config_trusted",
    "load_trust_state",
    "project_hooks_trust_key",
    "save_trust_state",
    "trust_project_hooks_config",
    "trust_state_path",
    "untrust_project_hooks_config",
]
