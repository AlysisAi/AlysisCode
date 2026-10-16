from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..config import AppConfig
from ..forge import RunPaths
from ..model_registry import ModelRegistry
from ..project_data import ProjectDataError, project_artifact_path
from .comprehender import AssetComprehender
from .index import AssetIndex
from .ingestion import ingest_asset
from .models import (
    AssetError,
    AssetNotFoundError,
    AssetRecord,
    ComprehensionRecord,
)
from .ocr import OcrProvider, TesseractOcrProvider

LOGGER = logging.getLogger(__name__)

SurfaceComprehensionStatus = Literal["pending", "running", "ready", "failed", "minimal"]
_ASSET_RUN_PATH_ATTRS: tuple[str, ...] = (
    "asset_store_dir",
    "assets_index_path",
    "assets_index_lock_path",
    "assets_raw_dir",
    "assets_extracted_dir",
    "assets_comprehensions_dir",
)


@dataclass(frozen=True)
class AssetSurfaceEntry:
    record: AssetRecord
    comprehension_status: SurfaceComprehensionStatus
    comprehension_source: str | None
    comprehension_summary_preview: str
    detected_language: str | None


@dataclass(frozen=True)
class AssetSurfaceDetail:
    record: AssetRecord
    comprehension_status: SurfaceComprehensionStatus
    comprehension: ComprehensionRecord | None
    versions: list[int]
    extracted_text_preview: str


@dataclass(frozen=True)
class AssetSurfaceAddResult:
    record: AssetRecord
    comprehension_handle: ComprehensionRefreshHandle | None
    comprehension_record: ComprehensionRecord | None


@dataclass
class ComprehensionRefreshHandle:
    asset_id: str
    thread: threading.Thread | None
    started_at: datetime
    cancellation_event: threading.Event
    _done_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _result: ComprehensionRecord | None = field(default=None, repr=False)

    def join(self, timeout: float | None = None) -> ComprehensionRecord | None:
        if self.thread is not None:
            self.thread.join(timeout)
        if self.thread is not None and self.thread.is_alive():
            return None
        return self._result

    def is_done(self) -> bool:
        if self.thread is None:
            return True
        return self._done_event.is_set()

    def _set_result(self, record: ComprehensionRecord) -> None:
        self._result = record
        self._done_event.set()

    def _mark_done(self) -> None:
        self._done_event.set()


@dataclass(frozen=True)
class AssetSurfaceJoinReport:
    completed: list[str]
    timed_out: list[str]


class AssetSurface:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        run_paths: RunPaths,
        model_registry: ModelRegistry | None = None,
        ocr_provider: OcrProvider | None = None,
        comprehender: AssetComprehender | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg = cfg
        self.run_paths = run_paths
        self.index = AssetIndex(run_paths)
        self.model_registry = model_registry or ModelRegistry(cfg=cfg)
        self.ocr_provider = ocr_provider
        self.comprehender = comprehender or AssetComprehender(
            cfg=cfg,
            model_registry=self.model_registry,
            ocr_provider=ocr_provider,
            run_paths=run_paths,
        )
        self.clock = clock
        self._registry_lock = threading.Lock()
        self._in_flight: dict[str, ComprehensionRefreshHandle] = {}

    def list_assets(self, *, include_deleted: bool = False) -> list[AssetSurfaceEntry]:
        return [
            self._entry_for(record)
            for record in self.index.records(include_deleted=include_deleted)
        ]

    def show_asset(self, asset_id: str) -> AssetSurfaceDetail:
        record = self.index.get(asset_id, include_deleted=True)
        return AssetSurfaceDetail(
            record=record,
            comprehension_status=self._surface_status(record),
            comprehension=self.comprehension_for(asset_id),
            versions=_comprehension_versions(self.run_paths, asset_id),
            extracted_text_preview=_read_text_preview(
                self.run_paths.root,
                record.extracted_text_path,
                limit=800,
            ),
        )

    def add_asset(
        self,
        path: Path,
        *,
        title: str,
        description: str = "",
        pinned: bool = False,
        added_by: dict[str, Any] | None = None,
        comprehend: Literal["sync", "async", "skip"] = "async",
        comprehension_callback: Callable[[ComprehensionRecord], None] | None = None,
        dedupe_policy: Literal["reject", "link"] = "reject",
    ) -> AssetSurfaceAddResult:
        existing_for_link = self._existing_asset_for_path(path) if dedupe_policy == "link" else None
        record = ingest_asset(
            path,
            title=title,
            description=description,
            run_paths=self.run_paths,
            pinned=pinned,
            added_by=added_by,
            dedupe_policy=dedupe_policy,
        )
        LOGGER.info(
            "asset_surface op=add_asset asset_id=%s kind=%s size_bytes=%s comprehend=%s",
            record.id,
            record.kind,
            record.size_bytes,
            comprehend,
        )
        if existing_for_link is not None and existing_for_link.id == record.id:
            return AssetSurfaceAddResult(
                record=record,
                comprehension_handle=None,
                comprehension_record=None,
            )
        if comprehend == "skip":
            return AssetSurfaceAddResult(
                record=record,
                comprehension_handle=None,
                comprehension_record=None,
            )
        if comprehend == "sync":
            handle = self.refresh_comprehension(
                record.id,
                mode="sync",
                callback=comprehension_callback,
            )
            return AssetSurfaceAddResult(
                record=self.index.get(record.id, include_deleted=True),
                comprehension_handle=None,
                comprehension_record=handle.join(),
            )
        handle = self.refresh_comprehension(
            record.id,
            mode="async",
            callback=comprehension_callback,
        )
        return AssetSurfaceAddResult(
            record=record,
            comprehension_handle=handle,
            comprehension_record=None,
        )

    def edit_asset(
        self,
        asset_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        pinned: bool | None = None,
        retrigger_comprehension: bool = False,
    ) -> AssetSurfaceDetail:
        current = self.index.get(asset_id, include_deleted=True)
        if current.deleted_at is not None:
            raise AssetError(f"Cannot edit deleted asset: {asset_id}")
        changes: list[str] = []
        next_record = current
        if title is not None:
            clean_title = title.strip()
            if not clean_title:
                raise AssetError("Asset title is required.")
            if clean_title != current.title:
                next_record = replace(next_record, title=clean_title)
                changes.append("title")
        if description is not None and description != current.description:
            next_record = replace(next_record, description=description)
            changes.append("description")
        if pinned is not None and bool(pinned) != current.pinned:
            next_record = replace(next_record, pinned=bool(pinned))
            changes.append("pinned")
        if changes:
            self.index.update(next_record)
        LOGGER.info(
            "asset_surface op=edit_asset asset_id=%s changes=%s retrigger_comprehension=%s",
            asset_id,
            f"[{','.join(changes)}]",
            str(bool(retrigger_comprehension)).lower(),
        )
        if retrigger_comprehension:
            self.refresh_comprehension(asset_id, mode="async")
        return self.show_asset(asset_id)

    def delete_asset(self, asset_id: str) -> AssetRecord:
        self._cancel_existing(asset_id, join=True)
        deleted = self.index.delete(asset_id)
        LOGGER.info("asset_surface op=delete_asset asset_id=%s", asset_id)
        return deleted

    def refresh_comprehension(
        self,
        asset_id: str,
        *,
        mode: Literal["sync", "async"] = "async",
        callback: Callable[[ComprehensionRecord], None] | None = None,
        angle: str | None = None,
    ) -> ComprehensionRefreshHandle:
        LOGGER.info(
            "asset_surface op=refresh_comprehension asset_id=%s mode=%s",
            asset_id,
            mode,
        )
        self._cancel_existing(asset_id, join=True)
        asset = self.index.get(asset_id, include_deleted=True)
        if asset.deleted_at is not None:
            raise AssetError(f"Cannot refresh deleted asset: {asset_id}")
        cancellation_event = threading.Event()
        handle = ComprehensionRefreshHandle(
            asset_id=asset_id,
            thread=None,
            started_at=self.clock(),
            cancellation_event=cancellation_event,
        )
        if mode == "sync":
            record = self._run_comprehension(
                asset=asset,
                handle=handle,
                callback=callback,
                angle=angle,
            )
            handle._set_result(record)
            return handle
        thread = threading.Thread(
            target=self._run_comprehension_thread,
            args=(asset, handle, callback, angle),
            name=f"asset-comprehension-{asset_id}",
            daemon=True,
        )
        handle.thread = thread
        with self._registry_lock:
            self._in_flight[asset_id] = handle
        thread.start()
        return handle

    def cancel_pending_comprehensions(self) -> int:
        with self._registry_lock:
            handles = [
                handle
                for handle in self._in_flight.values()
                if handle.thread is not None and handle.thread.is_alive()
            ]
        for handle in handles:
            handle.cancellation_event.set()
        return len(handles)

    def join_pending(self, *, timeout_seconds: float | None = None) -> AssetSurfaceJoinReport:
        with self._registry_lock:
            handles = list(self._in_flight.values())
        completed: list[str] = []
        timed_out: list[str] = []
        deadline = (
            None if timeout_seconds is None else time.monotonic() + max(float(timeout_seconds), 0.0)
        )
        for handle in handles:
            remaining = None if deadline is None else max(deadline - time.monotonic(), 0.0)
            record = handle.join(remaining)
            if record is None and handle.thread is not None and handle.thread.is_alive():
                timed_out.append(handle.asset_id)
            else:
                completed.append(handle.asset_id)
        return AssetSurfaceJoinReport(
            completed=list(dict.fromkeys(completed)),
            timed_out=list(dict.fromkeys(timed_out)),
        )

    def comprehension_for(self, asset_id: str) -> ComprehensionRecord | None:
        if self._is_running(asset_id):
            return None
        try:
            record = self.index.get(asset_id, include_deleted=True)
        except AssetNotFoundError:
            return None
        if record.comprehension_status == "pending":
            return None
        try:
            return self.index.read_comprehension(asset_id)
        except AssetError:
            return None

    def _entry_for(self, record: AssetRecord) -> AssetSurfaceEntry:
        comprehension = self.comprehension_for(record.id)
        status = self._surface_status(record)
        return AssetSurfaceEntry(
            record=record,
            comprehension_status=status,
            comprehension_source=comprehension.source if comprehension is not None else None,
            comprehension_summary_preview=_preview(
                comprehension.data.semantic_summary if comprehension is not None else "",
                120,
            ),
            detected_language=comprehension.detected_language
            if comprehension is not None
            else None,
        )

    def _surface_status(self, record: AssetRecord) -> SurfaceComprehensionStatus:
        if self._is_running(record.id):
            return "running"
        return record.comprehension_status

    def _run_comprehension_thread(
        self,
        asset: AssetRecord,
        handle: ComprehensionRefreshHandle,
        callback: Callable[[ComprehensionRecord], None] | None,
        angle: str | None,
    ) -> None:
        try:
            record = self._run_comprehension(
                asset=asset,
                handle=handle,
                callback=callback,
                angle=angle,
            )
            handle._set_result(record)
        finally:
            handle._mark_done()
            with self._registry_lock:
                if self._in_flight.get(asset.id) is handle:
                    self._in_flight.pop(asset.id, None)

    def _run_comprehension(
        self,
        *,
        asset: AssetRecord,
        handle: ComprehensionRefreshHandle,
        callback: Callable[[ComprehensionRecord], None] | None,
        angle: str | None = None,
    ) -> ComprehensionRecord:
        if angle is None:
            record = self.comprehender.comprehend(asset)
        else:
            record = self.comprehender.comprehend(asset, angle=angle)
        LOGGER.info(
            "asset_comprehension_complete asset_id=%s source=%s status=%s elapsed_ms=%s",
            asset.id,
            record.source,
            record.status,
            record.elapsed_ms,
        )
        if not handle.cancellation_event.is_set() and callback is not None:
            try:
                callback(record)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "asset_surface comprehension callback failed for asset_id=%s: %s",
                    asset.id,
                    exc,
                )
        return record

    def _is_running(self, asset_id: str) -> bool:
        with self._registry_lock:
            handle = self._in_flight.get(asset_id)
        return (
            handle is not None
            and handle.thread is not None
            and handle.thread.is_alive()
            and not handle.is_done()
        )

    def _cancel_existing(self, asset_id: str, *, join: bool) -> None:
        with self._registry_lock:
            handle = self._in_flight.pop(asset_id, None)
        if handle is None:
            return
        handle.cancellation_event.set()
        if join and handle.thread is not None and handle.thread.is_alive():
            handle.thread.join()

    def _existing_asset_for_path(self, path: Path) -> AssetRecord | None:
        try:
            digest = hashlib.sha256(path.expanduser().resolve().read_bytes()).hexdigest()
        except OSError:
            return None
        return self.index.find_by_sha256(digest)


def build_asset_surface(
    *,
    cfg: AppConfig,
    run_paths: RunPaths,
    model_registry: ModelRegistry | None = None,
) -> AssetSurface:
    if not run_paths_support_asset_surface(run_paths):
        missing = [
            attr
            for attr in _ASSET_RUN_PATH_ATTRS
            if not isinstance(getattr(run_paths, attr, None), Path)
        ]
        raise AssetError(
            "Run paths do not support the asset surface; missing path attributes: "
            + ", ".join(missing)
        )
    registry = model_registry or ModelRegistry(cfg=cfg)
    ocr_provider = _ocr_provider_from_config(cfg)
    return AssetSurface(
        cfg=cfg,
        run_paths=run_paths,
        model_registry=registry,
        ocr_provider=ocr_provider,
    )


def run_paths_support_asset_surface(run_paths: Any) -> bool:
    return all(isinstance(getattr(run_paths, attr, None), Path) for attr in _ASSET_RUN_PATH_ATTRS)


def _ocr_provider_from_config(cfg: AppConfig) -> OcrProvider | None:
    comprehension_cfg = cfg.assets.comprehension
    if comprehension_cfg.ocr_enabled == "never":
        return None
    provider_name = str(comprehension_cfg.ocr_provider or "").strip().lower()
    if provider_name != "tesseract":
        LOGGER.warning("Unsupported OCR provider configured for assets: %s", provider_name)
        return None
    provider = TesseractOcrProvider(timeout_seconds=comprehension_cfg.ocr_timeout_seconds)
    if not provider.is_available():
        return None
    return provider


def _comprehension_versions(run_paths: RunPaths, asset_id: str) -> list[int]:
    directory = run_paths.assets_comprehensions_dir / asset_id
    try:
        paths = list(directory.glob("v*.json"))
    except OSError:
        return []
    versions: list[int] = []
    for path in paths:
        stem = path.stem
        if not stem.startswith("v"):
            continue
        try:
            version = int(stem[1:])
        except ValueError:
            continue
        if version > 0:
            versions.append(version)
    return sorted(set(versions))


def _read_text_preview(root: Path, repo_relative_path: str | None, *, limit: int) -> str:
    if not repo_relative_path:
        return ""
    try:
        path = project_artifact_path(root, repo_relative_path).resolve()
        return _preview(path.read_text(encoding="utf-8", errors="replace"), limit)
    except (OSError, ProjectDataError):
        return ""


def _preview(text: str, limit: int) -> str:
    clean = str(text or "")
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "..."
