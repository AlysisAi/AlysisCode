from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..atomic_io import atomic_write_json
from ..forge import now_iso
from ..project_data import project_artifact_path
from .models import AssetError, AssetNotFoundError, AssetRecord, ComprehensionRecord
from .plan_binding import task_asset_briefing_for_execution
from .surface import AssetSurface

_MIRROR_REL_ROOT = ".alysis/task_assets"
_MIRROR_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MirroredAssetEntry:
    asset_id: str
    title: str
    kind: Literal["text", "image"]
    raw_workspace_path: Path | None
    raw_repo_relative_path: str | None
    extracted_text_workspace_path: Path | None
    extracted_text_repo_relative_path: str | None
    thumbnail_workspace_path: Path | None
    thumbnail_repo_relative_path: str | None
    comprehension_workspace_path: Path | None
    comprehension_repo_relative_path: str | None
    comprehension: ComprehensionRecord | None
    rationale: str | None
    expected_use: str | None
    status: Literal["mirrored", "deleted", "missing"]
    mime: str = ""
    size_bytes: int = 0
    original_filename: str = ""
    description: str = ""


@dataclass(frozen=True)
class TaskAssetMirror:
    workspace_path: Path
    manifest_path: Path
    primary: list[MirroredAssetEntry]
    may_need: list[MirroredAssetEntry]
    pinned: list[MirroredAssetEntry]
    task_id: str = ""


def mirror_task_assets(
    *,
    task: dict[str, Any],
    plan: dict[str, Any],
    surface: AssetSurface,
    workspace_path: Path,
) -> TaskAssetMirror:
    _ = plan
    workspace = workspace_path.resolve()
    target = workspace / _MIRROR_REL_ROOT
    staging = workspace / ".alysis" / f"task_assets.staging.{uuid.uuid4().hex}"
    staging_root = staging
    backup = workspace / ".alysis" / f"task_assets.backup.{uuid.uuid4().hex}"
    try:
        staging_root.mkdir(parents=True, exist_ok=False)
        active_records = surface.index.records(include_deleted=False)
        briefing = task_asset_briefing_for_execution(task, records=active_records)
        primary_specs = briefing.primary if briefing is not None else []
        may_need_specs = briefing.may_need if briefing is not None else []
        primary = [
            _mirror_entry(
                surface=surface,
                workspace=workspace,
                staging_root=staging_root,
                asset_id=entry.asset_id,
                rationale=entry.rationale,
                expected_use=entry.expected_use,
            )
            for entry in primary_specs
        ]
        may_need = [
            _mirror_entry(
                surface=surface,
                workspace=workspace,
                staging_root=staging_root,
                asset_id=entry.asset_id,
                rationale=entry.rationale,
                expected_use=entry.expected_use,
            )
            for entry in may_need_specs
        ]
        referenced = {entry.asset_id for entry in [*primary, *may_need]}
        pinned = [
            _mirror_entry(
                surface=surface,
                workspace=workspace,
                staging_root=staging_root,
                asset_id=record.id,
                rationale=None,
                expected_use=None,
            )
            for record in surface.index.records(include_deleted=False)
            if record.pinned and record.id not in referenced
        ]
        task_id = str(task.get("id") or "").strip()
        manifest_payload = {
            "schema_version": _MIRROR_SCHEMA_VERSION,
            "task_id": task_id,
            "run_id": surface.run_paths.run_id,
            "mirrored_at": now_iso(),
            "primary": [_entry_payload(entry, include_briefing=True) for entry in primary],
            "may_need": [_entry_payload(entry, include_briefing=True) for entry in may_need],
            "pinned": [_entry_payload(entry, include_briefing=False) for entry in pinned],
        }
        atomic_write_json(staging_root / "manifest.json", manifest_payload)
        _replace_directory(staging_root, target, backup)
        return TaskAssetMirror(
            workspace_path=workspace,
            manifest_path=target / "manifest.json",
            primary=[_retarget_entry(entry, workspace=workspace) for entry in primary],
            may_need=[_retarget_entry(entry, workspace=workspace) for entry in may_need],
            pinned=[_retarget_entry(entry, workspace=workspace) for entry in pinned],
            task_id=task_id,
        )
    except Exception:
        with suppress(FileNotFoundError):
            shutil.rmtree(staging_root)
        raise
    finally:
        with suppress(FileNotFoundError):
            shutil.rmtree(staging_root)
        with suppress(FileNotFoundError):
            shutil.rmtree(backup)


def load_task_asset_mirror(*, workspace_path: Path) -> TaskAssetMirror:
    workspace = workspace_path.resolve()
    manifest_path = workspace / _MIRROR_REL_ROOT / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        raise AssetError(f"Invalid task asset manifest: {manifest_path}")
    return TaskAssetMirror(
        workspace_path=workspace,
        manifest_path=manifest_path,
        primary=[
            _entry_from_payload(item, workspace=workspace) for item in payload.get("primary") or []
        ],
        may_need=[
            _entry_from_payload(item, workspace=workspace) for item in payload.get("may_need") or []
        ],
        pinned=[
            _entry_from_payload(item, workspace=workspace) for item in payload.get("pinned") or []
        ],
        task_id=str(payload.get("task_id") or "").strip(),
    )


def _mirror_entry(
    *,
    surface: AssetSurface,
    workspace: Path,
    staging_root: Path,
    asset_id: str,
    rationale: str | None,
    expected_use: str | None,
) -> MirroredAssetEntry:
    try:
        record = surface.index.get(asset_id, include_deleted=True)
    except AssetNotFoundError:
        return MirroredAssetEntry(
            asset_id=asset_id,
            title="<unknown>",
            kind="text",
            raw_workspace_path=None,
            raw_repo_relative_path=None,
            extracted_text_workspace_path=None,
            extracted_text_repo_relative_path=None,
            thumbnail_workspace_path=None,
            thumbnail_repo_relative_path=None,
            comprehension_workspace_path=None,
            comprehension_repo_relative_path=None,
            comprehension=None,
            rationale=rationale,
            expected_use=expected_use,
            status="missing",
        )
    if record.deleted_at is not None:
        return _deleted_entry(record=record, rationale=rationale, expected_use=expected_use)
    source = project_artifact_path(surface.run_paths.root, record.stored_path)
    if not source.exists():
        if surface.cfg.assets.worker.fail_on_mirror_error:
            raise AssetError(f"Asset raw file is missing on disk: {record.id}")
        return _missing_entry(record=record, rationale=rationale, expected_use=expected_use)
    raw_rel = f"{_MIRROR_REL_ROOT}/raw/{record.id}/{record.original_filename}"
    raw_dest = staging_root / "raw" / record.id / record.original_filename
    _copy_file(source=source, destination=raw_dest)
    extracted_dest, extracted_rel = _copy_optional(
        surface=surface,
        staging_root=staging_root,
        source_rel=record.extracted_text_path,
        destination_rel=f"{_MIRROR_REL_ROOT}/extracted/{record.id}.txt",
    )
    thumbnail_dest, thumbnail_rel = _copy_optional(
        surface=surface,
        staging_root=staging_root,
        source_rel=record.thumbnail_path,
        destination_rel=f"{_MIRROR_REL_ROOT}/extracted/{record.id}.thumb.png",
    )
    comprehension = _read_comprehension(surface, record)
    comprehension_dest: Path | None = None
    comprehension_rel: str | None = None
    if comprehension is not None:
        comprehension_rel = f"{_MIRROR_REL_ROOT}/comprehensions/{record.id}.json"
        comprehension_dest = staging_root / "comprehensions" / f"{record.id}.json"
        atomic_write_json(comprehension_dest, comprehension.to_dict())
    return MirroredAssetEntry(
        asset_id=record.id,
        title=record.title,
        kind=record.kind,
        raw_workspace_path=workspace / raw_rel,
        raw_repo_relative_path=raw_rel,
        extracted_text_workspace_path=extracted_dest and workspace / extracted_rel,
        extracted_text_repo_relative_path=extracted_rel,
        thumbnail_workspace_path=thumbnail_dest and workspace / thumbnail_rel,
        thumbnail_repo_relative_path=thumbnail_rel,
        comprehension_workspace_path=comprehension_dest and workspace / comprehension_rel,
        comprehension_repo_relative_path=comprehension_rel,
        comprehension=comprehension,
        rationale=rationale,
        expected_use=expected_use,
        status="mirrored",
        mime=record.mime,
        size_bytes=record.size_bytes,
        original_filename=record.original_filename,
        description=record.description,
    )


def _copy_optional(
    *,
    surface: AssetSurface,
    staging_root: Path,
    source_rel: str | None,
    destination_rel: str,
) -> tuple[Path | None, str | None]:
    if not source_rel:
        return None, None
    source = project_artifact_path(surface.run_paths.root, source_rel)
    if not source.exists():
        return None, None
    relative = destination_rel[len(_MIRROR_REL_ROOT) + 1 :]
    destination = staging_root / relative
    _copy_file(source=source, destination=destination)
    return destination, destination_rel


def _copy_file(*, source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        raise AssetError(f"Failed to mirror asset file: {source}") from exc


def _read_comprehension(
    surface: AssetSurface,
    record: AssetRecord,
) -> ComprehensionRecord | None:
    try:
        return surface.index.read_comprehension(record.id, record.comprehension_current_version)
    except AssetError:
        return None


def _deleted_entry(
    *,
    record: AssetRecord,
    rationale: str | None,
    expected_use: str | None,
) -> MirroredAssetEntry:
    return MirroredAssetEntry(
        asset_id=record.id,
        title=record.title,
        kind=record.kind,
        raw_workspace_path=None,
        raw_repo_relative_path=None,
        extracted_text_workspace_path=None,
        extracted_text_repo_relative_path=None,
        thumbnail_workspace_path=None,
        thumbnail_repo_relative_path=None,
        comprehension_workspace_path=None,
        comprehension_repo_relative_path=None,
        comprehension=None,
        rationale=rationale,
        expected_use=expected_use,
        status="deleted",
        mime=record.mime,
        size_bytes=record.size_bytes,
        original_filename=record.original_filename,
        description=record.description,
    )


def _missing_entry(
    *,
    record: AssetRecord,
    rationale: str | None,
    expected_use: str | None,
) -> MirroredAssetEntry:
    deleted = _deleted_entry(record=record, rationale=rationale, expected_use=expected_use)
    return MirroredAssetEntry(**{**deleted.__dict__, "status": "missing"})


def _entry_payload(entry: MirroredAssetEntry, *, include_briefing: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "asset_id": entry.asset_id,
        "title": entry.title,
        "kind": entry.kind,
        "mime": entry.mime,
        "size_bytes": entry.size_bytes,
        "raw_path": entry.raw_repo_relative_path,
        "extracted_text_path": entry.extracted_text_repo_relative_path,
        "thumbnail_path": entry.thumbnail_repo_relative_path,
        "comprehension_path": entry.comprehension_repo_relative_path,
        "comprehension_version": (
            entry.comprehension.version if entry.comprehension is not None else None
        ),
        "original_filename": entry.original_filename,
        "description": entry.description,
        "status": entry.status,
    }
    if include_briefing:
        payload["rationale"] = entry.rationale
        payload["expected_use"] = entry.expected_use
    return payload


def _entry_from_payload(payload: Any, *, workspace: Path) -> MirroredAssetEntry:
    raw = payload if isinstance(payload, dict) else {}
    status = str(raw.get("status") or "missing")
    if status not in {"mirrored", "deleted", "missing"}:
        status = "missing"
    comprehension: ComprehensionRecord | None = None
    comprehension_path = _path_or_none(workspace, raw.get("comprehension_path"))
    if comprehension_path is not None:
        with suppress(Exception):
            from .models import ComprehensionRecord

            comprehension = ComprehensionRecord.from_dict(
                json.loads(comprehension_path.read_text(encoding="utf-8"))
            )
    kind = str(raw.get("kind") or "text")
    if kind not in {"text", "image"}:
        kind = "text"
    return MirroredAssetEntry(
        asset_id=str(raw.get("asset_id") or "").strip(),
        title=str(raw.get("title") or ""),
        kind=kind,  # type: ignore[arg-type]
        raw_workspace_path=_path_or_none(workspace, raw.get("raw_path")),
        raw_repo_relative_path=_optional_string(raw.get("raw_path")),
        extracted_text_workspace_path=_path_or_none(workspace, raw.get("extracted_text_path")),
        extracted_text_repo_relative_path=_optional_string(raw.get("extracted_text_path")),
        thumbnail_workspace_path=_path_or_none(workspace, raw.get("thumbnail_path")),
        thumbnail_repo_relative_path=_optional_string(raw.get("thumbnail_path")),
        comprehension_workspace_path=comprehension_path,
        comprehension_repo_relative_path=_optional_string(raw.get("comprehension_path")),
        comprehension=comprehension,
        rationale=_optional_string(raw.get("rationale")),
        expected_use=_optional_string(raw.get("expected_use")),
        status=status,  # type: ignore[arg-type]
        mime=str(raw.get("mime") or ""),
        size_bytes=int(raw.get("size_bytes") or 0),
        original_filename=str(raw.get("original_filename") or ""),
        description=str(raw.get("description") or ""),
    )


def _path_or_none(workspace: Path, value: Any) -> Path | None:
    text = _optional_string(value)
    if text is None:
        return None
    return workspace / text


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _retarget_entry(entry: MirroredAssetEntry, *, workspace: Path) -> MirroredAssetEntry:
    return MirroredAssetEntry(
        **{
            **entry.__dict__,
            "raw_workspace_path": _path_or_none(workspace, entry.raw_repo_relative_path),
            "extracted_text_workspace_path": _path_or_none(
                workspace,
                entry.extracted_text_repo_relative_path,
            ),
            "thumbnail_workspace_path": _path_or_none(
                workspace, entry.thumbnail_repo_relative_path
            ),
            "comprehension_workspace_path": _path_or_none(
                workspace,
                entry.comprehension_repo_relative_path,
            ),
        }
    )


def _replace_directory(staging: Path, target: Path, backup: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(staging, target)
    except Exception:
        if backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)
