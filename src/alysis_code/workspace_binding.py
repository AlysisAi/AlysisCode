from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .repo_scan import _MANIFEST_SPECS, _README_NAMES, scan_workspace
from .runtime_artifacts import ROOT_RUNTIME_ARTIFACT_DIR_NAMES
from .workspace_context import WorkspaceContext, WorkspaceContextError, resolve_workspace_context

_PROJECT_SIGNAL_DIR_NAMES = frozenset({"src", "tests"})
_BROAD_PLAIN_DIR_THRESHOLD = 12
_PROJECT_CONTAINER_DIR_NAMES = frozenset({"code", "dev", "projects", "repos", "src", "work"})
_MAX_DISCOVERY_CHILDREN = 24
_MAX_DISCOVERY_RESULTS = 8


class WorkspaceBindingError(RuntimeError):
    pass


class WorkspaceRiskLevel:
    HEALTHY = "healthy"
    GUARDED = "guarded"
    BLOCKED = "blocked"


class WorkspaceAction:
    CHAT = "chat"
    FORGE = "forge"
    FORGE_PLAN = "forge_plan"
    FORGE_RUN = "forge_run"
    SWARM = "swarm"


@dataclass(frozen=True)
class WorkspacePolicy:
    action: str
    allow_guarded_override: bool
    interactive_guarded_resolution: bool
    interactive_allow_use_current: bool


@dataclass(frozen=True)
class WorkspaceBinding:
    requested_path: Path
    resolved_candidate_path: Path
    created_path: bool
    workspace_context: WorkspaceContext
    binding_source: str
    risk_level: str
    broad_workspace_override_used: bool = False
    risk_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceCandidate:
    path: Path
    score: int
    project_signals: tuple[str, ...]
    source: str

    @property
    def summary(self) -> str:
        if self.project_signals:
            return ", ".join(self.project_signals)
        return "existing directory"


def prepare_workspace_path(path: Path, *, create_if_missing: bool = False) -> tuple[Path, bool]:
    requested_path = path.expanduser().resolve(strict=False)
    if requested_path.exists():
        if not requested_path.is_dir():
            raise WorkspaceBindingError(f"Workspace path is not a directory: {requested_path}")
        return requested_path.resolve(), False

    if not create_if_missing:
        raise WorkspaceBindingError(
            f"Workspace path does not exist: {requested_path}. "
            "Pass create_if_missing=True to bootstrap it."
        )

    try:
        requested_path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as e:
        raise WorkspaceBindingError(f"Workspace path is not a directory: {requested_path}") from e
    except OSError as e:
        raise WorkspaceBindingError(f"Unable to create workspace path: {requested_path}") from e
    return requested_path.resolve(), True


def resolve_workspace_binding(
    path: Path,
    *,
    create_if_missing: bool = False,
    allow_broad_workspace: bool = False,
    source: str = "explicit_path",
    allow_subprocess: bool = True,
) -> WorkspaceBinding:
    requested_path, created_path = prepare_workspace_path(
        path,
        create_if_missing=create_if_missing,
    )
    try:
        workspace_context = resolve_workspace_context(
            requested_path,
            allow_subprocess=allow_subprocess,
        )
    except WorkspaceContextError as e:
        raise WorkspaceBindingError(str(e)) from e

    risk_level, risk_reasons = classify_workspace_risk(workspace_context)
    warnings: list[str] = []
    broad_workspace_override_used = False
    if created_path:
        warnings.append("Workspace directory was created at startup.")
    if risk_level == WorkspaceRiskLevel.GUARDED and allow_broad_workspace:
        broad_workspace_override_used = True
        warnings.append("Guarded workspace accepted because allow_broad_workspace=True.")
    return WorkspaceBinding(
        requested_path=requested_path,
        resolved_candidate_path=workspace_context.input_path,
        created_path=created_path,
        workspace_context=workspace_context,
        binding_source=(source.strip() or "explicit_path"),
        risk_level=risk_level,
        broad_workspace_override_used=broad_workspace_override_used,
        risk_reasons=risk_reasons,
        warnings=tuple(warnings),
    )


def workspace_policy(action: str) -> WorkspacePolicy:
    normalized = str(action or "").strip().lower()
    if normalized == WorkspaceAction.FORGE:
        return WorkspacePolicy(
            action=WorkspaceAction.FORGE,
            allow_guarded_override=False,
            interactive_guarded_resolution=False,
            interactive_allow_use_current=False,
        )
    if normalized == WorkspaceAction.FORGE_PLAN:
        return WorkspacePolicy(
            action=WorkspaceAction.FORGE_PLAN,
            allow_guarded_override=True,
            interactive_guarded_resolution=True,
            interactive_allow_use_current=False,
        )
    if normalized == WorkspaceAction.FORGE_RUN:
        # Same strength as swarm: both execute plan tasks and mutate the workspace.
        # It gets its own action so the guard message names the command the user ran.
        return WorkspacePolicy(
            action=WorkspaceAction.FORGE_RUN,
            allow_guarded_override=True,
            interactive_guarded_resolution=False,
            interactive_allow_use_current=False,
        )
    if normalized == WorkspaceAction.SWARM:
        return WorkspacePolicy(
            action=WorkspaceAction.SWARM,
            allow_guarded_override=True,
            interactive_guarded_resolution=False,
            interactive_allow_use_current=False,
        )
    return WorkspacePolicy(
        action=WorkspaceAction.CHAT,
        allow_guarded_override=True,
        interactive_guarded_resolution=True,
        interactive_allow_use_current=True,
    )


def workspace_action_label(action: str) -> str:
    normalized = workspace_policy(action).action
    if normalized == WorkspaceAction.FORGE:
        return "Forge"
    if normalized == WorkspaceAction.FORGE_PLAN:
        return "forge plan"
    if normalized == WorkspaceAction.FORGE_RUN:
        return "forge run"
    if normalized == WorkspaceAction.SWARM:
        return "forge swarm"
    return "chat/run startup"


def workspace_policy_violation_message(
    binding: WorkspaceBinding,
    *,
    action: str,
) -> str:
    policy = workspace_policy(action)
    label = workspace_action_label(policy.action)
    root_label = os.fspath(binding.workspace_context.workspace_root)
    requested_label = os.fspath(binding.requested_path)
    detail = "; ".join(binding.risk_reasons) or "workspace selection is not allowed"
    common_guidance = "Use --path <project>, choose/create a project folder, or start alysis from inside a project."
    if binding.risk_level == WorkspaceRiskLevel.BLOCKED:
        return f"{label} is blocked for {requested_label}: {detail}. {common_guidance}"
    if policy.action == WorkspaceAction.FORGE:
        return (
            f"{label} requires a narrower workspace than {root_label}: {detail}. {common_guidance}"
        )
    if policy.action in {WorkspaceAction.FORGE_RUN, WorkspaceAction.SWARM}:
        return (
            f"{label} requires a healthy workspace. Current binding for {requested_label} is "
            f"guarded: {detail}. {common_guidance} "
            f"Pass --allow-broad-workspace only if you intentionally want {label} to operate "
            "on this broad workspace."
        )
    if policy.action == WorkspaceAction.FORGE_PLAN:
        return (
            f"{label} is guarded for {requested_label}: {detail}. {common_guidance} "
            "Pass --allow-broad-workspace only if you intentionally want to plan from this "
            "broad workspace."
        )
    return (
        f"{label} is guarded for {requested_label}: {detail}. {common_guidance} "
        "Pass --allow-broad-workspace to continue non-interactively."
    )


def ensure_workspace_policy(
    binding: WorkspaceBinding,
    *,
    action: str,
    allow_broad_workspace: bool = False,
) -> WorkspaceBinding:
    policy = workspace_policy(action)
    if binding.risk_level == WorkspaceRiskLevel.HEALTHY:
        return binding
    if binding.risk_level == WorkspaceRiskLevel.BLOCKED:
        raise WorkspaceBindingError(
            workspace_policy_violation_message(binding, action=policy.action)
        )
    if policy.allow_guarded_override and allow_broad_workspace:
        return binding
    raise WorkspaceBindingError(workspace_policy_violation_message(binding, action=policy.action))


def classify_workspace_risk(workspace_context: WorkspaceContext) -> tuple[str, tuple[str, ...]]:
    workspace_root = workspace_context.workspace_root
    if _is_filesystem_root(workspace_root):
        return (
            WorkspaceRiskLevel.BLOCKED,
            ("filesystem root '/' is not a valid forge workspace",),
        )

    home_dir = _home_directory()
    if home_dir is not None and workspace_root == home_dir:
        return (
            WorkspaceRiskLevel.GUARDED,
            ("home directory is a broad workspace; pass an explicit override to continue",),
        )

    if workspace_context.git_root is not None:
        return (WorkspaceRiskLevel.HEALTHY, ())

    return _classify_plain_directory(workspace_context)


def _classify_plain_directory(workspace_context: WorkspaceContext) -> tuple[str, tuple[str, ...]]:
    scan = scan_workspace(context=workspace_context)
    top_level_names = {entry.get("path", "") for entry in scan.top_level_entries}
    if not top_level_names:
        return (WorkspaceRiskLevel.HEALTHY, ())

    signals = project_signals_for_path(workspace_context.workspace_root)
    has_project_signals = bool(signals)
    if has_project_signals:
        return (WorkspaceRiskLevel.HEALTHY, ())

    if len(top_level_names) >= _BROAD_PLAIN_DIR_THRESHOLD:
        return (
            WorkspaceRiskLevel.GUARDED,
            ("plain directory has many top-level entries without strong project signals",),
        )

    return (WorkspaceRiskLevel.HEALTHY, ())


def _home_directory() -> Path | None:
    try:
        return Path.home().resolve()
    except OSError:
        return None


def _is_filesystem_root(path: Path) -> bool:
    normalized = path.resolve()
    return normalized == normalized.parent and normalized == Path(os.path.abspath(os.sep))


def project_signals_for_path(path: Path) -> tuple[str, ...]:
    resolved = path.expanduser().resolve(strict=False)
    signals: list[str] = []
    git_dir = resolved / ".git"
    if git_dir.exists():
        signals.append(".git")

    manifest_names = [name for name, _kind in _MANIFEST_SPECS if (resolved / name).is_file()]
    signals.extend(manifest_names[:3])

    if any((resolved / name).is_file() for name in _README_NAMES):
        signals.append("README")
    if (resolved / "src").is_dir():
        signals.append("src/")
    if (resolved / "tests").is_dir():
        signals.append("tests/")
    return tuple(signals)


def discover_workspace_candidates(
    path: Path,
    *,
    limit: int = _MAX_DISCOVERY_RESULTS,
) -> tuple[WorkspaceCandidate, ...]:
    base_dir = path.expanduser().resolve()
    if not base_dir.exists() or not base_dir.is_dir():
        return ()

    candidates: dict[Path, WorkspaceCandidate] = {}
    immediate_children = _iter_candidate_dirs(base_dir, max_children=_MAX_DISCOVERY_CHILDREN)
    for child in immediate_children:
        _maybe_record_candidate(
            candidates,
            child,
            source="child",
        )
        if child.name.casefold() not in _PROJECT_CONTAINER_DIR_NAMES:
            continue
        for nested in _iter_candidate_dirs(child, max_children=_MAX_DISCOVERY_CHILDREN):
            _maybe_record_candidate(
                candidates,
                nested,
                source=f"nested:{child.name}",
            )

    ranked = sorted(
        candidates.values(),
        key=lambda item: (-item.score, item.path.name.casefold(), os.fspath(item.path)),
    )
    return tuple(ranked[: max(1, limit)])


def _iter_candidate_dirs(path: Path, *, max_children: int) -> list[Path]:
    children: list[Path] = []
    try:
        entries = sorted(path.iterdir(), key=lambda candidate: candidate.name.casefold())
    except OSError:
        return []
    for candidate in entries:
        if len(children) >= max_children:
            break
        if not candidate.is_dir():
            continue
        if candidate.name.startswith(".") or candidate.name in ROOT_RUNTIME_ARTIFACT_DIR_NAMES:
            continue
        children.append(candidate.resolve())
    return children


def _maybe_record_candidate(
    existing: dict[Path, WorkspaceCandidate],
    path: Path,
    *,
    source: str,
) -> None:
    signals = project_signals_for_path(path)
    if not signals:
        return
    score = _project_signal_score(signals)
    candidate = WorkspaceCandidate(
        path=path,
        score=score,
        project_signals=signals,
        source=source,
    )
    current = existing.get(path)
    if current is None or candidate.score > current.score:
        existing[path] = candidate


def _project_signal_score(signals: tuple[str, ...]) -> int:
    score = 0
    for signal in signals:
        if signal == ".git":
            score += 100
        elif signal in {"src/", "tests/", "README"}:
            score += 10
        else:
            score += 20
    return score
