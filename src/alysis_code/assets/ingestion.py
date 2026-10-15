from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import tempfile
import uuid
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from ..forge import RunPaths, now_iso
from ..project_data import project_artifact_path
from .index import AssetIndex
from .models import AssetAlreadyExistsError, AssetError, AssetRecord
from .paths import asset_raw_path, asset_thumbnail_path, repo_rel

_TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".tsv",
    ".log",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".html",
    ".css",
    ".xml",
    ".sql",
    ".sh",
    ".ps1",
}
_IMAGE_THUMB_MAX_EDGE = 512


def ingest_asset(
    path: Path,
    *,
    title: str,
    description: str = "",
    run_paths: RunPaths,
    pinned: bool = False,
    added_by: dict[str, Any] | None = None,
    dedupe_policy: Literal["reject", "link"] = "reject",
) -> AssetRecord:
    source = path.expanduser().resolve()
    clean_title = title.strip()
    if not clean_title:
        raise AssetError("Asset title is required.")
    if not source.exists():
        raise AssetError(f"Asset file does not exist: {source}")
    if not source.is_file():
        raise AssetError(f"Asset path is not a file: {source}")

    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    index = AssetIndex(run_paths)
    existing = index.find_by_sha256(digest)
    if existing is not None:
        if dedupe_policy == "link":
            return existing
        raise AssetAlreadyExistsError(existing.id)

    kind, mime = _classify_asset(source, raw)
    staging_dir = _staging_dir(run_paths)
    staged_raw = staging_dir / source.name
    staged_thumbnail = staging_dir / "thumbnail.png"
    try:
        _atomic_write_bytes(staged_raw, raw)
        if kind == "image":
            _write_thumbnail(source, staged_thumbnail)
        return index.add_with_publish(
            sha256=digest,
            dedupe_policy=dedupe_policy,
            build_record=lambda existing_ids: _build_record(
                run_paths=run_paths,
                existing_ids=existing_ids,
                digest=digest,
                title=clean_title,
                description=description,
                kind=kind,
                mime=mime,
                original_filename=source.name,
                size_bytes=len(raw),
                pinned=pinned,
                added_by=added_by,
            ),
            publish=lambda record: _publish_staged_asset(
                run_paths=run_paths,
                record=record,
                staged_raw=staged_raw,
                staged_thumbnail=staged_thumbnail if kind == "image" else None,
            ),
            rollback=lambda record: _rollback_published_asset(run_paths, record),
        )
    finally:
        with suppress(OSError):
            shutil.rmtree(staging_dir)


def _staging_dir(run_paths: RunPaths) -> Path:
    return run_paths.assets_raw_dir / ".staging" / uuid.uuid4().hex


def _build_record(
    *,
    run_paths: RunPaths,
    existing_ids: set[str],
    digest: str,
    title: str,
    description: str,
    kind: Literal["text", "image"],
    mime: str,
    original_filename: str,
    size_bytes: int,
    pinned: bool,
    added_by: dict[str, Any] | None,
) -> AssetRecord:
    asset_id = _asset_id_for_sha256(digest, existing_ids)
    stored_path = asset_raw_path(run_paths, asset_id, original_filename)
    record = AssetRecord(
        id=asset_id,
        title=title,
        description=description,
        kind=kind,
        mime=mime,
        original_filename=original_filename,
        size_bytes=size_bytes,
        sha256=digest,
        stored_path=repo_rel(run_paths.root, stored_path),
        extracted_text_path=None,
        thumbnail_path=None,
        pinned=bool(pinned),
        added_at=now_iso(),
        added_by=dict(added_by or {}),
        deleted_at=None,
        comprehension_status="pending",
        comprehension_current_version=None,
    )
    if kind == "text":
        return _with_text_paths(record)
    return _with_thumbnail(
        record, repo_rel(run_paths.root, asset_thumbnail_path(run_paths, asset_id))
    )


def _publish_staged_asset(
    *,
    run_paths: RunPaths,
    record: AssetRecord,
    staged_raw: Path,
    staged_thumbnail: Path | None,
) -> None:
    final_raw = project_artifact_path(run_paths.root, record.stored_path)
    final_raw.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged_raw, final_raw)
    if staged_thumbnail is not None and record.thumbnail_path is not None:
        final_thumbnail = project_artifact_path(run_paths.root, record.thumbnail_path)
        final_thumbnail.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_thumbnail, final_thumbnail)


def _rollback_published_asset(run_paths: RunPaths, record: AssetRecord) -> None:
    with suppress(OSError):
        shutil.rmtree(project_artifact_path(run_paths.root, record.stored_path).parent)
    if record.thumbnail_path is not None:
        with suppress(FileNotFoundError):
            project_artifact_path(run_paths.root, record.thumbnail_path).unlink()


def _with_text_paths(record: AssetRecord) -> AssetRecord:
    return replace(record, extracted_text_path=record.stored_path)


def _with_thumbnail(record: AssetRecord, thumbnail_path: str) -> AssetRecord:
    return replace(record, thumbnail_path=thumbnail_path)


def _classify_asset(source: Path, raw: bytes) -> tuple[Literal["text", "image"], str]:
    mime, _ = mimetypes.guess_type(source.name)
    suffix = source.suffix.lower()
    if mime and mime.startswith("image/"):
        _ensure_pillow_can_open(source)
        return "image", mime
    if mime and mime.startswith("text/"):
        _ensure_utf8_text(raw, source)
        return "text", mime
    if suffix in _TEXT_SUFFIXES:
        _ensure_utf8_text(raw, source)
        return "text", mime or "text/plain"
    raise AssetError(f"Unsupported asset file type: {source.name}")


def _ensure_utf8_text(raw: bytes, source: Path) -> None:
    if b"\x00" in raw:
        raise AssetError(f"Unsupported binary text asset: {source.name}")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise AssetError(f"Text asset must be UTF-8: {source.name}") from e


def _ensure_pillow_can_open(source: Path) -> None:
    try:
        from PIL import Image
    except ModuleNotFoundError as e:
        raise AssetError("Pillow is required for image asset ingestion.") from e
    try:
        with Image.open(source) as image:
            image.verify()
    except Exception as e:  # noqa: BLE001 - Pillow raises multiple format-specific errors
        raise AssetError(f"Unsupported image asset: {source.name}") from e


def _write_thumbnail(source: Path, destination: Path) -> None:
    try:
        from PIL import Image
    except ModuleNotFoundError as e:
        raise AssetError("Pillow is required for image asset thumbnails.") from e
    with Image.open(source) as image:
        normalized = image.convert("RGB")
        normalized.thumbnail((_IMAGE_THUMB_MAX_EDGE, _IMAGE_THUMB_MAX_EDGE))
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        os.close(fd)
        try:
            normalized.save(temp_path, format="PNG")
            os.replace(temp_path, destination)
        finally:
            with suppress(FileNotFoundError):
                temp_path.unlink()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        with suppress(FileNotFoundError):
            temp_path.unlink()


def _asset_id_for_sha256(sha256: str, existing_ids: set[str]) -> str:
    digest = sha256.strip().lower()
    for offset in range(0, max(len(digest) - 7, 1), 8):
        candidate = f"ast_{digest[offset : offset + 8]}"
        if len(candidate) == 12 and candidate not in existing_ids:
            return candidate
    raise AssetError("Unable to allocate stable asset id without collision.")
