from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import socket
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..config import AppConfig
from ..forge import RunPaths, now_iso
from ..project_data import ProjectDataError, project_artifact_path
from .models import AssetAlreadyExistsError, AssetError
from .surface import AssetSurface

LOGGER = logging.getLogger(__name__)

PLAN_SCHEMA_VERSION_V1 = 1
PLAN_SCHEMA_VERSION_V2 = 2
_LOCK_POLL_SECONDS = 0.05
_LOCK_WAIT_SECONDS = 30.0


@dataclass(frozen=True)
class MigratedLegacyAsset:
    legacy_stored_path: str
    legacy_text_copy_path: str | None
    new_asset_id: str
    title: str
    description: str
    comprehension_handle_running: bool


@dataclass(frozen=True)
class LegacyAssetMigrationResult:
    schema_version_before: int
    schema_version_after: int
    migrated_assets: list[MigratedLegacyAsset]
    skipped_existing: list[str]
    failed: list[tuple[str, str]]
    plan_assets_array_cleared: bool
    plan_v2_written: bool


def migrate_legacy_assets(
    *,
    cfg: AppConfig,
    run_paths: RunPaths,
    surface: AssetSurface,
    comprehend_mode: Literal["sync", "async", "skip"] = "async",
) -> LegacyAssetMigrationResult:
    plan = _read_plan(run_paths.plan_json_path)
    schema_before = _schema_version(plan)
    if schema_before >= PLAN_SCHEMA_VERSION_V2:
        return LegacyAssetMigrationResult(
            schema_version_before=schema_before,
            schema_version_after=schema_before,
            migrated_assets=[],
            skipped_existing=[],
            failed=[],
            plan_assets_array_cleared=False,
            plan_v2_written=False,
        )

    with LegacyMigrationLock(run_paths):
        plan = _read_plan(run_paths.plan_json_path)
        schema_before = _schema_version(plan)
        if schema_before >= PLAN_SCHEMA_VERSION_V2:
            return LegacyAssetMigrationResult(
                schema_version_before=schema_before,
                schema_version_after=schema_before,
                migrated_assets=[],
                skipped_existing=[],
                failed=[],
                plan_assets_array_cleared=False,
                plan_v2_written=False,
            )
        result = _migrate_locked(
            cfg=cfg,
            run_paths=run_paths,
            surface=surface,
            plan=plan,
            schema_before=schema_before,
            comprehend_mode="skip" if not cfg.assets.enabled else comprehend_mode,
        )
        return result


class LegacyMigrationLock:
    def __init__(self, run_paths: RunPaths) -> None:
        self.path = run_paths.run_dir / "legacy_migration.lock"
        self._fd: int | None = None

    def __enter__(self) -> LegacyMigrationLock:
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if self._recover_stale_lock():
                    continue
                if time.monotonic() >= deadline:
                    raise AssetError(
                        f"Timed out waiting for legacy asset migration lock: {self.path}"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
                continue
            self._fd = fd
            payload = {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "created_at": now_iso(),
                "created_epoch": time.time(),
            }
            os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
            return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        with suppress(FileNotFoundError):
            self.path.unlink()

    def _recover_stale_lock(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        created_epoch = _safe_float(payload.get("created_epoch"))
        if created_epoch is None or time.time() - created_epoch < _LOCK_WAIT_SECONDS:
            return False
        hostname = str(payload.get("hostname") or "").strip()
        if hostname and hostname != socket.gethostname():
            return False
        pid = _safe_int(payload.get("pid"))
        if pid is not None and _process_is_running(pid) is not False:
            return False
        with suppress(FileNotFoundError):
            self.path.unlink()
        return True


def _migrate_locked(
    *,
    cfg: AppConfig,
    run_paths: RunPaths,
    surface: AssetSurface,
    plan: dict[str, Any],
    schema_before: int,
    comprehend_mode: Literal["sync", "async", "skip"],
) -> LegacyAssetMigrationResult:
    legacy_assets = plan.get("assets")
    if not isinstance(legacy_assets, list):
        raise AssetError("Invalid legacy plan: 'assets' must be an array before migration.")
    title_counts: dict[str, int] = {}
    migrated: list[MigratedLegacyAsset] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for entry in legacy_assets:
        if not isinstance(entry, dict):
            failed.append(("(invalid legacy asset entry)", "Legacy asset entry must be an object"))
            continue
        stored_path = str(entry.get("stored_path") or "").strip()
        text_copy_path = _optional_string(entry.get("text_copy_path"))
        if not stored_path:
            failed.append(("(missing stored_path)", "Legacy asset is missing stored_path"))
            continue
        try:
            source = project_artifact_path(run_paths.root, stored_path).resolve()
            _ensure_under_root(run_paths.root, source)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError as exc:
            failed.append((stored_path, str(exc)))
            continue
        except (AssetError, ProjectDataError) as exc:
            failed.append((stored_path, str(exc)))
            continue

        existing = surface.index.find_by_sha256(digest, include_deleted=False)
        if existing is not None:
            skipped.append(stored_path)
            LOGGER.info(
                "legacy_asset_migration skipped existing legacy_stored_path=%s asset_id=%s",
                stored_path,
                existing.id,
            )
            continue

        title = _deduped_title(_derive_title(source), title_counts)
        try:
            add_result = surface.add_asset(
                source,
                title=title,
                description="",
                pinned=False,
                added_by={
                    "phase": "legacy_migration",
                    "schema_version_before": schema_before,
                    "legacy_stored_path": stored_path,
                },
                comprehend=comprehend_mode,
                dedupe_policy="link",
            )
        except AssetAlreadyExistsError as exc:
            skipped.append(stored_path)
            LOGGER.info(
                "legacy_asset_migration skipped dedupe legacy_stored_path=%s asset_id=%s",
                stored_path,
                exc.existing_id,
            )
            continue
        except AssetError as exc:
            failed.append((stored_path, str(exc)))
            continue
        handle = add_result.comprehension_handle
        migrated.append(
            MigratedLegacyAsset(
                legacy_stored_path=stored_path,
                legacy_text_copy_path=text_copy_path,
                new_asset_id=add_result.record.id,
                title=add_result.record.title,
                description=add_result.record.description,
                comprehension_handle_running=bool(handle is not None and not handle.is_done()),
            )
        )
        LOGGER.info(
            "legacy_asset_migration migrated legacy_stored_path=%s asset_id=%s comprehend=%s",
            stored_path,
            add_result.record.id,
            comprehend_mode,
        )

    if failed:
        return LegacyAssetMigrationResult(
            schema_version_before=schema_before,
            schema_version_after=schema_before,
            migrated_assets=migrated,
            skipped_existing=skipped,
            failed=failed,
            plan_assets_array_cleared=False,
            plan_v2_written=False,
        )

    plan["schema_version"] = PLAN_SCHEMA_VERSION_V2
    plan["assets"] = []
    plan["legacy_assets_migrated_at"] = now_iso()
    _bind_migrated_assets_to_matching_tasks(plan=plan, surface=surface, migrated=migrated)
    from ..forge import save_plan

    save_plan(run_paths, plan)
    return LegacyAssetMigrationResult(
        schema_version_before=schema_before,
        schema_version_after=PLAN_SCHEMA_VERSION_V2,
        migrated_assets=migrated,
        skipped_existing=skipped,
        failed=[],
        plan_assets_array_cleared=True,
        plan_v2_written=True,
    )


def _bind_migrated_assets_to_matching_tasks(
    *,
    plan: dict[str, Any],
    surface: AssetSurface,
    migrated: list[MigratedLegacyAsset],
) -> None:
    if not migrated:
        return
    from .plan_binding import bind_asset_to_matching_tasks

    try:
        active_records = surface.index.records(include_deleted=False)
    except AssetError:
        return
    by_id = {record.id: record for record in active_records}
    for item in migrated:
        record = by_id.get(item.new_asset_id)
        if record is None:
            continue
        bind_asset_to_matching_tasks(
            plan=plan,
            record=record,
            active_records=active_records,
        )


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AssetError(f"Failed to read plan for legacy migration: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AssetError(f"Invalid plan JSON for legacy migration: {path}") from exc
    if not isinstance(payload, dict):
        raise AssetError(f"Invalid plan structure for legacy migration: {path}")
    return payload


def _schema_version(plan: dict[str, Any]) -> int:
    try:
        return int(plan.get("schema_version", PLAN_SCHEMA_VERSION_V1) or PLAN_SCHEMA_VERSION_V1)
    except (TypeError, ValueError):
        return PLAN_SCHEMA_VERSION_V1


def _derive_title(source: Path) -> str:
    stem = source.stem.replace("_", " ").replace("-", " ").strip().casefold()
    stem = " ".join(stem.split())
    if not stem:
        return "Legacy asset"
    return stem[:1].upper() + stem[1:]


def _deduped_title(title: str, counts: dict[str, int]) -> str:
    count = counts.get(title, 0) + 1
    counts[title] = count
    return title if count == 1 else f"{title} {count}"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _process_is_running(pid: int) -> bool | None:
    try:
        if pid <= 0:
            return False
    except TypeError:
        return None
    if os.name == "nt":
        return _windows_process_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ESRCH:
            return False
        return None
    return True


def _windows_process_is_running(pid: int) -> bool | None:
    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == error_invalid_parameter:
                return False
            return None
        exit_code = wintypes.DWORD()
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _ensure_under_root(root: Path, path: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise AssetError(f"Legacy asset path escapes workspace root: {path}") from exc
