"""Public asset API; load optional processing only when an export is requested."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .asset_read_core import AssetReadResult, perform_asset_read
    from .budget_allocator import (
        AssetInclusionDecision,
        TaskAssetAllocation,
        allocate_task_assets,
    )
    from .comprehender import AssetComprehender
    from .index import AssetIndex
    from .ingestion import ingest_asset
    from .legacy_migration import (
        LegacyAssetMigrationResult,
        MigratedLegacyAsset,
        migrate_legacy_assets,
    )
    from .models import (
        AssetAlreadyExistsError,
        AssetError,
        AssetNotFoundError,
        AssetRecord,
        ComprehensionData,
        ComprehensionRecord,
        OcrError,
    )
    from .ocr import (
        OcrProvider,
        OcrResult,
        ScriptDetection,
        ScriptToLanguagesMapper,
        TesseractOcrProvider,
    )
    from .plan_binding import (
        AssetBriefingEntry,
        TaskAssetBriefing,
        bind_asset_to_matching_tasks,
        collect_referenced_asset_ids,
        infer_implicit_task_asset_briefing,
        parse_task_asset_briefing,
        serialize_task_asset_briefing,
        task_asset_briefing,
        task_asset_briefing_for_execution,
    )
    from .planner_context import (
        AssetReadinessPolicy,
        AssetReadinessReport,
        AssetReferenceReport,
        PlannerAssetsBundle,
        PlannerAssetsContext,
        asset_reference_check,
        build_planner_assets_block,
        build_planner_assets_bundle,
        ensure_planner_asset_readiness,
    )
    from .replanner_context import (
        PriorAssetUsageSummary,
        ReplannerAssetsBundle,
        build_replanner_assets_bundle,
    )
    from .surface import (
        AssetSurface,
        AssetSurfaceAddResult,
        AssetSurfaceDetail,
        AssetSurfaceEntry,
        AssetSurfaceJoinReport,
        ComprehensionRefreshHandle,
    )
    from .worker_mirror import MirroredAssetEntry, TaskAssetMirror, mirror_task_assets
    from .worker_section import render_relevant_assets_section

_EXPORT_MODULES = {
    "AssetAlreadyExistsError": "models",
    "AssetComprehender": "comprehender",
    "AssetError": "models",
    "AssetInclusionDecision": "budget_allocator",
    "AssetIndex": "index",
    "AssetNotFoundError": "models",
    "AssetRecord": "models",
    "AssetReadResult": "asset_read_core",
    "AssetReadinessPolicy": "planner_context",
    "AssetReadinessReport": "planner_context",
    "AssetReferenceReport": "planner_context",
    "AssetSurface": "surface",
    "AssetSurfaceAddResult": "surface",
    "AssetSurfaceDetail": "surface",
    "AssetSurfaceEntry": "surface",
    "AssetSurfaceJoinReport": "surface",
    "AssetBriefingEntry": "plan_binding",
    "ComprehensionData": "models",
    "ComprehensionRecord": "models",
    "ComprehensionRefreshHandle": "surface",
    "LegacyAssetMigrationResult": "legacy_migration",
    "MigratedLegacyAsset": "legacy_migration",
    "OcrError": "models",
    "OcrProvider": "ocr",
    "OcrResult": "ocr",
    "PlannerAssetsContext": "planner_context",
    "PlannerAssetsBundle": "planner_context",
    "PriorAssetUsageSummary": "replanner_context",
    "ReplannerAssetsBundle": "replanner_context",
    "ScriptDetection": "ocr",
    "ScriptToLanguagesMapper": "ocr",
    "TesseractOcrProvider": "ocr",
    "TaskAssetBriefing": "plan_binding",
    "TaskAssetAllocation": "budget_allocator",
    "TaskAssetMirror": "worker_mirror",
    "MirroredAssetEntry": "worker_mirror",
    "asset_reference_check": "planner_context",
    "allocate_task_assets": "budget_allocator",
    "bind_asset_to_matching_tasks": "plan_binding",
    "build_planner_assets_block": "planner_context",
    "build_planner_assets_bundle": "planner_context",
    "build_replanner_assets_bundle": "replanner_context",
    "collect_referenced_asset_ids": "plan_binding",
    "ensure_planner_asset_readiness": "planner_context",
    "infer_implicit_task_asset_briefing": "plan_binding",
    "ingest_asset": "ingestion",
    "migrate_legacy_assets": "legacy_migration",
    "mirror_task_assets": "worker_mirror",
    "parse_task_asset_briefing": "plan_binding",
    "perform_asset_read": "asset_read_core",
    "render_relevant_assets_section": "worker_section",
    "serialize_task_asset_briefing": "plan_binding",
    "task_asset_briefing": "plan_binding",
    "task_asset_briefing_for_execution": "plan_binding",
}

__all__ = [
    "AssetAlreadyExistsError",
    "AssetComprehender",
    "AssetError",
    "AssetInclusionDecision",
    "AssetIndex",
    "AssetNotFoundError",
    "AssetRecord",
    "AssetReadResult",
    "AssetReadinessPolicy",
    "AssetReadinessReport",
    "AssetReferenceReport",
    "AssetSurface",
    "AssetSurfaceAddResult",
    "AssetSurfaceDetail",
    "AssetSurfaceEntry",
    "AssetSurfaceJoinReport",
    "AssetBriefingEntry",
    "ComprehensionData",
    "ComprehensionRecord",
    "ComprehensionRefreshHandle",
    "LegacyAssetMigrationResult",
    "MigratedLegacyAsset",
    "OcrError",
    "OcrProvider",
    "OcrResult",
    "PlannerAssetsContext",
    "PlannerAssetsBundle",
    "PriorAssetUsageSummary",
    "ReplannerAssetsBundle",
    "ScriptDetection",
    "ScriptToLanguagesMapper",
    "TesseractOcrProvider",
    "TaskAssetBriefing",
    "TaskAssetAllocation",
    "TaskAssetMirror",
    "MirroredAssetEntry",
    "asset_reference_check",
    "allocate_task_assets",
    "bind_asset_to_matching_tasks",
    "build_planner_assets_block",
    "build_planner_assets_bundle",
    "build_replanner_assets_bundle",
    "collect_referenced_asset_ids",
    "ensure_planner_asset_readiness",
    "infer_implicit_task_asset_briefing",
    "ingest_asset",
    "migrate_legacy_assets",
    "mirror_task_assets",
    "parse_task_asset_briefing",
    "perform_asset_read",
    "render_relevant_assets_section",
    "serialize_task_asset_briefing",
    "task_asset_briefing",
    "task_asset_briefing_for_execution",
]


def __getattr__(name: str) -> Any:
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
