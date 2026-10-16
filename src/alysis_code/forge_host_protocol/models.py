"""Strict typed models for Forge Host Protocol v2.

The JSON Schema defines the declarative wire language. These models add the
same field constraints plus object-local semantic checks. Stream history,
approval consumption, and cross-event resolution belong to :mod:`ledger`.
"""

from __future__ import annotations

import shlex
import unicodedata
from datetime import datetime, timedelta
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from alysis_code.verification_command_analysis import analyze_verification_command

from .canonical import (
    JS_SAFE_INTEGER_MAX,
    approval_action_fingerprint,
    approval_policy_fingerprint,
    approval_scope_fingerprint,
    execution_admission_fingerprint,
    plan_fingerprint,
    verification_contract_fingerprint,
)
from .redaction import validate_redacted_runtime_text

PROTOCOL_NAME = "alysis.forge-host"
PROTOCOL_VERSION = "2.0"
PLAN_SCHEMA = "alysis.forge-host.plan/v2"
VERIFICATION_CONTRACT_SCHEMA = "alysis.forge.verification-contract/v2"
VERIFICATION_CONTRACT_VERSION = 2
APPROVAL_SCOPE_SCHEMA = "alysis.forge-host.approval-scope/v2"
APPROVAL_ACTION_SCHEMA = "alysis.forge-host.approval-action/v2"
EXECUTION_ADMISSION_SCHEMA = "alysis.forge-host.execution-admission/v2"
PHASE0_APPROVAL_SCOPE_LABEL = "Forge plan execution authority"
PHASE0_APPROVAL_SUMMARY = (
    "Run the bound Forge plan, provider, strategy, writes, and verification commands."
)

MessageId = Annotated[str, Field(pattern=r"^msg_[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")]
RequestId = Annotated[str, Field(pattern=r"^req_[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")]
IdempotencyKey = Annotated[str, Field(pattern=r"^idem_[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")]
OpaqueId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*_[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")]
StableId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$")]
Fingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(ge=0, le=JS_SAFE_INTEGER_MAX)]
PositiveSequence = Annotated[int, Field(ge=1, le=JS_SAFE_INTEGER_MAX)]
ProtocolVersion = Literal["2.0"]
NonEmptyText = Annotated[str, Field(min_length=1, max_length=4096)]
SafeCount = Annotated[int, Field(ge=0, le=JS_SAFE_INTEGER_MAX)]


def _validate_safe_diagnostic(value: str) -> str:
    return validate_redacted_runtime_text(value)


def _validate_optional_safe_diagnostic(value: str) -> str:
    return value if not value else validate_redacted_runtime_text(value)


SafeDiagnosticText = Annotated[
    str,
    Field(min_length=1, max_length=2000),
    AfterValidator(_validate_safe_diagnostic),
]
OptionalSafeDiagnosticText = Annotated[
    str,
    Field(max_length=500),
    AfterValidator(_validate_optional_safe_diagnostic),
]
SafeRuntimeSummary = Annotated[
    str,
    Field(min_length=1, max_length=1000),
    AfterValidator(_validate_safe_diagnostic),
]
SafeAuthorityLabel = Annotated[
    str,
    Field(min_length=1, max_length=500),
    AfterValidator(_validate_safe_diagnostic),
]
SafeAuthoritySummary = Annotated[
    str,
    Field(min_length=1, max_length=1000),
    AfterValidator(_validate_safe_diagnostic),
]


def _validate_workspace_relative_path(value: str) -> str:
    if value != value.strip():
        raise ValueError("workspace-relative path cannot have surrounding whitespace")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("workspace-relative path must use NFC normalization")
    if value.startswith(("/", "~", "\\")) or "\\" in value or ":" in value:
        raise ValueError("workspace-relative path cannot be absolute or URI-like")
    if any(character in value for character in "*?[]"):
        raise ValueError("workspace-relative path cannot contain glob syntax")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("workspace-relative path cannot contain control characters")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("workspace-relative path must be normalized and traversal-free")
    return value


WorkspaceRelativePath = Annotated[
    str,
    Field(min_length=1, max_length=1024),
    AfterValidator(_validate_workspace_relative_path),
]


def _validate_workspace_directory(value: str) -> str:
    return value if value == "." else _validate_workspace_relative_path(value)


WorkspaceDirectory = Annotated[
    str,
    Field(min_length=1, max_length=1024),
    AfterValidator(_validate_workspace_directory),
]


def _validate_workspace_scope_path(value: str) -> str:
    if value.endswith("/**"):
        prefix = value[:-3]
        _validate_workspace_relative_path(prefix)
        return value
    return _validate_workspace_relative_path(value)


WorkspaceScopePath = Annotated[
    str,
    Field(min_length=1, max_length=1024),
    AfterValidator(_validate_workspace_scope_path),
]


def _validate_git_branch(value: str) -> str:
    """Validate the portable authored subset of ``git check-ref-format --branch``."""

    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("task branch must use NFC normalization")
    if value in {"@", "HEAD"} or value.startswith(("-", "/")) or value.endswith(("/", ".")):
        raise ValueError("task branch is not a valid Git branch name")
    if "//" in value or ".." in value or "@{" in value:
        raise ValueError("task branch is not a valid Git branch name")
    if any(
        ord(character) < 32 or ord(character) == 127 or character in " ~^:?*[\\"
        for character in value
    ):
        raise ValueError("task branch is not a valid Git branch name")
    for component in value.split("/"):
        if not component or component.startswith(".") or component.casefold().endswith(".lock"):
            raise ValueError("task branch is not a portable Git branch name")
    return value


GitBranchName = Annotated[
    str,
    Field(min_length=1, max_length=200),
    AfterValidator(_validate_git_branch),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class NativeStrictModel(BaseModel):
    """Closed snake-case native approval projection for the Phase-0 bridge."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Identity(StrictModel):
    session_id: Annotated[str, Field(pattern=r"^ses_[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")]
    generation_id: Annotated[str, Field(pattern=r"^gen_[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")]
    run_id: Annotated[str, Field(pattern=r"^run_[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")] | None
    revision: Revision

    @property
    def stable_key(self) -> tuple[str, str, str | None]:
        return (self.session_id, self.generation_id, self.run_id)


HostKind = Literal["engine", "desktop", "cli", "vscode", "test"]


class Peer(StrictModel):
    name: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9._-]{1,63}$")]
    version: Annotated[
        str,
        Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$"),
    ]
    host_kind: HostKind


class Capability(StrictModel):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_-]*)+$")]
    version: Annotated[int, Field(ge=1, le=JS_SAFE_INTEGER_MAX)]


def _require_unique(values: list[str], description: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{description} must be unique")


def _require_unique_capabilities(capabilities: list[Capability]) -> list[Capability]:
    _require_unique([capability.name for capability in capabilities], "capability names")
    return capabilities


class ReplayCursor(StrictModel):
    operation_id: OpaqueId
    identity: Identity
    after_sequence: Annotated[int, Field(ge=0, le=JS_SAFE_INTEGER_MAX)]


class ReplayWindow(StrictModel):
    operation_id: OpaqueId
    after_sequence: Annotated[int, Field(ge=0, le=JS_SAFE_INTEGER_MAX)]
    through_sequence: Annotated[int, Field(ge=0, le=JS_SAFE_INTEGER_MAX)]

    @model_validator(mode="after")
    def window_is_ordered(self) -> ReplayWindow:
        if self.through_sequence < self.after_sequence:
            raise ValueError("throughSequence must not precede afterSequence")
        return self


class HandshakeRequest(StrictModel):
    type: Literal["handshake.request"]
    client: Peer
    supported_versions: Annotated[list[str], Field(min_length=1, max_length=16)]
    capabilities: Annotated[list[Capability], Field(max_length=256)]
    replay_cursor: ReplayCursor | None

    @field_validator("supported_versions")
    @classmethod
    def supported_versions_are_unique(cls, versions: list[str]) -> list[str]:
        _require_unique(versions, "supported protocol versions")
        for version in versions:
            parts = version.split(".")
            if len(parts) != 2 or not all(part.isdecimal() for part in parts):
                raise ValueError("supported protocol versions must use MAJOR.MINOR")
        return versions

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_unique(cls, value: list[Capability]) -> list[Capability]:
        return _require_unique_capabilities(value)


class HandshakeResponse(StrictModel):
    type: Literal["handshake.response"]
    server: Peer
    selected_version: ProtocolVersion
    capabilities: Annotated[list[Capability], Field(max_length=256)]
    identity: Identity
    replay_window: ReplayWindow | None

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_unique(cls, value: list[Capability]) -> list[Capability]:
        return _require_unique_capabilities(value)

    @model_validator(mode="after")
    def handshake_phase_matches_replay(self) -> HandshakeResponse:
        if self.replay_window is None and self.identity.run_id is not None:
            raise ValueError("initial handshake identity must not contain runId")
        return self


class PlannerGoal(StrictModel):
    kind: Literal["goal"]
    text: NonEmptyText


class PlannerAnswer(StrictModel):
    kind: Literal["answer"]
    question_id: OpaqueId
    text: NonEmptyText


class PlannerRevision(StrictModel):
    kind: Literal["revision"]
    text: NonEmptyText


PlannerValue: TypeAlias = Annotated[
    PlannerGoal | PlannerAnswer | PlannerRevision,
    Field(discriminator="kind"),
]


class RevisionedCommand(StrictModel):
    identity: Identity
    expected_revision: Revision

    @model_validator(mode="after")
    def expected_revision_matches_identity(self) -> RevisionedCommand:
        if self.expected_revision != self.identity.revision:
            raise ValueError("expectedRevision must equal identity.revision")
        return self


class PlannerInput(RevisionedCommand):
    type: Literal["planner.input"]
    input: PlannerValue

    @model_validator(mode="after")
    def planner_is_pre_run(self) -> PlannerInput:
        if self.identity.run_id is not None:
            raise ValueError("planner.input must not contain runId")
        return self


ApprovalDecisionValue = Literal["allow_once", "deny"]


class ApprovalDecision(RevisionedCommand):
    type: Literal["approval.decision"]
    approval_operation_id: OpaqueId
    approval_id: OpaqueId
    action_fingerprint: Fingerprint
    scope_fingerprint: Fingerprint
    expires_at: AwareDatetime
    decision: ApprovalDecisionValue

    @model_validator(mode="after")
    def expiry_is_canonical(self) -> ApprovalDecision:
        _require_canonical_expiry(self.expires_at)
        return self


class SequentialExecutionStrategy(StrictModel):
    mode: Literal["sequential"]
    max_parallel_workers: Literal[1]
    integration_policy: Literal["fail_fast"]


class ParallelExecutionStrategy(StrictModel):
    mode: Literal["parallel"]
    max_parallel_workers: Annotated[int, Field(ge=2, le=16)]
    integration_policy: Literal["fail_fast", "collect_then_integrate"]


ExecutionStrategy: TypeAlias = Annotated[
    SequentialExecutionStrategy | ParallelExecutionStrategy,
    Field(discriminator="mode"),
]


class WorkspacePathPolicy(StrictModel):
    """Filesystem comparison promise bound to the opaque workspace identity."""

    comparison: Literal["case_sensitive", "case_insensitive"]
    unicode_normalization: Literal["NFC"]
    symlink_policy: Literal["resolve_beneath_workspace"]


class ExecutionAdmission(StrictModel):
    """Immutable engine-owned authority for one admitted Forge execution."""

    schema_id: Literal["alysis.forge-host.execution-admission/v2"] = Field(alias="schema")
    workspace_ref: OpaqueId
    provider_binding_ref: OpaqueId
    workspace_path_policy: WorkspacePathPolicy
    execution_strategy: ExecutionStrategy
    per_managed_task_max_steps: Annotated[int, Field(ge=1, le=10_000)]
    run_timeout_ms: Annotated[int, Field(ge=1_000, le=86_400_000)]
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def fingerprint_matches_admission(self) -> ExecutionAdmission:
        if execution_admission_fingerprint(self) != self.fingerprint:
            raise ValueError("execution admission fingerprint does not match immutable settings")
        return self


class RunStart(RevisionedCommand):
    type: Literal["run.start"]
    host_plan_fingerprint: Fingerprint
    native_approval_fingerprint: Fingerprint
    verification_contract_fingerprint: Fingerprint
    execution_admission: ExecutionAdmission

    @model_validator(mode="after")
    def start_allocates_the_run(self) -> RunStart:
        if self.identity.run_id is not None:
            raise ValueError("run.start must use the pre-run identity")
        return self


class RunCancel(RevisionedCommand):
    type: Literal["run.cancel"]
    reason_code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")]
    message: SafeDiagnosticText | None = None

    @model_validator(mode="after")
    def cancel_requires_a_run(self) -> RunCancel:
        if self.identity.run_id is None:
            raise ValueError("run.cancel requires runId")
        return self


class CommandAccepted(StrictModel):
    type: Literal["command.accepted"]
    identity: Identity
    duplicate: bool


ProtocolErrorCode = Literal[
    "invalid_message",
    "unsupported_version",
    "capability_unavailable",
    "stale_identity",
    "stale_revision",
    "idempotency_conflict",
    "invalid_transition",
    "internal_error",
]

_PROTOCOL_ERROR_SUMMARIES = {
    "invalid_message": "The message is invalid.",
    "unsupported_version": "The protocol version is unsupported.",
    "capability_unavailable": "The requested capability is unavailable.",
    "stale_identity": "The message identity is stale.",
    "stale_revision": "The message revision is stale.",
    "idempotency_conflict": "The idempotency authority conflicts.",
    "invalid_transition": "The protocol transition is invalid.",
    "internal_error": "The protocol operation failed.",
}


class ProtocolError(StrictModel):
    type: Literal["protocol.error"]
    code: ProtocolErrorCode
    message: Annotated[str, Field(min_length=1, max_length=1000)]
    retryable: bool
    current_identity: Identity | None = None

    @model_validator(mode="after")
    def message_is_stable_and_non_reflective(self) -> ProtocolError:
        if self.message != _PROTOCOL_ERROR_SUMMARIES[self.code]:
            raise ValueError("protocol error message must use the stable summary for its code")
        return self


class WorkspaceScope(StrictModel):
    path: WorkspaceScopePath
    kind: Literal["file", "directory"]

    @model_validator(mode="after")
    def glob_is_directory_scope(self) -> WorkspaceScope:
        if self.path.endswith("/**") and self.kind != "directory":
            raise ValueError("recursive scope patterns must have directory kind")
        return self


def _comparison_path(path: str, *, case_sensitive: bool) -> str:
    normalized = path.removesuffix("/**")
    return normalized if case_sensitive else normalized.casefold()


def _scope_contains(
    container: WorkspaceScope,
    candidate: WorkspaceScope,
    *,
    case_sensitive: bool = True,
) -> bool:
    container_path = _comparison_path(container.path, case_sensitive=case_sensitive)
    candidate_path = _comparison_path(candidate.path, case_sensitive=case_sensitive)
    if container.kind == "file":
        return candidate.kind == "file" and candidate_path == container_path
    if candidate_path == container_path:
        return candidate.kind == "directory"
    return candidate_path.startswith(f"{container_path}/")


def _scopes_overlap(
    left: WorkspaceScope,
    right: WorkspaceScope,
    *,
    case_sensitive: bool = True,
) -> bool:
    if _comparison_path(left.path, case_sensitive=case_sensitive) == _comparison_path(
        right.path, case_sensitive=case_sensitive
    ):
        # A file/directory substitution at the same lexical path must fail closed
        # for forbidden and approval authority even though it is not containment.
        return True
    return _scope_contains(left, right, case_sensitive=case_sensitive) or _scope_contains(
        right, left, case_sensitive=case_sensitive
    )


def _require_unique_comparison_paths(
    paths: list[str],
    description: str,
    *,
    case_sensitive: bool,
) -> None:
    identities = [_comparison_path(path, case_sensitive=case_sensitive) for path in paths]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{description} contain aliases under the workspace path policy")


class AcceptanceCriterion(StrictModel):
    criterion_id: StableId
    description: Annotated[str, Field(min_length=1, max_length=1000)]


class PlanTask(StrictModel):
    task_id: StableId
    order: Annotated[int, Field(ge=1, le=10_000)]
    task_kind: Literal[
        "analysis_only",
        "implementation",
        "test_only",
        "verification_only",
        "documentation_only",
        "unknown",
    ]
    branch: GitBranchName
    dependencies: Annotated[list[StableId], Field(max_length=256)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    acceptance: Annotated[list[AcceptanceCriterion], Field(min_length=1, max_length=64)]
    estimated_scopes: Annotated[list[WorkspaceScope], Field(max_length=64)]
    write_scopes: Annotated[list[WorkspaceScope], Field(max_length=64)]

    @model_validator(mode="after")
    def task_collections_are_unique(self) -> PlanTask:
        _require_unique(self.dependencies, "task dependencies")
        _require_unique(
            [criterion.criterion_id for criterion in self.acceptance],
            "acceptance criterion IDs within a task",
        )
        estimated = [(scope.path, scope.kind) for scope in self.estimated_scopes]
        writes = [(scope.path, scope.kind) for scope in self.write_scopes]
        if len(estimated) != len(set(estimated)) or len(writes) != len(set(writes)):
            raise ValueError("workspace scopes must be unique")
        return self


class PlanIntent(StrictModel):
    kind: Literal["build", "fix", "refactor", "audit", "test", "document"]
    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    affected_scopes: Annotated[list[WorkspaceScope], Field(max_length=256)]

    @model_validator(mode="after")
    def affected_scopes_are_unique(self) -> PlanIntent:
        keys = [(scope.path, scope.kind) for scope in self.affected_scopes]
        if len(keys) != len(set(keys)):
            raise ValueError("intent affectedScopes must be unique")
        return self


PlanningConstraintKind = Literal["target_root", "forbidden_root", "decoy_root", "unrelated_root"]


class PlanningPathConstraint(StrictModel):
    path: WorkspaceScopePath
    kind: PlanningConstraintKind
    reason_code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")]
    evidence: Annotated[str, Field(max_length=2000)]


class PlanningConstraints(StrictModel):
    """Lossless public projection of the CLI's canonical planning constraints."""

    schema_version: Literal[1]
    target_roots: Annotated[list[PlanningPathConstraint], Field(max_length=256)]
    forbidden_roots: Annotated[list[PlanningPathConstraint], Field(max_length=256)]
    decoy_roots: Annotated[list[PlanningPathConstraint], Field(max_length=256)]
    unrelated_roots: Annotated[list[PlanningPathConstraint], Field(max_length=256)]

    @model_validator(mode="after")
    def collections_are_typed_and_unique(self) -> PlanningConstraints:
        collections = (
            (self.target_roots, "target_root"),
            (self.forbidden_roots, "forbidden_root"),
            (self.decoy_roots, "decoy_root"),
            (self.unrelated_roots, "unrelated_root"),
        )
        identities: list[tuple[str, str]] = []
        for records, expected_kind in collections:
            if any(record.kind != expected_kind for record in records):
                raise ValueError("planning constraint collection does not match record kind")
            identities.extend((record.kind, record.path) for record in records)
        if len(identities) != len(set(identities)):
            raise ValueError("planning constraints must be unique by kind and path")
        return self


class PlanDefinition(StrictModel):
    schema_id: Literal["alysis.forge-host.plan/v2"] = Field(alias="schema")
    project_goal: Annotated[str, Field(min_length=1, max_length=4096)]
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    requirements: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=1000)]], Field(max_length=256)
    ]
    planning_constraints: PlanningConstraints
    asset_refs: Annotated[list[OpaqueId], Field(max_length=256)]
    intent: PlanIntent
    tasks: Annotated[list[PlanTask], Field(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def plan_collections_are_unique(self) -> PlanDefinition:
        _require_unique(self.requirements, "plan requirements")
        _require_unique(self.asset_refs, "plan assetRefs")
        return self


class NativePlanningPathConstraint(NativeStrictModel):
    path: WorkspaceScopePath
    kind: PlanningConstraintKind
    reason_code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")]
    evidence: Annotated[str, Field(max_length=2000)]


class NativePlanningConstraints(NativeStrictModel):
    schema_version: Literal[1]
    target_roots: Annotated[list[NativePlanningPathConstraint], Field(max_length=256)]
    forbidden_roots: Annotated[list[NativePlanningPathConstraint], Field(max_length=256)]
    decoy_roots: Annotated[list[NativePlanningPathConstraint], Field(max_length=256)]
    unrelated_roots: Annotated[list[NativePlanningPathConstraint], Field(max_length=256)]

    @model_validator(mode="after")
    def collections_are_typed_and_unique(self) -> NativePlanningConstraints:
        collections = (
            (self.target_roots, "target_root"),
            (self.forbidden_roots, "forbidden_root"),
            (self.decoy_roots, "decoy_root"),
            (self.unrelated_roots, "unrelated_root"),
        )
        identities: list[tuple[str, str]] = []
        for records, expected_kind in collections:
            if any(record.kind != expected_kind for record in records):
                raise ValueError("native planning constraint collection has the wrong kind")
            identities.extend((record.kind, record.path) for record in records)
        if len(identities) != len(set(identities)):
            raise ValueError("native planning constraints must be unique")
        return self


class NativeIntentRecord(NativeStrictModel):
    kind: Literal["build", "fix", "refactor", "audit", "test", "document"]
    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    affected_scopes: Annotated[list[WorkspaceScope], Field(max_length=256)]


class NativePlanTask(NativeStrictModel):
    id: StableId
    task_kind: Literal[
        "analysis_only",
        "implementation",
        "test_only",
        "verification_only",
        "documentation_only",
        "unknown",
    ]
    branch: GitBranchName
    dependencies: Annotated[list[StableId], Field(max_length=256)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    acceptance_criteria: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=1000)]],
        Field(min_length=1, max_length=64),
    ]
    estimated_files: Annotated[list[WorkspaceScope], Field(max_length=64)]
    write_scope: Annotated[list[WorkspaceScope], Field(max_length=64)]

    @model_validator(mode="after")
    def collections_are_unique(self) -> NativePlanTask:
        _require_unique(self.dependencies, "native task dependencies")
        _require_unique(self.acceptance_criteria, "native task acceptance criteria")
        for scopes, name in (
            (self.estimated_files, "estimated_files"),
            (self.write_scope, "write_scope"),
        ):
            keys = [(scope.path, scope.kind) for scope in scopes]
            if len(keys) != len(set(keys)):
                raise ValueError(f"native task {name} entries must be unique")
        return self


class NativeApprovalDefinition(NativeStrictModel):
    """Closed, lossless native approval projection accepted by Host Protocol v2.

    This is a new migration profile, not a permissive serialization of legacy
    plans.  Unknown or runtime-only fields are rejected rather than silently
    omitted from approval authority.
    """

    schema_id: Literal["alysis.forge-native-approval/v2"] = Field(alias="schema")
    project_goal: Annotated[str, Field(min_length=1, max_length=4096)]
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    requirements: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=1000)]], Field(max_length=256)
    ]
    planning_constraints: NativePlanningConstraints
    assets: Annotated[list[OpaqueId], Field(max_length=256)]
    intent_record: NativeIntentRecord
    tasks: Annotated[list[NativePlanTask], Field(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def collections_are_unique(self) -> NativeApprovalDefinition:
        _require_unique(self.requirements, "native plan requirements")
        _require_unique(self.assets, "native plan assets")
        _require_unique([task.id for task in self.tasks], "native plan task IDs")
        return self


def _host_native_projection_equivalent(
    host: PlanDefinition, native: NativeApprovalDefinition
) -> bool:
    projected = PlanDefinition(
        schema=PLAN_SCHEMA,
        project_goal=native.project_goal,
        summary=native.summary,
        requirements=native.requirements,
        planning_constraints=native.planning_constraints.model_dump(
            mode="json", by_alias=False, exclude_none=False
        ),
        asset_refs=native.assets,
        intent={
            "kind": native.intent_record.kind,
            "summary": native.intent_record.summary,
            "affected_scopes": native.intent_record.affected_scopes,
        },
        tasks=[
            {
                "task_id": task.id,
                "order": order,
                "task_kind": task.task_kind,
                "branch": task.branch,
                "dependencies": task.dependencies,
                "title": task.title,
                "description": task.description,
                "acceptance": [
                    {
                        "criterion_id": f"{task.id}:criterion:{criterion_index}",
                        "description": description,
                    }
                    for criterion_index, description in enumerate(task.acceptance_criteria, start=1)
                ],
                "estimated_scopes": task.estimated_files,
                "write_scopes": task.write_scope,
            }
            for order, task in enumerate(native.tasks, start=1)
        ],
    )
    from .canonical import canonical_json_bytes

    return canonical_json_bytes(host) == canonical_json_bytes(projected)


VerificationProvenance = Literal[
    "HOST_AUTHORITATIVE",
    "EXPLICIT_USER_COMMAND",
    "PREEXISTING_REPO_NATIVE",
    "PREEXISTING_TASK_CHECKER",
    "INFERRED_HEURISTIC",
]
VerificationTrustLevel = Literal["TRUSTED", "UNTRUSTED"]
VerificationRequirementLevel = Literal["REQUIRED", "ADVISORY"]


class VerificationRequirement(StrictModel):
    requirement_id: StableId
    task_id: StableId | None
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    provenance: VerificationProvenance
    trust_level: VerificationTrustLevel
    requirement: VerificationRequirementLevel


class VerificationScope(StrictModel):
    scope_id: StableId
    phase: Literal["task", "integration"]
    task_id: StableId | None
    cwd: WorkspaceDirectory
    referenced_paths: Annotated[list[WorkspaceRelativePath], Field(max_length=256)]
    allowed_outputs: Annotated[list[WorkspaceRelativePath], Field(max_length=256)]
    declared_inputs: Annotated[list[WorkspaceRelativePath], Field(max_length=256)]

    @model_validator(mode="after")
    def scope_is_coherent(self) -> VerificationScope:
        if self.phase == "task" and self.task_id is None:
            raise ValueError("task verification scope requires taskId")
        if self.phase == "integration" and self.task_id is not None:
            raise ValueError("integration verification scope must not contain taskId")
        for values, description in (
            (self.referenced_paths, "referenced paths"),
            (self.allowed_outputs, "allowed outputs"),
            (self.declared_inputs, "declared inputs"),
        ):
            _require_unique(values, description)
        if not set(self.referenced_paths).issubset(
            set(self.allowed_outputs) | set(self.declared_inputs)
        ):
            raise ValueError(
                "referencedPaths must be contained by allowedOutputs or declaredInputs"
            )
        return self


class VerificationCommand(StrictModel):
    command_id: StableId
    task_id: StableId | None
    scope_id: StableId
    original_text: Annotated[str, Field(min_length=1, max_length=4096)]
    display_text: Annotated[str, Field(min_length=1, max_length=4096)]
    execution_mode: Literal["ARGV", "TRUSTED_SHELL_EXPRESSION", "INTERPRETER_SNIPPET", "INVALID"]
    provenance: VerificationProvenance
    trust_level: VerificationTrustLevel
    requirement: VerificationRequirementLevel
    timeout_policy: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")]
    requirement_ids: Annotated[list[StableId], Field(max_length=64)]
    validation_status: Literal["VALID", "INVALID"]
    rejection_reason: OptionalSafeDiagnosticText
    evidentiary_capability: Literal["ASSERTIVE", "NON_ASSERTIVE", "UNKNOWN"]
    capability_reason: OptionalSafeDiagnosticText
    argv: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=2048)]],
        Field(max_length=256),
    ]

    @model_validator(mode="after")
    def command_is_coherent(self) -> VerificationCommand:
        _require_unique(self.requirement_ids, "command requirement IDs")
        if self.validation_status == "VALID":
            canonical_argv = tuple(self.argv)
            canonical_text = shlex.join(self.argv)
            analysis = analyze_verification_command(
                canonical_text,
                source="repo_scan.likely_test_commands",
                contract_type="repo_native",
                trusted=True,
            )
            if (
                self.execution_mode != "ARGV"
                or self.provenance not in {"PREEXISTING_REPO_NATIVE", "HOST_AUTHORITATIVE"}
                or self.trust_level != "TRUSTED"
                or self.requirement != "REQUIRED"
                or self.evidentiary_capability != "ASSERTIVE"
                or self.rejection_reason
                or self.original_text != canonical_text
                or self.display_text != canonical_text
                or canonical_argv not in {("pytest",), ("pytest", "-q")}
                or analysis.parts != canonical_argv
                or analysis.command_family != "pytest"
                or analysis.shell_control_flow != "none"
                or not analysis.is_valid_verifier
                or analysis.capability_reason != self.capability_reason
            ):
                raise ValueError(
                    "Phase 0 permits only canonical repo-native whole-suite pytest argv"
                )
        elif self.execution_mode != "INVALID" or not self.rejection_reason or self.argv:
            raise ValueError("an invalid command must be INVALID, rejected, and have no argv")
        return self


class VerificationCheckDefinition(StrictModel):
    check_id: StableId
    domain: Literal["acceptance", "verification"]
    task_id: StableId | None
    requirement_ids: Annotated[list[StableId], Field(min_length=1, max_length=64)]
    command_ids: Annotated[list[StableId], Field(max_length=64)]

    @model_validator(mode="after")
    def references_are_unique(self) -> VerificationCheckDefinition:
        _require_unique(self.requirement_ids, "check requirement IDs")
        _require_unique(self.command_ids, "check command IDs")
        if self.domain == "verification" and not self.command_ids:
            raise ValueError("verification checks require at least one commandId")
        if self.domain == "acceptance" and self.command_ids:
            raise ValueError("acceptance checks cannot carry executable commandIds")
        return self


class VerificationContract(StrictModel):
    schema_id: Literal["alysis.forge.verification-contract/v2"] = Field(alias="schema")
    protocol_version: Literal[2]
    workspace_ref: OpaqueId
    approved_plan_fingerprint: Fingerprint
    host_plan_fingerprint: Fingerprint
    scopes: Annotated[list[VerificationScope], Field(min_length=1, max_length=256)]
    requirements: Annotated[list[VerificationRequirement], Field(min_length=1, max_length=1024)]
    commands: Annotated[list[VerificationCommand], Field(max_length=512)]
    checks: Annotated[list[VerificationCheckDefinition], Field(min_length=1, max_length=512)]
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def contract_references_and_fingerprint_are_valid(self) -> VerificationContract:
        scope_by_id = {scope.scope_id: scope for scope in self.scopes}
        requirement_by_id = {
            requirement.requirement_id: requirement for requirement in self.requirements
        }
        command_by_id = {command.command_id: command for command in self.commands}
        check_by_id = {check.check_id: check for check in self.checks}
        if len(scope_by_id) != len(self.scopes):
            raise ValueError("verification scope IDs must be unique")
        if len(requirement_by_id) != len(self.requirements):
            raise ValueError("verification requirement IDs must be unique")
        if len(command_by_id) != len(self.commands):
            raise ValueError("verification command IDs must be unique")
        if len(check_by_id) != len(self.checks):
            raise ValueError("verification check IDs must be unique")

        for command in self.commands:
            scope = scope_by_id.get(command.scope_id)
            if scope is None:
                raise ValueError("command scopeId must resolve")
            if command.task_id != scope.task_id:
                raise ValueError("command taskId must match its verification scope")
            if not set(command.requirement_ids).issubset(requirement_by_id):
                raise ValueError("command requirementIds must resolve")
            if any(
                requirement_by_id[requirement_id].task_id != command.task_id
                for requirement_id in command.requirement_ids
            ):
                raise ValueError("command requirements must have the same taskId")

        covered_requirements: set[str] = set()
        for check in self.checks:
            if not set(check.requirement_ids).issubset(requirement_by_id):
                raise ValueError("check requirementIds must resolve")
            if not set(check.command_ids).issubset(command_by_id):
                raise ValueError("check commandIds must resolve")
            if any(
                requirement_by_id[requirement_id].task_id != check.task_id
                for requirement_id in check.requirement_ids
            ):
                raise ValueError("check requirements must have the same taskId")
            if any(
                command_by_id[command_id].task_id != check.task_id
                for command_id in check.command_ids
            ):
                raise ValueError("check commands must have the same taskId")
            if check.domain == "acceptance" and any(
                requirement_by_id[requirement_id].provenance != "HOST_AUTHORITATIVE"
                or requirement_by_id[requirement_id].trust_level != "TRUSTED"
                or requirement_by_id[requirement_id].requirement != "REQUIRED"
                for requirement_id in check.requirement_ids
            ):
                raise ValueError(
                    "acceptance requirements must be HOST_AUTHORITATIVE TRUSTED REQUIRED"
                )
            if check.domain == "verification":
                check_commands = [command_by_id[command_id] for command_id in check.command_ids]
                if any(
                    command.validation_status != "VALID"
                    or command.execution_mode != "ARGV"
                    or not command.argv
                    or command.evidentiary_capability != "ASSERTIVE"
                    or command.trust_level != "TRUSTED"
                    or command.requirement != "REQUIRED"
                    for command in check_commands
                ):
                    raise ValueError(
                        "verification checks require executable VALID ASSERTIVE TRUSTED REQUIRED commands"
                    )
                required_check_requirements = {
                    requirement_id
                    for requirement_id in check.requirement_ids
                    if requirement_by_id[requirement_id].requirement == "REQUIRED"
                }
                command_requirements = {
                    requirement_id
                    for command in check_commands
                    for requirement_id in command.requirement_ids
                }
                if not required_check_requirements.issubset(command_requirements):
                    raise ValueError(
                        "verification commands must cover every required check requirement"
                    )
            covered_requirements.update(check.requirement_ids)
        required = {
            requirement.requirement_id
            for requirement in self.requirements
            if requirement.requirement == "REQUIRED"
        }
        if not required.issubset(covered_requirements):
            raise ValueError("every required verification requirement must be covered by a check")
        if verification_contract_fingerprint(self) != self.contract_fingerprint:
            raise ValueError("contractFingerprint does not match the canonical contract")
        return self


class FilesystemApprovalResource(StrictModel):
    access: Literal["read", "write"]
    kind: Literal["file", "directory"]
    path: WorkspaceScopePath

    @model_validator(mode="after")
    def recursive_path_is_directory(self) -> FilesystemApprovalResource:
        if self.path.endswith("/**") and self.kind != "directory":
            raise ValueError("recursive approval resources must have directory kind")
        return self


class FilesystemApprovalScope(StrictModel):
    schema_id: Literal["alysis.forge-host.approval-scope/v2"] = Field(alias="schema")
    kind: Literal["filesystem"]
    label: SafeAuthorityLabel
    workspace_ref: OpaqueId
    resources: Annotated[list[FilesystemApprovalResource], Field(max_length=256)]
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def fingerprint_matches_scope(self) -> FilesystemApprovalScope:
        keys = [(item.access, item.kind, item.path) for item in self.resources]
        if len(keys) != len(set(keys)):
            raise ValueError("approval filesystem resources must be unique")
        if approval_scope_fingerprint(self) != self.fingerprint:
            raise ValueError("scope fingerprint does not match scope contents")
        return self


class ProcessApprovalScope(StrictModel):
    schema_id: Literal["alysis.forge-host.approval-scope/v2"] = Field(alias="schema")
    kind: Literal["process"]
    label: SafeAuthorityLabel
    workspace_ref: OpaqueId
    cwd: WorkspaceDirectory
    argv: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=2048)]],
        Field(min_length=1, max_length=256),
    ]
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def fingerprint_matches_scope(self) -> ProcessApprovalScope:
        if approval_scope_fingerprint(self) != self.fingerprint:
            raise ValueError("scope fingerprint does not match scope contents")
        return self


class NetworkApprovalScope(StrictModel):
    schema_id: Literal["alysis.forge-host.approval-scope/v2"] = Field(alias="schema")
    kind: Literal["network"]
    label: SafeAuthorityLabel
    origins: Annotated[
        list[
            Annotated[
                str,
                Field(
                    pattern=r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?$"
                ),
            ]
        ],
        Field(min_length=1, max_length=64),
    ]
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def fingerprint_matches_scope(self) -> NetworkApprovalScope:
        _require_unique(self.origins, "network origins")
        if approval_scope_fingerprint(self) != self.fingerprint:
            raise ValueError("scope fingerprint does not match scope contents")
        return self


class ComputerUseApprovalScope(StrictModel):
    schema_id: Literal["alysis.forge-host.approval-scope/v2"] = Field(alias="schema")
    kind: Literal["computer_use"]
    label: SafeAuthorityLabel
    capabilities: Annotated[
        list[Literal["screen_view", "app_inspection", "keyboard", "pointer"]],
        Field(min_length=1, max_length=4),
    ]
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def fingerprint_matches_scope(self) -> ComputerUseApprovalScope:
        _require_unique(self.capabilities, "Computer Use capabilities")
        if approval_scope_fingerprint(self) != self.fingerprint:
            raise ValueError("scope fingerprint does not match scope contents")
        return self


class ProviderApprovalScope(StrictModel):
    schema_id: Literal["alysis.forge-host.approval-scope/v2"] = Field(alias="schema")
    kind: Literal["provider"]
    label: SafeAuthorityLabel
    provider_binding_ref: OpaqueId
    operation: Literal["plan", "generate", "review", "verify", "recover"]
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def fingerprint_matches_scope(self) -> ProviderApprovalScope:
        if approval_scope_fingerprint(self) != self.fingerprint:
            raise ValueError("scope fingerprint does not match scope contents")
        return self


ApprovalScope: TypeAlias = Annotated[
    FilesystemApprovalScope
    | ProcessApprovalScope
    | NetworkApprovalScope
    | ComputerUseApprovalScope
    | ProviderApprovalScope,
    Field(discriminator="kind"),
]


class PlanExecuteAction(StrictModel):
    kind: Literal["plan_execute"]
    host_plan_fingerprint: Fingerprint
    native_approval_fingerprint: Fingerprint
    verification_contract_fingerprint: Fingerprint
    execution_admission_fingerprint: Fingerprint
    approval_policy_fingerprint: Fingerprint


class SubjectAction(StrictModel):
    kind: Literal[
        "workspace_write", "process_execute", "network_access", "computer_control", "provider_use"
    ]
    subject_fingerprint: Fingerprint


ApprovalAction: TypeAlias = Annotated[
    PlanExecuteAction | SubjectAction,
    Field(discriminator="kind"),
]


class PlanApprovalPolicy(StrictModel):
    required: bool
    scope: FilesystemApprovalScope | None
    risk: Literal["low", "medium", "high", "critical"] | None
    summary: SafeAuthoritySummary | None
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def references_match_requirement(self) -> PlanApprovalPolicy:
        if not self.required:
            raise ValueError(
                "Phase 0 requires explicit plan approval until composite preauthorization exists"
            )
        values = (self.scope, self.risk, self.summary)
        if self.required != all(value is not None for value in values):
            raise ValueError(
                "all approval policy fields are present if and only if approval is required"
            )
        if approval_policy_fingerprint(self) != self.fingerprint:
            raise ValueError("approval policy fingerprint does not match policy contents")
        return self


class PlanReady(StrictModel):
    type: Literal["plan.ready"]
    plan_definition: PlanDefinition
    host_plan_fingerprint: Fingerprint
    native_approval_definition: NativeApprovalDefinition
    native_approval_fingerprint: Fingerprint
    verification_contract: VerificationContract
    execution_admission: ExecutionAdmission
    approval_policy: PlanApprovalPolicy

    @model_validator(mode="after")
    def plan_and_contract_are_canonically_bound(self) -> PlanReady:
        case_sensitive_paths = (
            self.execution_admission.workspace_path_policy.comparison == "case_sensitive"
        )
        task_ids = [task.task_id for task in self.plan_definition.tasks]
        _require_unique(task_ids, "plan task IDs")
        branch_identities = [
            task.branch if case_sensitive_paths else task.branch.casefold()
            for task in self.plan_definition.tasks
        ]
        if len(branch_identities) != len(set(branch_identities)):
            raise ValueError("plan task branches alias under the workspace path policy")
        if [task.order for task in self.plan_definition.tasks] != list(
            range(1, len(self.plan_definition.tasks) + 1)
        ):
            raise ValueError("plan tasks must have contiguous order matching array order")
        seen: set[str] = set()
        criteria: dict[str, tuple[str, str]] = {}
        for task in self.plan_definition.tasks:
            if not set(task.dependencies).issubset(seen):
                raise ValueError("task dependencies must reference earlier tasks")
            seen.add(task.task_id)
            for criterion in task.acceptance:
                if criterion.criterion_id in criteria:
                    raise ValueError("acceptance criterion IDs must be unique across the plan")
                criteria[criterion.criterion_id] = (task.task_id, criterion.description)

        constraints = self.plan_definition.planning_constraints
        _require_unique_comparison_paths(
            [
                record.path
                for records in (
                    constraints.target_roots,
                    constraints.forbidden_roots,
                    constraints.decoy_roots,
                    constraints.unrelated_roots,
                )
                for record in records
            ],
            "planning constraints",
            case_sensitive=case_sensitive_paths,
        )
        targets = [
            WorkspaceScope(path=record.path, kind="directory")
            for record in constraints.target_roots
        ]
        blocked = [
            WorkspaceScope(path=record.path, kind="directory")
            for records in (
                constraints.forbidden_roots,
                constraints.decoy_roots,
                constraints.unrelated_roots,
            )
            for record in records
        ]
        authored_scopes = [
            *self.plan_definition.intent.affected_scopes,
            *(
                scope
                for task in self.plan_definition.tasks
                for scope in (*task.estimated_scopes, *task.write_scopes)
            ),
        ]
        _require_unique_comparison_paths(
            [scope.path for scope in self.plan_definition.intent.affected_scopes],
            "intent affectedScopes",
            case_sensitive=case_sensitive_paths,
        )
        for task in self.plan_definition.tasks:
            _require_unique_comparison_paths(
                [scope.path for scope in task.estimated_scopes],
                f"task {task.task_id} estimatedScopes",
                case_sensitive=case_sensitive_paths,
            )
            _require_unique_comparison_paths(
                [scope.path for scope in task.write_scopes],
                f"task {task.task_id} writeScopes",
                case_sensitive=case_sensitive_paths,
            )
            if any(
                not any(
                    _scope_contains(
                        estimate,
                        write,
                        case_sensitive=case_sensitive_paths,
                    )
                    for estimate in task.estimated_scopes
                )
                for write in task.write_scopes
            ):
                raise ValueError(
                    "writeScopes must respect the admitted workspace path comparison policy"
                )
        if targets and any(
            not any(
                _scope_contains(target, scope, case_sensitive=case_sensitive_paths)
                for target in targets
            )
            for scope in authored_scopes
        ):
            raise ValueError("plan scopes must remain inside declared target roots")
        if any(
            _scopes_overlap(scope, forbidden, case_sensitive=case_sensitive_paths)
            for scope in authored_scopes
            for forbidden in blocked
        ):
            raise ValueError("plan scopes cannot overlap forbidden, decoy, or unrelated roots")
        if any(
            _scopes_overlap(target, forbidden, case_sensitive=case_sensitive_paths)
            for target in targets
            for forbidden in blocked
        ):
            raise ValueError("target roots cannot alias forbidden, decoy, or unrelated roots")

        if plan_fingerprint(self.plan_definition) != self.host_plan_fingerprint:
            raise ValueError("hostPlanFingerprint does not match the Host-v2 plan definition")
        from .canonical import canonical_fingerprint

        if (
            canonical_fingerprint(self.native_approval_definition)
            != self.native_approval_fingerprint
        ):
            raise ValueError("nativeApprovalFingerprint does not match nativeApprovalDefinition")
        if not _host_native_projection_equivalent(
            self.plan_definition, self.native_approval_definition
        ):
            raise ValueError(
                "nativeApprovalDefinition is not losslessly equivalent to planDefinition"
            )
        contract = self.verification_contract
        if contract.host_plan_fingerprint != self.host_plan_fingerprint:
            raise ValueError("verification contract must bind hostPlanFingerprint")
        if contract.approved_plan_fingerprint != self.native_approval_fingerprint:
            raise ValueError("verification contract must bind nativeApprovalFingerprint")
        if self.execution_admission.workspace_ref != contract.workspace_ref:
            raise ValueError("execution admission must bind the contract workspaceRef")
        tasks_by_id = {task.task_id: task for task in self.plan_definition.tasks}
        task_id_set = set(tasks_by_id)
        for collection in (
            contract.scopes,
            contract.requirements,
            contract.commands,
            contract.checks,
        ):
            if any(
                item.task_id is not None and item.task_id not in task_id_set for item in collection
            ):
                raise ValueError("verification contract taskIds must resolve to plan tasks")
        all_write_scopes = [
            scope for task in self.plan_definition.tasks for scope in task.write_scopes
        ]
        for verification_scope in contract.scopes:
            for paths, description in (
                (verification_scope.referenced_paths, "referencedPaths"),
                (verification_scope.allowed_outputs, "allowedOutputs"),
                (verification_scope.declared_inputs, "declaredInputs"),
            ):
                _require_unique_comparison_paths(
                    paths,
                    f"verification scope {verification_scope.scope_id} {description}",
                    case_sensitive=case_sensitive_paths,
                )
            permitted = (
                tasks_by_id[verification_scope.task_id].write_scopes
                if verification_scope.task_id is not None
                else all_write_scopes
            )
            for output in verification_scope.allowed_outputs:
                candidate = WorkspaceScope(path=output, kind="file")
                if not any(
                    _scope_contains(
                        scope,
                        candidate,
                        case_sensitive=case_sensitive_paths,
                    )
                    for scope in permitted
                ):
                    raise ValueError(
                        "verification allowedOutputs must stay within approved task writes"
                    )
        contract_task_requirements = {
            requirement.requirement_id: (requirement.task_id, requirement.text)
            for requirement in contract.requirements
            if requirement.task_id is not None
        }
        if contract_task_requirements != criteria:
            raise ValueError("task verification requirements must exactly match plan acceptance")
        expected_risk = (
            "high"
            if all_write_scopes or self.execution_admission.execution_strategy.mode == "parallel"
            else "medium"
        )
        if (
            not self.approval_policy.required
            or self.approval_policy.scope is None
            or self.approval_policy.scope.label != PHASE0_APPROVAL_SCOPE_LABEL
            or self.approval_policy.summary != PHASE0_APPROVAL_SUMMARY
            or self.approval_policy.risk != expected_risk
        ):
            raise ValueError(
                "Phase 0 approval presentation must be deterministically derived from authority"
            )
        if self.approval_policy.required:
            assert self.approval_policy.scope is not None
            if self.approval_policy.scope.workspace_ref != contract.workspace_ref:
                raise ValueError("plan approval scope must bind the contract workspaceRef")
            expected_writes = {
                (scope.path, scope.kind)
                for task in self.plan_definition.tasks
                for scope in task.write_scopes
            }
            resources = self.approval_policy.scope.resources
            actual_writes = {(resource.path, resource.kind) for resource in resources}
            if any(resource.access != "write" for resource in resources):
                raise ValueError("plan approval scope can contain only write resources")
            if actual_writes != expected_writes:
                raise ValueError(
                    "plan approval resources must exactly match canonical task writeScopes"
                )
        return self


def _require_canonical_expiry(value: datetime) -> None:
    if value.utcoffset() != timedelta(0) or value.microsecond:
        raise ValueError("approval expiresAt must be UTC with whole-second precision")


_ACTION_SCOPE_KIND = {
    "plan_execute": "filesystem",
    "workspace_write": "filesystem",
    "process_execute": "process",
    "network_access": "network",
    "computer_control": "computer_use",
    "provider_use": "provider",
}


class ApprovalRequired(StrictModel):
    type: Literal["approval.required"]
    approval_id: OpaqueId
    action: ApprovalAction
    action_fingerprint: Fingerprint
    summary: SafeAuthoritySummary
    risk: Literal["low", "medium", "high", "critical"]
    scope: ApprovalScope
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def action_matches_scope(self) -> ApprovalRequired:
        if _ACTION_SCOPE_KIND[self.action.kind] != self.scope.kind:
            raise ValueError("approval action kind must match scope kind")
        _require_canonical_expiry(self.expires_at)
        return self


class PlannerQuestion(StrictModel):
    type: Literal["planner.question"]
    question_id: OpaqueId
    prompt: NonEmptyText
    required: bool


class RunProgress(StrictModel):
    type: Literal["run.progress"]
    phase: Literal["preflight", "executing", "verifying", "reviewing", "finalizing"]
    task_id: StableId | None
    step: Annotated[int, Field(ge=0, le=JS_SAFE_INTEGER_MAX)]
    total_steps: Annotated[int, Field(ge=0, le=JS_SAFE_INTEGER_MAX)]
    task_model_steps: SafeCount | None
    summary: SafeRuntimeSummary
    worker_id: OpaqueId | None

    @model_validator(mode="after")
    def progress_is_coherent(self) -> RunProgress:
        if self.total_steps and self.step > self.total_steps:
            raise ValueError("step cannot exceed totalSteps")
        if self.phase in {"executing", "verifying"} and self.task_id is None:
            raise ValueError("executing and verifying progress requires taskId")
        if (self.task_id is None) != (self.task_model_steps is None):
            raise ValueError("taskModelSteps is present if and only if taskId is present")
        return self


class ArtifactRecord(StrictModel):
    artifact_id: OpaqueId
    role: Literal["primary", "evidence", "preview", "log", "report"]
    kind: Literal[
        "file", "directory", "image", "video", "audio", "archive", "report", "log", "diff"
    ]
    status: Literal["created", "updated", "unchanged", "missing", "failed"]
    workspace_relative_path: WorkspaceRelativePath
    digest: Fingerprint | None
    size_bytes: SafeCount | None

    @model_validator(mode="after")
    def metadata_matches_availability(self) -> ArtifactRecord:
        if (self.digest is None) != (self.size_bytes is None):
            raise ValueError("artifact digest and sizeBytes must be present together")
        available = self.status in {"created", "updated", "unchanged"}
        if available != (self.digest is not None):
            raise ValueError(
                "available artifacts require digest and size; unavailable artifacts omit both"
            )
        return self


class EvidenceRecord(StrictModel):
    evidence_id: OpaqueId
    role: Literal["acceptance", "verification", "execution", "review", "provider"]
    kind: Literal["test", "lint", "build", "screenshot", "log", "diff", "review", "recovery"]
    status: Literal["passed", "failed", "warning", "informational", "not_run"]
    check_id: StableId | None
    task_id: StableId | None
    requirement_ids: Annotated[list[StableId], Field(max_length=64)]
    command_ids: Annotated[list[StableId], Field(max_length=1)]
    real_execution: bool | None
    exit_code: Annotated[int, Field(ge=0, le=255)] | None
    timed_out: bool | None
    summary: SafeRuntimeSummary
    artifact_refs: Annotated[list[OpaqueId], Field(max_length=64)]

    @model_validator(mode="after")
    def evidence_binding_is_coherent(self) -> EvidenceRecord:
        _require_unique(self.requirement_ids, "evidence requirement IDs")
        _require_unique(self.command_ids, "evidence command IDs")
        _require_unique(self.artifact_refs, "evidence artifactRefs")
        is_contract_evidence = self.role in {"acceptance", "verification"}
        if is_contract_evidence != (self.check_id is not None and bool(self.requirement_ids)):
            raise ValueError(
                "acceptance/verification evidence requires check and requirement bindings"
            )
        if self.role == "verification":
            if len(self.command_ids) != 1 or self.real_execution is None or self.timed_out is None:
                raise ValueError(
                    "verification evidence requires exactly one command and one execution fact"
                )
            if self.status == "passed" and (
                not self.real_execution or self.exit_code != 0 or self.timed_out
            ):
                raise ValueError(
                    "passed verification evidence requires a real successful execution"
                )
        elif (
            self.command_ids
            or self.real_execution is not None
            or self.exit_code is not None
            or self.timed_out is not None
        ):
            raise ValueError("only verification evidence can contain command execution facts")
        if self.status == "passed" and not self.artifact_refs:
            raise ValueError("passed evidence requires immutable artifactRefs")
        return self


class EvidenceReady(StrictModel):
    type: Literal["evidence.ready"]
    evidence: Annotated[list[EvidenceRecord], Field(min_length=1, max_length=256)]
    artifacts: Annotated[list[ArtifactRecord], Field(max_length=256)]

    @model_validator(mode="after")
    def manifest_resolves_all_evidence_artifacts(self) -> EvidenceReady:
        evidence_ids = [record.evidence_id for record in self.evidence]
        artifact_ids = [record.artifact_id for record in self.artifacts]
        _require_unique(evidence_ids, "evidence IDs")
        _require_unique(artifact_ids, "artifact IDs")
        artifact_id_set = set(artifact_ids)
        unresolved = {
            artifact_ref
            for record in self.evidence
            for artifact_ref in record.artifact_refs
            if artifact_ref not in artifact_id_set
        }
        if unresolved:
            raise ValueError("evidence artifactRefs must resolve within the artifact manifest")
        artifacts_by_id = {record.artifact_id: record for record in self.artifacts}
        for record in self.evidence:
            if record.status == "passed" and any(
                artifacts_by_id[artifact_ref].status not in {"created", "updated", "unchanged"}
                for artifact_ref in record.artifact_refs
            ):
                raise ValueError("passed evidence requires available immutable artifacts")
        return self


CheckStatus = Literal["passed", "failed", "skipped", "not_run"]


class CheckResult(StrictModel):
    check_id: StableId
    task_id: StableId | None
    requirement_ids: Annotated[list[StableId], Field(min_length=1, max_length=64)]
    status: CheckStatus
    summary: SafeRuntimeSummary
    evidence_refs: Annotated[list[OpaqueId], Field(max_length=64)]

    @model_validator(mode="after")
    def check_is_coherent(self) -> CheckResult:
        _require_unique(self.requirement_ids, "check result requirement IDs")
        _require_unique(self.evidence_refs, "check result evidenceRefs")
        if self.status == "passed" and not self.evidence_refs:
            raise ValueError("a passed check requires evidence")
        return self


AggregateCheckStatus = Literal["passed", "failed", "not_run"]


class VerificationResult(StrictModel):
    status: AggregateCheckStatus
    checks: Annotated[list[CheckResult], Field(max_length=256)]

    @model_validator(mode="after")
    def aggregate_matches_checks(self) -> VerificationResult:
        _require_unique([check.check_id for check in self.checks], "aggregate check IDs")
        statuses = [check.status for check in self.checks]
        if self.status == "passed" and (
            not statuses or any(status != "passed" for status in statuses)
        ):
            raise ValueError("a passed aggregate requires non-empty, exclusively passed checks")
        if self.status == "failed" and "failed" not in statuses:
            raise ValueError("a failed aggregate must contain a failed check")
        if self.status == "not_run" and any(status != "not_run" for status in statuses):
            raise ValueError("a not_run aggregate can contain only not_run checks")
        return self


class TerminalReason(StrictModel):
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")]
    message: SafeDiagnosticText
    retryable: bool


class Usage(StrictModel):
    model_calls: SafeCount
    input_tokens: SafeCount
    output_tokens: SafeCount
    cost_known: bool
    total_cost_micros: SafeCount | None

    @model_validator(mode="after")
    def known_cost_has_a_value(self) -> Usage:
        if self.cost_known != (self.total_cost_micros is not None):
            raise ValueError("totalCostMicros is present if and only if costKnown is true")
        return self


class Timing(StrictModel):
    """Run timing with monotonic ``elapsedMs`` as accounting authority.

    Wall-clock timestamps are presentation/audit metadata and may diverge after
    system clock adjustments; timeout admission uses only elapsedMs.
    """

    started_at: AwareDatetime
    finished_at: AwareDatetime
    elapsed_ms: Annotated[
        int,
        Field(
            ge=0,
            le=JS_SAFE_INTEGER_MAX,
            description="Monotonic engine elapsed time used for timeout accounting.",
        ),
    ]
    timed_out: bool

    @model_validator(mode="after")
    def finish_is_not_before_start(self) -> Timing:
        if self.finished_at < self.started_at:
            raise ValueError("finishedAt must not be before startedAt")
        return self


class Workers(StrictModel):
    requested: SafeCount
    started: SafeCount
    succeeded: SafeCount
    failed: SafeCount

    @model_validator(mode="after")
    def counts_are_coherent(self) -> Workers:
        if self.started > self.requested:
            raise ValueError("started workers cannot exceed requested workers")
        if self.succeeded + self.failed != self.started:
            raise ValueError("terminal workers must all be settled")
        return self


class TaskExecutionUsage(StrictModel):
    task_id: StableId
    model_steps: SafeCount


TerminalStatus = Literal["succeeded", "failed", "cancelled", "interrupted"]


class TerminalResult(StrictModel):
    host_plan_fingerprint: Fingerprint
    native_approval_fingerprint: Fingerprint
    verification_contract_fingerprint: Fingerprint
    execution_admission_fingerprint: Fingerprint
    status: TerminalStatus
    reason: TerminalReason
    exit_code: Annotated[int, Field(ge=0, le=255)] | None
    acceptance: VerificationResult
    verification: VerificationResult
    usage: Usage
    timing: Timing
    workers: Workers
    task_usage: Annotated[list[TaskExecutionUsage], Field(min_length=1, max_length=256)]
    artifact_refs: Annotated[list[OpaqueId], Field(max_length=256)]

    @model_validator(mode="after")
    def success_requires_passed_contracts(self) -> TerminalResult:
        _require_unique(self.artifact_refs, "terminal artifactRefs")
        _require_unique([record.task_id for record in self.task_usage], "terminal task usage IDs")
        if self.usage.model_calls < sum(record.model_steps for record in self.task_usage):
            raise ValueError("modelCalls cannot underreport managed task model steps")
        if self.status == "succeeded":
            if self.acceptance.status != "passed" or self.verification.status != "passed":
                raise ValueError("succeeded requires passed acceptance and verification")
            if self.exit_code != 0:
                raise ValueError("succeeded requires exitCode 0")
            if self.timing.timed_out:
                raise ValueError("a timed-out run cannot be succeeded")
            if self.workers.failed:
                raise ValueError("a succeeded run cannot contain failed workers")
            if (
                self.workers.started != self.workers.requested
                or self.workers.succeeded != self.workers.started
            ):
                raise ValueError("a succeeded run requires every requested worker to succeed")
        return self


SnapshotPhase = Literal[
    "planning", "awaiting_approval", "ready", "running", "cancelling", "terminal"
]


class StateSnapshot(StrictModel):
    type: Literal["state.snapshot"]
    phase: SnapshotPhase
    goal: Annotated[str, Field(min_length=1, max_length=4096)] | None
    pending_approval_id: OpaqueId | None
    terminal: TerminalResult | None

    @model_validator(mode="after")
    def terminal_matches_phase(self) -> StateSnapshot:
        if (self.phase == "terminal") != (self.terminal is not None):
            raise ValueError("terminal result is present if and only if phase is terminal")
        if self.phase == "awaiting_approval" and self.pending_approval_id is None:
            raise ValueError("awaiting_approval requires pendingApprovalId")
        if self.phase != "awaiting_approval" and self.pending_approval_id is not None:
            raise ValueError("pendingApprovalId is valid only while awaiting_approval")
        return self


class RunTerminal(StrictModel):
    type: Literal["run.terminal"]
    result: TerminalResult


RequestPayload: TypeAlias = Annotated[
    HandshakeRequest | PlannerInput | ApprovalDecision | RunStart | RunCancel,
    Field(discriminator="type"),
]
ResponsePayload: TypeAlias = Annotated[
    HandshakeResponse | CommandAccepted | ProtocolError,
    Field(discriminator="type"),
]
EventPayload: TypeAlias = Annotated[
    StateSnapshot
    | PlannerQuestion
    | PlanReady
    | ApprovalRequired
    | RunProgress
    | EvidenceReady
    | RunTerminal,
    Field(discriminator="type"),
]


class EnvelopeBase(StrictModel):
    protocol: Literal["alysis.forge-host"]
    protocol_version: ProtocolVersion
    message_id: MessageId
    operation_id: OpaqueId | None
    sent_at: AwareDatetime


class RequestEnvelope(EnvelopeBase):
    kind: Literal["request"]
    request_id: RequestId
    idempotency_key: IdempotencyKey
    payload: RequestPayload

    @model_validator(mode="after")
    def operation_matches_request(self) -> RequestEnvelope:
        if isinstance(self.payload, HandshakeRequest):
            if self.operation_id is not None:
                raise ValueError("initial handshake request operationId must be null")
        elif self.operation_id is None:
            raise ValueError("correlated requests require operationId")
        return self


class ResponseEnvelope(EnvelopeBase):
    kind: Literal["response"]
    reply_to: RequestId
    payload: ResponsePayload

    @model_validator(mode="after")
    def operation_matches_response(self) -> ResponseEnvelope:
        if isinstance(self.payload, HandshakeResponse):
            if self.operation_id is not None:
                raise ValueError("handshake response operationId must be null")
        elif self.operation_id is None:
            raise ValueError("correlated responses require operationId")
        return self


class EventEnvelope(EnvelopeBase):
    kind: Literal["event"]
    sequence: PositiveSequence
    identity: Identity
    payload: EventPayload

    @model_validator(mode="after")
    def identity_and_authority_match_event(self) -> EventEnvelope:
        if self.operation_id is None:
            raise ValueError("events require operationId")
        pre_run = isinstance(self.payload, PlannerQuestion | PlanReady) or (
            isinstance(self.payload, StateSnapshot)
            and self.payload.phase in {"planning", "awaiting_approval", "ready"}
        )
        run_bound = isinstance(self.payload, RunProgress | EvidenceReady | RunTerminal) or (
            isinstance(self.payload, StateSnapshot)
            and self.payload.phase in {"running", "cancelling", "terminal"}
        )
        if pre_run and self.identity.run_id is not None:
            raise ValueError("pre-run events must not contain runId")
        if run_bound and self.identity.run_id is None:
            raise ValueError("run events require runId")

        approval_id: str | None = None
        action: ApprovalAction | None = None
        supplied_action_fingerprint: str | None = None
        scope: ApprovalScope | None = None
        expires_at: datetime | None = None
        risk: str | None = None
        summary: str | None = None
        if isinstance(self.payload, ApprovalRequired):
            approval_id = self.payload.approval_id
            action = self.payload.action
            supplied_action_fingerprint = self.payload.action_fingerprint
            scope = self.payload.scope
            expires_at = self.payload.expires_at
            risk = self.payload.risk
            summary = self.payload.summary
        if approval_id is not None:
            assert action is not None
            assert scope is not None
            assert expires_at is not None
            assert risk is not None
            assert summary is not None
            expires_wire = expires_at.isoformat().replace("+00:00", "Z")
            expected = approval_action_fingerprint(
                operation_id=self.operation_id,
                session_id=self.identity.session_id,
                generation_id=self.identity.generation_id,
                run_id=self.identity.run_id,
                revision=self.identity.revision,
                approval_id=approval_id,
                action=action.model_dump(mode="json", by_alias=True, exclude_none=False),
                scope_fingerprint=scope.fingerprint,
                expires_at=expires_wire,
                risk=risk,
                summary=summary,
            )
            if expected != supplied_action_fingerprint:
                raise ValueError("actionFingerprint does not match exact approval authority")
        return self


Envelope: TypeAlias = Annotated[
    RequestEnvelope | ResponseEnvelope | EventEnvelope,
    Field(discriminator="kind"),
]
ENVELOPE_ADAPTER = TypeAdapter(Envelope)


def utc_now() -> datetime:
    return datetime.now().astimezone()
