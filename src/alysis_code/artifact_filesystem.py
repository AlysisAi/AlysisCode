"""Shared session artifact filesystem operations without application data classes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

ARTIFACT_HANDLE_INDEX_DIR = ".artifact_handles"


def normalize_artifact_parts(parts: tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for raw_part in parts:
        text = str(raw_part).strip().replace("\\", "/")
        if not text:
            raise ValueError("artifact path part cannot be empty")
        candidate = PurePosixPath(text)
        if candidate.is_absolute():
            raise ValueError("artifact path part must be relative")
        for segment in candidate.parts:
            if segment in {"", "."}:
                continue
            if segment == "..":
                raise ValueError("artifact path cannot traverse upward")
            normalized.append(segment)
    if not normalized:
        raise ValueError("artifact path cannot be empty")
    return normalized


class ArtifactFilesystemOperations:
    """One implementation of containment, locators and content-bound handles."""

    __slots__ = ()
    filesystem_root: Path
    locator_prefix: str

    def _handle_for_locator(self, locator: str, content_sha256: str) -> str:
        identity = f"{self.filesystem_root.resolve()}\0{locator}\0{content_sha256}"
        return "artifact:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _artifact_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as artifact_file:
            for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def register_artifact(self, artifact_path: Path) -> str:
        """Persist an exact, session-bound handle for an existing contained artifact."""
        from .private_artifact_io import _atomic_private_write_text

        locator = self.locator_for_path(artifact_path)
        if not self.resolve_locator(locator).is_file():
            raise ValueError("artifact must exist before its handle is registered")
        content_sha256 = self._artifact_hash(self.resolve_locator(locator))
        handle = self._handle_for_locator(locator, content_sha256)
        index_path = self.artifact_fs_path(
            ARTIFACT_HANDLE_INDEX_DIR, handle.removeprefix("artifact:") + ".json"
        )
        _atomic_private_write_text(
            index_path,
            json.dumps({"handle": handle, "locator": locator, "content_sha256": content_sha256}),
            containment_root=self.filesystem_root,
        )
        return handle

    def resolve_handle(self, handle: str) -> str:
        """Resolve only an exact published handle in this session's private index."""
        if re.fullmatch(r"artifact:[0-9a-f]{20}", str(handle)) is None:
            raise ValueError("handle must be an exact artifact: reference from this session")
        index_locator = self.artifact_locator(
            ARTIFACT_HANDLE_INDEX_DIR, handle.removeprefix("artifact:") + ".json"
        )
        index = self.resolve_locator(index_locator)
        data = json.loads(index.read_text(encoding="utf-8"))
        locator = data["locator"]
        expected_hash = data["content_sha256"]
        if (
            data.get("handle") != handle
            or self._handle_for_locator(locator, expected_hash) != handle
        ):
            raise ValueError("artifact handle identity does not match this session")
        path = self.resolve_locator(locator)
        if not path.is_file() or self._artifact_hash(path) != expected_hash:
            raise FileNotFoundError("artifact handle is no longer available")
        return locator

    def available_handles(self, *, limit: int = 20, after_handle: str = "") -> list[dict[str, str]]:
        """List bounded current-session references, skipping corrupt or foreign entries."""
        try:
            index_root = self.resolve_locator(self.artifact_locator(ARTIFACT_HANDLE_INDEX_DIR))
        except (OSError, RuntimeError, ValueError):
            return []
        if not index_root.is_dir():
            return []
        references: list[dict[str, str]] = []
        # Scan only our index, never an arbitrary workspace or another session.
        for index in sorted(index_root.glob("*.json"), key=lambda item: item.name, reverse=True):
            if len(references) >= max(1, min(limit, 100)):
                break
            handle = "artifact:" + index.stem
            if after_handle and handle >= after_handle:
                continue
            try:
                locator = self.resolve_handle(handle)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            references.append({"handle": handle, "locator": locator})
        return references

    def artifact_fs_path(self, *parts: str) -> Path:
        return self.filesystem_root.joinpath(*normalize_artifact_parts(parts))

    def artifact_locator(self, *parts: str) -> str:
        locator_parts = normalize_artifact_parts((self.locator_prefix, *parts))
        return "/".join(locator_parts)

    def locator_for_path(self, artifact_path: Path) -> str:
        rel = artifact_path.resolve().relative_to(self.filesystem_root.resolve()).as_posix()
        return self.artifact_locator(rel)

    def resolve_locator(self, locator: str) -> Path:
        """Resolve one logical locator inside this session's artifact root."""

        text = str(locator or "").strip()
        if not text:
            raise ValueError("locator cannot be empty")
        if "\\" in text or text.startswith("/"):
            raise ValueError("locator must be a relative POSIX locator")
        segments = text.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError("locator contains an invalid path segment")
        prefix = normalize_artifact_parts((self.locator_prefix,))
        if segments[: len(prefix)] != prefix or len(segments) == len(prefix):
            raise ValueError(f"locator must start with {self.artifact_locator('x')[:-1]}")

        root = self.filesystem_root.resolve()
        candidate = root.joinpath(*segments[len(prefix) :]).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("locator resolves outside the session artifact root") from exc
        return candidate


class FilesystemArtifactLayout(ArtifactFilesystemOperations):
    """Read-only filesystem identity for the isolated I/O worker."""

    __slots__ = ("_filesystem_root", "_locator_prefix")

    def __init__(self, filesystem_root: Path, locator_prefix: str = "session_artifacts"):
        self._filesystem_root = filesystem_root
        self._locator_prefix = locator_prefix

    @property
    def filesystem_root(self) -> Path:
        return self._filesystem_root

    @property
    def locator_prefix(self) -> str:
        return self._locator_prefix
