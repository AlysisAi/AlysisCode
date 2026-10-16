"""Lossless Phase-0 bridge from native Forge plans to Host Protocol v2.

The adapter is intentionally narrower than the native planner schema.  It
admits the first vertical slice (one local implementation task, sequential
execution, and one repo-native pytest verifier) or fails closed.  All checks in
this module are read-only except persisting deterministic task branch *names*
to the native plan; no Git reference is created or updated here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

from .command_text import command_text
from .forge import RunPaths, requirement_text, save_plan
from .forge_host_protocol.canonical import (
    approval_policy_fingerprint,
    approval_scope_fingerprint,
    canonical_fingerprint,
    canonical_json_bytes,
    execution_admission_fingerprint,
    plan_fingerprint,
    verification_contract_fingerprint,
)
from .forge_host_protocol.models import (
    PHASE0_APPROVAL_SCOPE_LABEL,
    PHASE0_APPROVAL_SUMMARY,
    AcceptanceCriterion,
    ExecutionAdmission,
    FilesystemApprovalResource,
    FilesystemApprovalScope,
    NativeApprovalDefinition,
    NativeIntentRecord,
    NativePlanningConstraints,
    NativePlanningPathConstraint,
    NativePlanTask,
    PlanApprovalPolicy,
    PlanDefinition,
    PlanIntent,
    PlanningConstraints,
    PlanningPathConstraint,
    PlanReady,
    PlanTask,
    SequentialExecutionStrategy,
    VerificationCheckDefinition,
    VerificationCommand,
    VerificationContract,
    VerificationRequirement,
    VerificationScope,
    WorkspacePathPolicy,
    WorkspaceScope,
)
from .git_ops import generate_task_branch_name
from .repo_scan import scan_workspace
from .runtime_artifacts import is_runtime_artifact_path
from .verification_contract import VerificationCommandSpec, build_verification_command_specs
from .workspace_context import resolve_workspace_context

IntentKind = Literal["build", "fix", "refactor", "audit", "test", "document"]
ScopeKind = Literal["file", "directory"]
_HOST_INTENT_SUMMARY_MAX_CHARS = 1000


class ForgeHostPreflightError(RuntimeError):
    """A stable, non-secret admission failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ProviderBinding:
    """Opaque provider selection captured before execution approval."""

    provider_binding_ref: str
    provider: str
    model: str
    account_fingerprint: str | None = None


@dataclass(frozen=True)
class WorkspaceGitBinding:
    workspace_ref: str
    workspace_root: Path
    root_device: int
    root_inode: int
    git_root: Path
    base_branch: str
    base_head: str
    path_comparison: Literal["case_sensitive", "case_insensitive"]


@dataclass(frozen=True)
class NativeTaskDiagnostic:
    task_id: str
    task_kind_reason: str


@dataclass(frozen=True)
class PytestExecutableBinding:
    """Private identity of the executable authorized to run canonical pytest."""

    resolved_path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class NativePlanAdmission:
    """Immutable authority plus the private native execution binding."""

    paths: RunPaths
    plan_ready: PlanReady
    native_plan_snapshot: Mapping[str, Any]
    native_plan_snapshot_digest: str
    native_run_id: str
    workspace_git: WorkspaceGitBinding
    provider_binding: ProviderBinding
    pytest_executable: PytestExecutableBinding
    verification_specs: tuple[VerificationCommandSpec, ...]
    diagnostics: tuple[NativeTaskDiagnostic, ...]


@dataclass(frozen=True)
class NativeAdapterRequest:
    paths: RunPaths
    plan: dict[str, Any]
    planner_input: str
    workspace_ref: str
    provider_binding: ProviderBinding
    per_managed_task_max_steps: int
    run_timeout_ms: int
    intent_kind: IntentKind | None = None
    verification_commands: Sequence[str] | None = None
    trusted_pytest: Path | None = None
    declared_scope_kinds: Mapping[str, ScopeKind] = field(
        default_factory=lambda: MappingProxyType({})
    )


GitRunner = Callable[[Path, Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class NativeAdapterDependencies:
    git_runner: GitRunner | None = None
    plan_saver: Callable[[RunPaths, dict[str, Any]], None] = save_plan
    current_provider_binding: Callable[[], ProviderBinding] | None = None
    pytest_resolver: Callable[[], Path] | None = None


_INTENT_VERBS: dict[IntentKind, frozenset[str]] = {
    "build": frozenset({"add", "build", "create", "implement", "introduce"}),
    "fix": frozenset({"correct", "fix", "patch", "repair", "resolve"}),
    "refactor": frozenset({"refactor", "reorganize", "restructure"}),
    "audit": frozenset({"analyze", "audit", "inspect", "investigate", "review"}),
    "test": frozenset({"check", "test", "verify"}),
    "document": frozenset({"document", "documenting", "docs"}),
}
_NATIVE_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "created_at",
        "updated_at",
        "project_goal",
        "summary",
        "requirements",
        "tasks",
        "planning_constraints",
        "assets",
        "plan_status",
        "plan_status_detail",
        "plan_repair",
    }
)
_NATIVE_TASK_FIELDS = frozenset(
    {
        "id",
        "title",
        "description",
        "acceptance_criteria",
        "dependencies",
        "estimated_files",
        "write_scope",
        "branch",
        "status",
        "attempts",
        "task_kind",
        "task_kind_reason",
        "analysis_only",
        "parallel_group",
    }
)


def _fail(code: str, message: str) -> None:
    raise ForgeHostPreflightError(code, message)


def _default_git_runner(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return subprocess.run(
        ["git", "-C", os.fspath(root), *args],
        check=False,
        capture_output=True,
        **command_text(),
        env=env,
    )


def _git(
    runner: GitRunner,
    root: Path,
    args: Sequence[str],
    *,
    code: str,
) -> str:
    result = runner(root, args)
    if result.returncode != 0:
        _fail(code, "Git preflight failed for the selected workspace.")
    return (result.stdout or "").strip()


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def detect_filesystem_path_comparison(root: Path) -> Literal["case_sensitive", "case_insensitive"]:
    """Probe existing directory entries without creating a probe file."""

    for entry in os.scandir(root):
        alternate = "".join(
            character.swapcase() if character.isalpha() else character for character in entry.name
        )
        if alternate == entry.name:
            continue
        alternate_path = root / alternate
        if os.path.lexists(alternate_path) and _same_file(Path(entry.path), alternate_path):
            return "case_insensitive"
    return "case_sensitive"


def _comparison_key(value: str, *, comparison: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return normalized if comparison == "case_sensitive" else normalized.casefold()


def _normalize_scope_path(raw: Any) -> tuple[str, bool]:
    value = str(raw) if isinstance(raw, str) else ""
    if not value or value != value.strip() or unicodedata.normalize("NFC", value) != value:
        _fail("invalid_scope_path", "Plan scopes must be non-empty NFC paths without padding.")
    if value.startswith(("/", "~", "\\")) or "\\" in value or ":" in value:
        _fail("invalid_scope_path", "Plan scopes must be normalized workspace-relative paths.")
    recursive = value.endswith("/**")
    base = value[:-3] if recursive else value
    if any(character in base for character in "*?[]"):
        _fail("invalid_scope_path", "Only a terminal '/**' recursive scope is supported.")
    parts = PurePosixPath(base).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        _fail("invalid_scope_path", "Plan scopes must be traversal-free relative paths.")
    normalized = PurePosixPath(*parts).as_posix()
    if normalized != base:
        _fail("invalid_scope_path", "Plan scopes must use canonical POSIX spelling.")
    return value, recursive


def _assert_resolves_beneath(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError:
        _fail("scope_escapes_workspace", "A scoped path resolves outside the workspace.")
    return resolved


def _scope_from_native(
    raw: Any,
    *,
    root: Path,
    comparison: str,
    declared_scope_kinds: Mapping[str, ScopeKind],
    required_kind: ScopeKind | None = None,
) -> WorkspaceScope:
    value, recursive = _normalize_scope_path(raw)
    base = value[:-3] if recursive else value
    current = root
    missing = False
    actual_kind: ScopeKind | None = None
    for component in PurePosixPath(base).parts:
        if missing:
            current /= component
            continue
        try:
            entries = list(os.scandir(current))
        except OSError:
            _fail("scope_lstat_failed", "A scoped path ancestor cannot be inspected safely.")
        aliases = [
            entry
            for entry in entries
            if _comparison_key(entry.name, comparison=comparison)
            == _comparison_key(component, comparison=comparison)
        ]
        normalized_aliases = {
            _comparison_key(entry.name, comparison=comparison) for entry in aliases
        }
        if len(aliases) > 1 or len(normalized_aliases) > 1:
            _fail("scope_path_alias", "A scoped path has ambiguous filesystem aliases.")
        if not aliases:
            missing = True
            current /= component
            continue
        entry = aliases[0]
        if entry.name != component or unicodedata.normalize("NFC", entry.name) != entry.name:
            _fail("scope_path_alias", "A scoped path does not match its real filesystem spelling.")
        current = Path(entry.path)
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            _fail("scope_lstat_failed", "A scoped path cannot be inspected safely.")
        if stat.S_ISLNK(mode):
            resolved = _assert_resolves_beneath(root, current)
            current = resolved
        elif component != PurePosixPath(base).parts[-1] and not stat.S_ISDIR(mode):
            _fail("scope_ancestor_not_directory", "A scoped path ancestor is not a directory.")

    if missing:
        ancestor = current
        while not os.path.lexists(ancestor):
            if ancestor == root:
                break
            ancestor = ancestor.parent
        _assert_resolves_beneath(root, ancestor)
    else:
        resolved = _assert_resolves_beneath(root, current)
        mode = os.lstat(resolved).st_mode
        if stat.S_ISDIR(mode):
            actual_kind = "directory"
        elif stat.S_ISREG(mode):
            actual_kind = "file"
        else:
            _fail("unsupported_scope_kind", "A scoped path is not a regular file or directory.")

    declared = declared_scope_kinds.get(value) or declared_scope_kinds.get(base)
    kind: ScopeKind = (
        "directory" if recursive else actual_kind or declared or required_kind or "file"
    )
    if recursive and declared not in {None, "directory"}:
        _fail("scope_kind_mismatch", "Recursive scopes must name directories.")
    if actual_kind is not None and declared is not None and declared != actual_kind:
        _fail("scope_kind_mismatch", "A declared scope kind conflicts with lstat.")
    if required_kind is not None and kind != required_kind:
        _fail("scope_kind_mismatch", "A planning root must be a directory scope.")
    return WorkspaceScope(path=value, kind=kind)


def _workspace_is_clean(runner: GitRunner, root: Path) -> bool:
    for args in (("diff", "--quiet", "--"), ("diff", "--cached", "--quiet", "--")):
        result = runner(root, args)
        if result.returncode not in {0, 1}:
            _fail("git_status_failed", "Git cleanliness could not be inspected.")
        if result.returncode == 1:
            return False
    result = runner(root, ("ls-files", "--others", "--exclude-standard", "-z"))
    if result.returncode != 0:
        _fail("git_status_failed", "Git untracked files could not be inspected.")
    untracked = [item for item in (result.stdout or "").split("\0") if item]
    if any(not is_runtime_artifact_path(item, root=root) for item in untracked):
        return False

    # ``git status`` and the ordinary untracked inventory deliberately omit
    # ignored material.  That is unsafe for an execution authority boundary:
    # an existing ignored target could contain user data, and an agent could
    # overwrite it without either the scope gate or Git attribution seeing the
    # preimage.  Phase 0 therefore admits only exact Alysis Code runtime ignored
    # entries. ``--directory`` keeps large ignored trees bounded to their root
    # entry; a later isolated-worktree profile can safely relax this policy.
    ignored_result = runner(
        root,
        (
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        ),
    )
    if ignored_result.returncode != 0:
        _fail("git_status_failed", "Git ignored files could not be inspected.")
    ignored = [item for item in (ignored_result.stdout or "").split("\0") if item]
    return not any(not is_runtime_artifact_path(item, root=root) for item in ignored)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ForgeHostPreflightError(
            "pytest_identity_failed", "The trusted pytest executable cannot be read."
        ) from exc
    return digest.hexdigest()


def capture_pytest_executable_binding(
    executable: Path,
    *,
    workspace_root: Path,
) -> PytestExecutableBinding:
    """Capture strong, private identity for one explicitly trusted executable."""

    try:
        resolved = executable.expanduser().resolve(strict=True)
        workspace = workspace_root.resolve(strict=True)
        metadata = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise ForgeHostPreflightError(
            "pytest_identity_failed", "The trusted pytest executable is unavailable."
        ) from exc
    try:
        resolved.relative_to(workspace)
    except ValueError:
        pass
    else:
        _fail(
            "pytest_inside_workspace",
            "The trusted pytest executable must be outside the mutable workspace.",
        )
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        _fail(
            "pytest_not_executable",
            "The trusted pytest executable is not an executable regular file.",
        )
    if not resolved.name.casefold().startswith(("pytest", "py.test")):
        _fail("pytest_identity_failed", "The trusted executable is not pytest.")
    return PytestExecutableBinding(
        resolved_path=resolved,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        sha256=_sha256_file(resolved),
    )


def revalidate_pytest_executable_binding(
    binding: PytestExecutableBinding,
    *,
    workspace_root: Path,
) -> None:
    """Fail if the approved pytest executable was replaced or modified."""

    current = capture_pytest_executable_binding(
        binding.resolved_path,
        workspace_root=workspace_root,
    )
    if current != binding:
        _fail(
            "pytest_identity_changed",
            "The trusted pytest executable changed after approval.",
        )


def _assert_branch_available(
    runner: GitRunner,
    root: Path,
    branch: str,
    *,
    comparison: str,
) -> None:
    result = runner(root, ("check-ref-format", "--branch", branch))
    if result.returncode != 0 or (result.stdout or "").strip() != branch:
        _fail("invalid_task_branch", "The deterministic task branch is not Git-valid.")
    refs = _git(
        runner,
        root,
        ("for-each-ref", "--format=%(refname:strip=2)", "refs/heads"),
        code="git_refs_failed",
    ).splitlines()
    candidate = _comparison_key(branch, comparison=comparison)
    for ref in refs:
        existing = _comparison_key(ref, comparison=comparison)
        if (
            candidate == existing
            or candidate.startswith(f"{existing}/")
            or existing.startswith(f"{candidate}/")
        ):
            _fail("task_branch_collision", "The deterministic task branch collides with a ref.")


def inspect_workspace_git_binding(
    *,
    paths: RunPaths,
    workspace_ref: str,
    runner: GitRunner | None = None,
) -> WorkspaceGitBinding:
    git_runner = runner or _default_git_runner
    root = paths.root.resolve(strict=True)
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode):
        _fail("workspace_not_directory", "The selected workspace is not a directory.")
    top = Path(
        _git(
            git_runner,
            root,
            ("rev-parse", "--show-toplevel"),
            code="not_git_repository",
        )
    ).resolve(strict=True)
    if top != root:
        _fail("workspace_git_mismatch", "The selected workspace is not the Git top level.")
    branch = _git(
        git_runner,
        root,
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        code="detached_head",
    )
    head = _git(
        git_runner,
        root,
        ("rev-parse", "--verify", "HEAD^{commit}"),
        code="missing_base_head",
    )
    if len(head) not in {40, 64} or any(character not in "0123456789abcdef" for character in head):
        _fail("invalid_base_head", "The selected workspace HEAD is not a full commit ID.")
    if not _workspace_is_clean(git_runner, root):
        _fail("dirty_workspace", "The selected workspace contains non-runtime changes.")
    return WorkspaceGitBinding(
        workspace_ref=workspace_ref,
        workspace_root=root,
        root_device=root_stat.st_dev,
        root_inode=root_stat.st_ino,
        git_root=top,
        base_branch=branch,
        base_head=head,
        path_comparison=detect_filesystem_path_comparison(root),
    )


def _intent_kind(planner_input: str, explicit: IntentKind | None) -> IntentKind:
    text = planner_input.strip()
    if not text:
        _fail("missing_planner_input", "The immutable plan requires explicit planner input.")
    first = text.split(maxsplit=1)[0].casefold().strip(".,:;!?")
    inferred = [kind for kind, verbs in _INTENT_VERBS.items() if first in verbs]
    if explicit is not None:
        if inferred and inferred != [explicit]:
            _fail("ambiguous_intent", "Explicit intent conflicts with the planner input.")
        return explicit
    if len(inferred) != 1:
        _fail("ambiguous_intent", "Planner input does not map to one Phase-0 intent kind.")
    return inferred[0]


def _bounded_intent_summary(planner_input: str) -> str:
    """Project immutable planner input into the bounded Host display field.

    Host planner input may include an App-owned execution-contract appendix and
    is preserved in full for planning and resume authority.  ``PlanIntent`` is
    deliberately smaller, so long input uses a readable first paragraph plus
    a digest of the complete normalized input.  The digest prevents two long
    inputs with the same prefix from producing identical plan authority.
    """

    text = planner_input.strip()
    if not text:
        _fail("missing_planner_input", "The immutable plan requires explicit planner input.")
    if len(text) <= _HOST_INTENT_SUMMARY_MAX_CHARS:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    suffix = f"\n\n[full-input-sha256:{digest}]"
    available = _HOST_INTENT_SUMMARY_MAX_CHARS - len(suffix)
    readable = text.split("\n\n", maxsplit=1)[0].strip()
    if not readable:
        readable = text
    return readable[:available].rstrip() + suffix


def _constraint_collection(
    raw: Any,
    *,
    expected_kind: str,
    root: Path,
    comparison: str,
    declared_scope_kinds: Mapping[str, ScopeKind],
) -> tuple[list[NativePlanningPathConstraint], list[PlanningPathConstraint]]:
    if not isinstance(raw, list):
        _fail("invalid_constraints", "Planning constraint collections must be arrays.")
    native: list[NativePlanningPathConstraint] = []
    host: list[PlanningPathConstraint] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "path",
            "kind",
            "reason_code",
            "evidence",
        }:
            _fail("invalid_constraints", "Planning constraints must use the canonical shape.")
        if item.get("kind") != expected_kind:
            _fail("invalid_constraints", "Planning constraint kind does not match its collection.")
        scope = _scope_from_native(
            item.get("path"),
            root=root,
            comparison=comparison,
            declared_scope_kinds=declared_scope_kinds,
            required_kind="directory",
        )
        payload = {
            "path": scope.path,
            "kind": expected_kind,
            "reason_code": item.get("reason_code"),
            "evidence": item.get("evidence"),
        }
        try:
            native.append(NativePlanningPathConstraint.model_validate(payload, strict=True))
            host.append(PlanningPathConstraint.model_validate(payload, strict=True))
        except Exception as exc:
            raise ForgeHostPreflightError(
                "invalid_constraints", "Planning constraints are not losslessly representable."
            ) from exc
    return native, host


def _build_verification_contract(
    *,
    task_id: str,
    acceptance: Sequence[AcceptanceCriterion],
    writes: Sequence[WorkspaceScope],
    workspace_ref: str,
    host_fingerprint: str,
    native_fingerprint: str,
    commands: Sequence[str],
    command_source: str,
) -> tuple[VerificationContract, tuple[VerificationCommandSpec, ...]]:
    selected = [str(command).strip() for command in commands if str(command).strip()]
    if len(selected) != 1 or selected[0] not in {"pytest", "pytest -q"}:
        _fail("missing_assertive_verifier", "Phase 0 requires repo-native pytest or pytest -q.")
    command_text = selected[0]
    criterion_ids = tuple(item.criterion_id for item in acceptance)
    specs = build_verification_command_specs(
        [command_text],
        source=command_source,
        contract_type="repo_native",
        acceptance_criterion_ids=criterion_ids,
    )
    if len(specs) != 1:
        _fail("invalid_verification_command", "Verification selection was not singular.")
    spec = specs[0]
    if (
        spec.execution_mode.value != "ARGV"
        or spec.provenance.value not in {"PREEXISTING_REPO_NATIVE", "HOST_AUTHORITATIVE"}
        or spec.trust_level.value != "TRUSTED"
        or spec.requirement.value != "REQUIRED"
        or spec.validation_status.value != "VALID"
        or spec.evidentiary_capability.value != "ASSERTIVE"
    ):
        _fail("invalid_verification_command", "Selected verification is not assertive authority.")

    referenced = sorted(
        {scope.path.removesuffix("/**") for scope in writes if not scope.path.endswith("/**")}
    )
    if not referenced:
        _fail("invalid_verification_scope", "Phase 0 verification requires a concrete output.")
    scope_id = f"scope.{task_id}"
    requirement_models = [
        VerificationRequirement(
            requirement_id=criterion.criterion_id,
            task_id=task_id,
            text=criterion.description,
            provenance="HOST_AUTHORITATIVE",
            trust_level="TRUSTED",
            requirement="REQUIRED",
        )
        for criterion in acceptance
    ]
    command_id = "command.pytest"
    # The native analyzer assigns a private stable ``vc_*`` identifier, while
    # Host v2 deliberately uses one canonical protocol command identity. Keep
    # both authority views byte-for-byte aligned before admission; the executor
    # must never fall back to argv-only matching.
    spec = replace(spec, command_id=command_id)
    specs = (spec,)
    command_model = VerificationCommand(
        command_id=command_id,
        task_id=task_id,
        scope_id=scope_id,
        original_text=command_text,
        display_text=command_text,
        execution_mode="ARGV",
        provenance=spec.provenance.value,
        trust_level="TRUSTED",
        requirement="REQUIRED",
        timeout_policy=spec.timeout_policy,
        requirement_ids=list(criterion_ids),
        validation_status="VALID",
        rejection_reason="",
        evidentiary_capability="ASSERTIVE",
        capability_reason=spec.capability_reason,
        argv=list(spec.argv),
    )
    contract_payload: dict[str, Any] = {
        "schema": "alysis.forge.verification-contract/v2",
        "protocolVersion": 2,
        "workspaceRef": workspace_ref,
        "approvedPlanFingerprint": native_fingerprint,
        "hostPlanFingerprint": host_fingerprint,
        "scopes": [
            VerificationScope(
                scope_id=scope_id,
                phase="task",
                task_id=task_id,
                cwd=".",
                referenced_paths=referenced,
                allowed_outputs=referenced,
                declared_inputs=[],
            )
        ],
        "requirements": requirement_models,
        "commands": [command_model],
        "checks": [
            VerificationCheckDefinition(
                check_id=f"check.acceptance.{task_id}",
                domain="acceptance",
                task_id=task_id,
                requirement_ids=list(criterion_ids),
                command_ids=[],
            ),
            VerificationCheckDefinition(
                check_id=f"check.verification.{task_id}",
                domain="verification",
                task_id=task_id,
                requirement_ids=list(criterion_ids),
                command_ids=[command_id],
            ),
        ],
        "contractFingerprint": "0" * 64,
    }
    contract_payload["contractFingerprint"] = verification_contract_fingerprint(contract_payload)
    return VerificationContract.model_validate(contract_payload, strict=True), specs


def build_native_plan_admission(
    request: NativeAdapterRequest,
    *,
    dependencies: NativeAdapterDependencies | None = None,
) -> NativePlanAdmission:
    """Preflight and bind one finalized native plan to Host-v2 authority."""

    deps = dependencies or NativeAdapterDependencies()
    runner = deps.git_runner or _default_git_runner
    plan = request.plan
    unknown_plan_fields = set(plan).difference(_NATIVE_PLAN_FIELDS)
    if unknown_plan_fields:
        _fail("lossy_native_plan", "The native plan contains unsupported authority fields.")
    if str(plan.get("run_id") or "") != request.paths.run_id:
        _fail("native_run_mismatch", "The native plan run ID does not match its reserved paths.")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        _fail("unsupported_task_count", "Phase 0 requires exactly one native plan task.")
    if plan.get("assets") not in (None, []):
        _fail("assets_deferred", "Assets are not admitted by the Phase-0 Host bridge.")
    task = tasks[0]
    if set(task).difference(_NATIVE_TASK_FIELDS):
        _fail("lossy_native_task", "The native task contains unsupported authority fields.")
    if task.get("mcp_scope") or task.get("parallel_group") or task.get("asset_briefing"):
        _fail("deferred_task_authority", "Phase 0 does not admit MCP, assets, or parallel tasks.")
    if str(task.get("status") or "planned") != "planned":
        _fail("task_not_planned", "The admitted native task must still be planned.")
    if int(task.get("attempts") or 0) != 0:
        _fail("task_already_attempted", "The admitted native task must not have prior attempts.")
    recorded_plan_status = str(plan.get("plan_status") or "").strip()
    if recorded_plan_status and recorded_plan_status != "execution_ready":
        _fail("plan_not_ready", "The native plan is not marked execution-ready.")

    workspace_git = inspect_workspace_git_binding(
        paths=request.paths,
        workspace_ref=request.workspace_ref,
        runner=runner,
    )
    root = workspace_git.workspace_root
    comparison = workspace_git.path_comparison
    pytest_path = request.trusted_pytest
    if pytest_path is None and deps.pytest_resolver is not None:
        pytest_path = deps.pytest_resolver()
    if pytest_path is None:
        _fail(
            "missing_pytest_identity",
            "Phase 0 requires an explicitly trusted pytest executable binding.",
        )
    pytest_executable = capture_pytest_executable_binding(
        pytest_path,
        workspace_root=root,
    )
    if (
        deps.current_provider_binding is not None
        and deps.current_provider_binding() != request.provider_binding
    ):
        _fail("provider_binding_changed", "The selected provider binding changed.")
    task_id = str(task.get("id") or "").strip()
    title = str(task.get("title") or "").strip()
    description = str(task.get("description") or "").strip()
    task_kind = str(task.get("task_kind") or "unknown").strip()
    if task_kind not in {
        "analysis_only",
        "implementation",
        "test_only",
        "verification_only",
        "documentation_only",
    }:
        _fail("unsupported_task_kind", "The native task kind is not losslessly representable.")
    if task_kind != "implementation":
        _fail("unsupported_task_kind", "The first Host slice admits one implementation task.")
    if task.get("analysis_only") not in (None, False):
        _fail("task_kind_conflict", "Native task flags conflict with implementation authority.")
    acceptance_raw = task.get("acceptance_criteria")
    if not isinstance(acceptance_raw, list) or not acceptance_raw:
        _fail("missing_acceptance", "The native task requires acceptance criteria.")
    acceptance_text = [str(item).strip() for item in acceptance_raw]
    if any(not item for item in acceptance_text) or len(set(acceptance_text)) != len(
        acceptance_text
    ):
        _fail("invalid_acceptance", "Acceptance criteria must be non-empty and unique.")
    dependencies_raw = task.get("dependencies") or []
    if dependencies_raw != []:
        _fail("unsupported_dependencies", "A one-task Phase-0 plan cannot have dependencies.")

    generated_branch = generate_task_branch_name(task_id, title)
    authored_branch = str(task.get("branch") or "").strip()
    if authored_branch and authored_branch != generated_branch:
        _fail("nondeterministic_task_branch", "The native task branch is not deterministic.")
    _assert_branch_available(runner, root, generated_branch, comparison=comparison)

    estimated_raw = task.get("estimated_files")
    writes_raw = task.get("write_scope")
    if not isinstance(estimated_raw, list) or not isinstance(writes_raw, list) or not writes_raw:
        _fail("invalid_task_scopes", "The native task requires explicit estimated/write scopes.")
    estimated = [
        _scope_from_native(
            item,
            root=root,
            comparison=comparison,
            declared_scope_kinds=request.declared_scope_kinds,
        )
        for item in estimated_raw
    ]
    writes = [
        _scope_from_native(
            item,
            root=root,
            comparison=comparison,
            declared_scope_kinds=request.declared_scope_kinds,
        )
        for item in writes_raw
    ]

    constraints_raw = plan.get("planning_constraints")
    if not isinstance(constraints_raw, Mapping) or set(constraints_raw) != {
        "schema_version",
        "target_roots",
        "forbidden_roots",
        "decoy_roots",
        "unrelated_roots",
    }:
        _fail("invalid_constraints", "The native plan requires canonical planning constraints.")
    if constraints_raw.get("schema_version") != 1:
        _fail("invalid_constraints", "The native planning constraint version is unsupported.")
    native_constraint_values: dict[str, Any] = {"schema_version": 1}
    host_constraint_values: dict[str, Any] = {"schema_version": 1}
    for key, kind in (
        ("target_roots", "target_root"),
        ("forbidden_roots", "forbidden_root"),
        ("decoy_roots", "decoy_root"),
        ("unrelated_roots", "unrelated_root"),
    ):
        native_items, host_items = _constraint_collection(
            constraints_raw.get(key),
            expected_kind=kind,
            root=root,
            comparison=comparison,
            declared_scope_kinds=request.declared_scope_kinds,
        )
        native_constraint_values[key] = native_items
        host_constraint_values[key] = host_items
    native_constraints = NativePlanningConstraints.model_validate(
        native_constraint_values, strict=True
    )
    host_constraints = PlanningConstraints.model_validate(host_constraint_values, strict=True)

    project_goal = str(plan.get("project_goal") or "").strip()
    summary = str(plan.get("summary") or "").strip()
    if not project_goal or not summary or not task_id or not title or not description:
        _fail("incomplete_native_plan", "The native plan lacks required authored text.")
    requirements_raw = plan.get("requirements")
    if not isinstance(requirements_raw, list):
        _fail("invalid_requirements", "Native requirements must be an array.")
    if any(not isinstance(item, str) for item in requirements_raw):
        _fail(
            "lossy_requirements",
            "Structured native requirement metadata is not representable in Host v2.",
        )
    requirements = [requirement_text(item) for item in requirements_raw]
    if any(not item for item in requirements) or len(set(requirements)) != len(requirements):
        _fail("invalid_requirements", "Native requirements must be lossless unique text.")

    affected: list[WorkspaceScope] = []
    for constraint in host_constraints.target_roots:
        affected.append(WorkspaceScope(path=constraint.path, kind="directory"))
    affected.extend(writes)
    seen_scopes: set[tuple[str, str]] = set()
    affected = [
        scope
        for scope in affected
        if not (
            (scope.path, scope.kind) in seen_scopes or seen_scopes.add((scope.path, scope.kind))
        )
    ]
    intent_kind = _intent_kind(request.planner_input, request.intent_kind)
    intent_summary = _bounded_intent_summary(request.planner_input)
    intent = PlanIntent(kind=intent_kind, summary=intent_summary, affected_scopes=affected)
    native_intent = NativeIntentRecord(
        kind=intent_kind,
        summary=intent_summary,
        affected_scopes=affected,
    )
    acceptance = [
        AcceptanceCriterion(
            criterion_id=f"{task_id}:criterion:{index}",
            description=text,
        )
        for index, text in enumerate(acceptance_text, start=1)
    ]
    host_task = PlanTask(
        task_id=task_id,
        order=1,
        task_kind=task_kind,
        branch=generated_branch,
        dependencies=[],
        title=title,
        description=description,
        acceptance=acceptance,
        estimated_scopes=estimated,
        write_scopes=writes,
    )
    native_task = NativePlanTask(
        id=task_id,
        task_kind=task_kind,
        branch=generated_branch,
        dependencies=[],
        title=title,
        description=description,
        acceptance_criteria=acceptance_text,
        estimated_files=estimated,
        write_scope=writes,
    )
    host_definition = PlanDefinition(
        schema="alysis.forge-host.plan/v2",
        project_goal=project_goal,
        summary=summary,
        requirements=requirements,
        planning_constraints=host_constraints,
        asset_refs=[],
        intent=intent,
        tasks=[host_task],
    )
    native_definition = NativeApprovalDefinition(
        schema="alysis.forge-native-approval/v2",
        project_goal=project_goal,
        summary=summary,
        requirements=requirements,
        planning_constraints=native_constraints,
        assets=[],
        intent_record=native_intent,
        tasks=[native_task],
    )
    host_fingerprint = plan_fingerprint(host_definition)
    native_fingerprint = canonical_fingerprint(native_definition)
    verification_commands = request.verification_commands
    # Explicit host selection is authority captured before approval. A scan is
    # only a suggestion and must never acquire authority from the repo_native
    # compatibility label or the canonical spelling of a test runner.
    command_source = "forge_host.authoritative_verification_commands"
    if verification_commands is None:
        command_source = "repo_scan.likely_test_commands"
        verification_commands = scan_workspace(
            context=resolve_workspace_context(root)
        ).likely_test_commands
    contract, specs = _build_verification_contract(
        task_id=task_id,
        acceptance=acceptance,
        writes=writes,
        workspace_ref=request.workspace_ref,
        host_fingerprint=host_fingerprint,
        native_fingerprint=native_fingerprint,
        commands=verification_commands,
        command_source=command_source,
    )
    path_policy = WorkspacePathPolicy(
        comparison=comparison,
        unicode_normalization="NFC",
        symlink_policy="resolve_beneath_workspace",
    )
    admission_payload: dict[str, Any] = {
        "schema": "alysis.forge-host.execution-admission/v2",
        "workspaceRef": request.workspace_ref,
        "providerBindingRef": request.provider_binding.provider_binding_ref,
        "workspacePathPolicy": path_policy,
        "executionStrategy": SequentialExecutionStrategy(
            mode="sequential",
            max_parallel_workers=1,
            integration_policy="fail_fast",
        ),
        "perManagedTaskMaxSteps": request.per_managed_task_max_steps,
        "runTimeoutMs": request.run_timeout_ms,
        "fingerprint": "0" * 64,
    }
    admission_payload["fingerprint"] = execution_admission_fingerprint(admission_payload)
    execution_admission = ExecutionAdmission.model_validate(admission_payload, strict=True)
    resources = [
        FilesystemApprovalResource(access="write", kind=scope.kind, path=scope.path)
        for scope in writes
    ]
    scope_payload: dict[str, Any] = {
        "schema": "alysis.forge-host.approval-scope/v2",
        "kind": "filesystem",
        "label": PHASE0_APPROVAL_SCOPE_LABEL,
        "workspaceRef": request.workspace_ref,
        "resources": resources,
        "fingerprint": "0" * 64,
    }
    scope_payload["fingerprint"] = approval_scope_fingerprint(scope_payload)
    approval_scope = FilesystemApprovalScope.model_validate(scope_payload, strict=True)
    policy_payload: dict[str, Any] = {
        "required": True,
        "scope": approval_scope,
        "risk": "high",
        "summary": PHASE0_APPROVAL_SUMMARY,
        "fingerprint": "0" * 64,
    }
    policy_payload["fingerprint"] = approval_policy_fingerprint(policy_payload)
    approval_policy = PlanApprovalPolicy.model_validate(policy_payload, strict=True)
    plan_ready = PlanReady(
        type="plan.ready",
        plan_definition=host_definition,
        host_plan_fingerprint=host_fingerprint,
        native_approval_definition=native_definition,
        native_approval_fingerprint=native_fingerprint,
        verification_contract=contract,
        execution_admission=execution_admission,
        approval_policy=approval_policy,
    )

    # Branch names are authored plan data, not Git refs. Persist only after the
    # complete immutable projection and every non-mutating preflight has passed.
    task["branch"] = generated_branch
    deps.plan_saver(request.paths, plan)
    snapshot = copy.deepcopy(plan)
    snapshot_bytes = canonical_json_bytes(snapshot, exclude_computed_fingerprints=False)
    return NativePlanAdmission(
        paths=request.paths,
        plan_ready=plan_ready,
        native_plan_snapshot=MappingProxyType(snapshot),
        native_plan_snapshot_digest=hashlib.sha256(snapshot_bytes).hexdigest(),
        native_run_id=request.paths.run_id,
        workspace_git=workspace_git,
        provider_binding=request.provider_binding,
        pytest_executable=pytest_executable,
        verification_specs=specs,
        diagnostics=(
            NativeTaskDiagnostic(
                task_id=task_id,
                task_kind_reason=str(task.get("task_kind_reason") or "").strip(),
            ),
        ),
    )


def revalidate_native_plan_admission(
    admission: NativePlanAdmission,
    *,
    dependencies: NativeAdapterDependencies | None = None,
) -> dict[str, Any]:
    """Reload native authority and verify workspace/Git/provider bindings.

    Call this while the sequential engine's mutation guard is held.  The
    returned object is a fresh private plan instance suitable for execution.
    """

    deps = dependencies or NativeAdapterDependencies()
    runner = deps.git_runner or _default_git_runner
    try:
        loaded = json.loads(admission.paths.plan_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForgeHostPreflightError(
            "native_plan_reload_failed", "The admitted native plan cannot be reloaded."
        ) from exc
    if not isinstance(loaded, dict):
        _fail("native_plan_reload_failed", "The admitted native plan is no longer an object.")
    digest = hashlib.sha256(
        canonical_json_bytes(loaded, exclude_computed_fingerprints=False)
    ).hexdigest()
    if digest != admission.native_plan_snapshot_digest:
        _fail("native_plan_changed", "The native plan changed after approval.")
    root = admission.workspace_git.workspace_root
    root_stat = os.lstat(root)
    if (root_stat.st_dev, root_stat.st_ino) != (
        admission.workspace_git.root_device,
        admission.workspace_git.root_inode,
    ):
        _fail("workspace_identity_changed", "The workspace root identity changed.")
    current = inspect_workspace_git_binding(
        paths=admission.paths,
        workspace_ref=admission.workspace_git.workspace_ref,
        runner=runner,
    )
    if (
        current.git_root != admission.workspace_git.git_root
        or current.base_branch != admission.workspace_git.base_branch
        or current.base_head != admission.workspace_git.base_head
        or current.path_comparison != admission.workspace_git.path_comparison
    ):
        _fail("git_binding_changed", "The selected base branch or HEAD changed after approval.")
    loaded_tasks = loaded.get("tasks")
    if (
        not isinstance(loaded_tasks, list)
        or len(loaded_tasks) != 1
        or not isinstance(loaded_tasks[0], Mapping)
    ):
        _fail("native_plan_changed", "The admitted native task projection changed.")
    admitted_branch = str(loaded_tasks[0].get("branch") or "").strip()
    if not admitted_branch:
        _fail("native_plan_changed", "The admitted native task branch is missing.")
    _assert_branch_available(
        runner,
        root,
        admitted_branch,
        comparison=admission.workspace_git.path_comparison,
    )
    revalidate_pytest_executable_binding(
        admission.pytest_executable,
        workspace_root=root,
    )
    if deps.current_provider_binding is not None:
        current_provider = deps.current_provider_binding()
        if current_provider != admission.provider_binding:
            _fail("provider_binding_changed", "The provider binding changed after approval.")
    return copy.deepcopy(loaded)


def native_plan_projection_bytes(admission: NativePlanAdmission) -> bytes:
    """Return a full, immutable JSON snapshot for an injected sequential runner."""

    return canonical_json_bytes(
        admission.native_plan_snapshot,
        exclude_computed_fingerprints=False,
    )


def private_native_plan_admission(admission: NativePlanAdmission) -> NativePlanAdmission:
    """Canonicalize a deep private copy across an untrusted adapter boundary."""

    if not isinstance(admission, NativePlanAdmission):
        raise TypeError("native admission has the wrong concrete type")
    plan_ready = PlanReady.model_validate_json(
        admission.plan_ready.model_dump_json(by_alias=True),
        strict=True,
    )
    snapshot = copy.deepcopy(dict(admission.native_plan_snapshot))
    digest = hashlib.sha256(
        canonical_json_bytes(snapshot, exclude_computed_fingerprints=False)
    ).hexdigest()
    if digest != admission.native_plan_snapshot_digest:
        _fail("native_snapshot_mismatch", "The native plan snapshot binding is inconsistent.")
    if str(snapshot.get("run_id") or "") != admission.native_run_id:
        _fail("native_run_mismatch", "The private native run binding is inconsistent.")
    workspace = WorkspaceGitBinding(
        workspace_ref=admission.workspace_git.workspace_ref,
        workspace_root=Path(admission.workspace_git.workspace_root),
        root_device=admission.workspace_git.root_device,
        root_inode=admission.workspace_git.root_inode,
        git_root=Path(admission.workspace_git.git_root),
        base_branch=admission.workspace_git.base_branch,
        base_head=admission.workspace_git.base_head,
        path_comparison=admission.workspace_git.path_comparison,
    )
    provider = ProviderBinding(
        provider_binding_ref=admission.provider_binding.provider_binding_ref,
        provider=admission.provider_binding.provider,
        model=admission.provider_binding.model,
        account_fingerprint=admission.provider_binding.account_fingerprint,
    )
    pytest_executable = PytestExecutableBinding(
        resolved_path=Path(admission.pytest_executable.resolved_path),
        device=admission.pytest_executable.device,
        inode=admission.pytest_executable.inode,
        size=admission.pytest_executable.size,
        mtime_ns=admission.pytest_executable.mtime_ns,
        sha256=admission.pytest_executable.sha256,
    )
    specs = tuple(copy.deepcopy(item) for item in admission.verification_specs)
    diagnostics = tuple(
        NativeTaskDiagnostic(
            task_id=item.task_id,
            task_kind_reason=item.task_kind_reason,
        )
        for item in admission.diagnostics
    )
    return NativePlanAdmission(
        paths=admission.paths,
        plan_ready=plan_ready,
        native_plan_snapshot=MappingProxyType(snapshot),
        native_plan_snapshot_digest=digest,
        native_run_id=admission.native_run_id,
        workspace_git=workspace,
        provider_binding=provider,
        pytest_executable=pytest_executable,
        verification_specs=specs,
        diagnostics=diagnostics,
    )


__all__ = [
    "ForgeHostPreflightError",
    "IntentKind",
    "NativeAdapterDependencies",
    "NativeAdapterRequest",
    "NativePlanAdmission",
    "NativeTaskDiagnostic",
    "ProviderBinding",
    "PytestExecutableBinding",
    "ScopeKind",
    "WorkspaceGitBinding",
    "build_native_plan_admission",
    "detect_filesystem_path_comparison",
    "capture_pytest_executable_binding",
    "inspect_workspace_git_binding",
    "native_plan_projection_bytes",
    "private_native_plan_admission",
    "revalidate_pytest_executable_binding",
    "revalidate_native_plan_admission",
]
