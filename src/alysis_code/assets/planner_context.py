from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..model_registry import ModelRegistry
from ..project_data import project_artifact_path
from .models import AssetError, AssetRecord, ComprehensionRecord
from .plan_binding import task_asset_briefing
from .surface import AssetSurface, SurfaceComprehensionStatus

LOGGER = logging.getLogger(__name__)
_TRUNCATION_SUFFIX = "... (truncated for context budget)"


@dataclass(frozen=True)
class PlannerAssetsContext:
    text_block: str
    inline_image_paths: list[str]
    pending_asset_ids: list[str]
    failed_asset_ids: list[str]
    minimal_asset_ids: list[str]
    referenced_asset_ids: list[str]


class AssetReadinessPolicy(Enum):
    BLOCK = "block"
    SOFT = "soft"
    PARTIAL = "partial"


@dataclass(frozen=True)
class AssetReadinessReport:
    ready: list[str]
    pending: list[str]
    failed: list[str]
    minimal: list[str]
    timed_out: list[str]
    policy: AssetReadinessPolicy


@dataclass(frozen=True)
class AssetReferenceReport:
    deleted_referenced: list[tuple[str, str]]
    missing_referenced: list[tuple[str, str]]
    pinned_added: list[str]


@dataclass(frozen=True)
class PlannerAssetsBundle:
    context: PlannerAssetsContext
    inline_image_paths: list[str]
    readiness_report: AssetReadinessReport
    reference_report: AssetReferenceReport


def build_planner_assets_block(
    surface: AssetSurface,
    *,
    include_pending: bool = False,
) -> PlannerAssetsContext:
    entries = sorted(
        surface.list_assets(include_deleted=False),
        key=lambda entry: (not entry.record.pinned, entry.record.id),
    )
    max_chars = max(int(surface.cfg.assets.planner.max_chars_per_asset), len(_TRUNCATION_SUFFIX))
    lines = [
        "## Available Assets",
        "",
        "The user has provided the following resources for this run. Use them when they help. "
        "You may inspect text assets via `asset_read` and request re-comprehension via "
        "`asset_inspect`. Do not invent asset ids.",
    ]
    inline_image_paths: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    minimal: list[str] = []

    if not entries:
        lines.extend(["", "- (no assets attached)"])

    for entry in entries:
        record = entry.record
        status = entry.comprehension_status
        if status in {"pending", "running"}:
            pending.append(record.id)
            if not include_pending:
                continue
        elif status == "failed":
            failed.append(record.id)
        elif status == "minimal":
            minimal.append(record.id)
        comprehension = surface.comprehension_for(record.id)
        block = _asset_block(
            record=record,
            status=status,
            comprehension=comprehension,
            max_chars=max_chars,
        )
        lines.extend(["", block])
        if (
            record.kind == "image"
            and status in {"ready", "minimal", "failed"}
            and record.stored_path
        ):
            inline_image_paths.append(
                str(project_artifact_path(surface.run_paths.root, record.stored_path).resolve())
            )

    context = PlannerAssetsContext(
        text_block="\n".join(lines).rstrip(),
        inline_image_paths=inline_image_paths,
        pending_asset_ids=pending,
        failed_asset_ids=failed,
        minimal_asset_ids=minimal,
        referenced_asset_ids=[],
    )
    LOGGER.info(
        "planner_assets_block assets_total=%s ready=%s minimal=%s failed=%s pending=%s image_inline=%s",
        len(entries),
        len([entry for entry in entries if entry.comprehension_status == "ready"]),
        len(minimal),
        len(failed),
        len(pending),
        len(inline_image_paths),
    )
    return context


def ensure_planner_asset_readiness(
    surface: AssetSurface,
    *,
    policy: AssetReadinessPolicy,
    timeout_seconds: float | None = None,
) -> AssetReadinessReport:
    timed_out: list[str] = []
    _start_pending_comprehensions(surface)
    if policy is AssetReadinessPolicy.BLOCK:
        join_report = surface.join_pending(timeout_seconds=timeout_seconds)
        timed_out = join_report.timed_out

    report = _readiness_snapshot(surface=surface, policy=policy, timed_out=timed_out)
    LOGGER.info(
        "planner_assets_readiness policy=%s ready=%s pending=%s timed_out=%s",
        policy.value,
        len(report.ready),
        len(report.pending),
        len(report.timed_out),
    )
    if policy is AssetReadinessPolicy.BLOCK and (report.pending or report.timed_out):
        offenders = list(dict.fromkeys([*report.pending, *report.timed_out]))
        raise AssetError(
            "Asset comprehension did not finish before planner readiness timeout: "
            + ", ".join(offenders)
        )
    return report


def asset_reference_check(
    plan: dict[str, Any] | None, surface: AssetSurface
) -> AssetReferenceReport:
    if not isinstance(plan, dict):
        return AssetReferenceReport(deleted_referenced=[], missing_referenced=[], pinned_added=[])
    records = {record.id: record for record in surface.index.records(include_deleted=True)}
    referenced_ids: set[str] = set()
    deleted: list[tuple[str, str]] = []
    missing: list[tuple[str, str]] = []
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip() or "(unknown)"
        try:
            briefing = task_asset_briefing(task)
        except AssetError:
            continue
        if briefing is None:
            continue
        for entry in [*briefing.primary, *briefing.may_need]:
            referenced_ids.add(entry.asset_id)
            record = records.get(entry.asset_id)
            if record is None:
                missing.append((task_id, entry.asset_id))
            elif record.deleted_at is not None:
                deleted.append((task_id, entry.asset_id))
    pinned_added = sorted(
        record.id
        for record in records.values()
        if record.pinned and record.deleted_at is None and record.id not in referenced_ids
    )
    report = AssetReferenceReport(
        deleted_referenced=list(dict.fromkeys(deleted)),
        missing_referenced=list(dict.fromkeys(missing)),
        pinned_added=pinned_added,
    )
    LOGGER.info(
        "planner_assets_reference_check deleted_referenced=%s missing_referenced=%s pinned_added=%s",
        len(report.deleted_referenced),
        len(report.missing_referenced),
        len(report.pinned_added),
    )
    return report


def build_planner_assets_bundle(
    *,
    cfg: AppConfig,
    surface: AssetSurface,
    plan: dict[str, Any] | None,
    role_model: str,
    model_registry: ModelRegistry,
) -> PlannerAssetsBundle:
    policy = AssetReadinessPolicy(str(cfg.assets.planner.readiness_policy))
    readiness = ensure_planner_asset_readiness(
        surface,
        policy=policy,
        timeout_seconds=float(cfg.assets.planner.readiness_timeout_seconds),
    )
    context = build_planner_assets_block(
        surface,
        include_pending=policy is AssetReadinessPolicy.PARTIAL,
    )
    reference_report = asset_reference_check(plan, surface)
    referenced_ids = sorted(_collect_referenced_asset_ids_best_effort(plan or {}))
    if reference_report.deleted_referenced or reference_report.missing_referenced:
        context = replace(
            context,
            text_block=context.text_block + _drift_block(reference_report),
            referenced_asset_ids=referenced_ids,
        )
    else:
        context = replace(context, referenced_asset_ids=referenced_ids)
    inline_paths: list[str] = []
    if cfg.assets.planner.inline_images and _model_supports_vision(model_registry, role_model):
        inline_paths = context.inline_image_paths[
            : max(int(cfg.assets.planner.max_inline_images), 0)
        ]
    return PlannerAssetsBundle(
        context=context,
        inline_image_paths=inline_paths,
        readiness_report=readiness,
        reference_report=reference_report,
    )


def _asset_block(
    *,
    record: AssetRecord,
    status: SurfaceComprehensionStatus,
    comprehension: ComprehensionRecord | None,
    max_chars: int,
) -> str:
    header = f'### {record.id} - "{record.title}"'
    if status in {"pending", "running"}:
        return _truncate_block(
            "\n".join(
                [
                    header,
                    "- Status: comprehension pending - content not yet structured.",
                    f"- Kind: {_kind_label(record)}",
                    "- The planner should not rely on this asset until comprehension is ready.",
                ]
            ),
            max_chars=max_chars,
        )
    if status == "minimal":
        lines = [
            header,
            "- Status: minimal comprehension - only user-provided metadata is available.",
            f"- Description: {record.description or '(none)'}",
            f"- Kind: {_kind_label(record)}",
            "- Treat as low-confidence context.",
        ]
        if record.kind == "image":
            lines.append(
                "- Visual/OCR note: no structured visual or OCR understanding is available; "
                "ask the user or refresh after configuring a vision/OCR provider if the image matters."
            )
        return _truncate_block("\n".join(lines), max_chars=max_chars)
    if status == "failed":
        return _truncate_block(
            "\n".join(
                [
                    header,
                    "- Status: comprehension failed - ask the user about this asset if it matters.",
                    f"- Kind: {_kind_label(record)}",
                    f"- Error: {(comprehension.error if comprehension else '') or 'unknown'}",
                ]
            ),
            max_chars=max_chars,
        )
    if comprehension is None:
        return _truncate_block(
            "\n".join(
                [
                    header,
                    "- Status: comprehension unavailable.",
                    f"- Kind: {_kind_label(record)}",
                ]
            ),
            max_chars=max_chars,
        )
    lines = [
        header,
        f"- Kind: {_kind_label(record)}",
        "- Source: "
        + _source_line(comprehension)
        + f" - confidence: {_confidence_label(comprehension)}",
        f"- Pinned: {'yes' if record.pinned else 'no'}",
        f"- Summary: {comprehension.data.semantic_summary or '(none)'}",
    ]
    _append_entities(lines, comprehension)
    _append_list(lines, "Stated facts", comprehension.data.stated_facts)
    _append_list(lines, "Stated decisions", comprehension.data.stated_decisions)
    _append_list(lines, "Stated constraints", comprehension.data.stated_constraints)
    _append_list(lines, "Actionable signals", comprehension.data.actionable_signals)
    _append_list(lines, "Open questions", comprehension.data.open_questions)
    return _truncate_block("\n".join(lines), max_chars=max_chars)


def _source_line(comprehension: ComprehensionRecord) -> str:
    lang = comprehension.detected_language or "unknown"
    if comprehension.language_confidence is None:
        return f"{comprehension.source} - detected language: {lang}"
    return f"{comprehension.source} - detected language: {lang} ({comprehension.language_confidence:.2f})"


def _append_entities(lines: list[str], comprehension: ComprehensionRecord) -> None:
    entities = []
    for item in comprehension.data.key_entities[:12]:
        if not isinstance(item, dict):
            continue
        entity_type = str(item.get("type") or "").strip()
        value = str(item.get("value") or "").strip()
        if entity_type and value:
            entities.append(f"{entity_type}={value}")
    if entities:
        lines.append("- Key entities: " + ", ".join(entities))


def _append_list(lines: list[str], label: str, values: list[str]) -> None:
    clean_values = [str(value).strip() for value in values if str(value).strip()]
    if not clean_values:
        return
    lines.append(f"- {label}:")
    for value in clean_values[:8]:
        lines.append(f"  - {value}")


def _kind_label(record: AssetRecord) -> str:
    subtype = record.mime.split("/")[-1] if record.mime else "unknown"
    return f"{record.kind} ({subtype}, {_format_size(record.size_bytes)})"


def _format_size(size_bytes: int) -> str:
    size = float(max(size_bytes, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.0f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def _confidence_label(comprehension: ComprehensionRecord) -> str:
    score = float(comprehension.confidence_modifier or 0.0)
    if score >= 0.8:
        return "high"
    if score >= 0.55:
        return "medium"
    return "low"


def _truncate_block(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    limit = max(max_chars - len(_TRUNCATION_SUFFIX), 0)
    return text[:limit].rstrip() + _TRUNCATION_SUFFIX


def _readiness_snapshot(
    *,
    surface: AssetSurface,
    policy: AssetReadinessPolicy,
    timed_out: list[str],
) -> AssetReadinessReport:
    ready: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    minimal: list[str] = []
    for entry in surface.list_assets(include_deleted=False):
        status = entry.comprehension_status
        if status == "ready":
            ready.append(entry.record.id)
        elif status in {"pending", "running"}:
            pending.append(entry.record.id)
        elif status == "failed":
            failed.append(entry.record.id)
        elif status == "minimal":
            minimal.append(entry.record.id)
    return AssetReadinessReport(
        ready=ready,
        pending=pending,
        failed=failed,
        minimal=minimal,
        timed_out=timed_out,
        policy=policy,
    )


def _start_pending_comprehensions(surface: AssetSurface) -> list[str]:
    started: list[str] = []
    for entry in surface.list_assets(include_deleted=False):
        if entry.comprehension_status != "pending":
            continue
        try:
            surface.refresh_comprehension(entry.record.id, mode="async")
        except AssetError as exc:
            LOGGER.warning(
                "planner_assets_readiness failed to start comprehension asset_id=%s: %s",
                entry.record.id,
                exc,
            )
            continue
        started.append(entry.record.id)
    return started


def _collect_referenced_asset_ids_best_effort(plan: dict[str, Any]) -> set[str]:
    referenced: set[str] = set()
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        try:
            briefing = task_asset_briefing(task)
        except AssetError:
            continue
        if briefing is None:
            continue
        referenced.update(entry.asset_id for entry in briefing.primary)
        referenced.update(entry.asset_id for entry in briefing.may_need)
    return referenced


def _drift_block(report: AssetReferenceReport) -> str:
    lines = ["", "", "## Plan-Asset Drift", ""]
    if report.deleted_referenced:
        lines.append("Deleted assets are still referenced by tasks:")
        for task_id, asset_id in report.deleted_referenced:
            lines.append(f"- {task_id}: {asset_id}")
    if report.missing_referenced:
        lines.append("Missing assets are still referenced by tasks:")
        for task_id, asset_id in report.missing_referenced:
            lines.append(f"- {task_id}: {asset_id}")
    lines.append("Revise affected task asset_briefing entries before relying on them.")
    return "\n".join(lines)


def _model_supports_vision(model_registry: ModelRegistry, role_model: str) -> bool:
    try:
        return bool(model_registry.get(role_model).supports_vision)
    except Exception:  # noqa: BLE001 - metadata policy handles hard failures elsewhere
        return False


def absolute_asset_path(root: Path, stored_path: str) -> str:
    return str(project_artifact_path(root, stored_path).resolve())
