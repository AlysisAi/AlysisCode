from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .artifact_filesystem import (
    ARTIFACT_HANDLE_INDEX_DIR as ARTIFACT_HANDLE_INDEX_DIR,
)
from .artifact_filesystem import (
    ArtifactFilesystemOperations,
)
from .artifact_filesystem import (
    normalize_artifact_parts as normalize_artifact_parts,
)


def _path_is_under_root(*, path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class ModelArtifactReference:
    locator: str
    artifact_readable_via_fs: bool
    artifact_location: str


@dataclass(frozen=True)
class SessionArtifactLayout(ArtifactFilesystemOperations):
    filesystem_root: Path
    locator_prefix: str = "session_artifacts"

    def model_reference_for_path(
        self,
        *,
        artifact_path: Path,
        workspace_root: Path | None,
    ) -> ModelArtifactReference:
        readable_via_fs = _path_is_under_root(path=artifact_path, root=workspace_root)
        return ModelArtifactReference(
            locator=self.locator_for_path(artifact_path),
            artifact_readable_via_fs=readable_via_fs,
            artifact_location="workspace_root" if readable_via_fs else "external_session_store",
        )

    def display_reference_for_path(
        self,
        *,
        artifact_path: Path,
        workspace_root: Path | None,
    ) -> str:
        model_reference = self.model_reference_for_path(
            artifact_path=artifact_path,
            workspace_root=workspace_root,
        )
        if model_reference.artifact_readable_via_fs and workspace_root is not None:
            return artifact_path.resolve().relative_to(workspace_root.resolve()).as_posix()
        return model_reference.locator
